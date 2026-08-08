#!/usr/bin/env python3
"""Focused regressions for Coordinator-owned radio live-rate resolution."""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from playback_transition_test_support import run_main_handoff_through_coordinator


class SamplerateDriftWatcherTests(unittest.IsolatedAsyncioTestCase):
    """Stable MPV/source drift is observed before one Coordinator request."""

    async def asyncSetUp(self):
        self.originals = {
            name: getattr(main, name)
            for name in (
                "player_instance", "current_track_info",
                "playback_transition_coordinator", "measurement_sr_session",
                "samplerate_drift_signature", "samplerate_drift_readbacks",
                "_is_measurement_window_open", "get_samplerate_status",
            )
        }

        class Player:
            _running = True
            state = {
                "current_file": "https://radio.example/48k",
                "paused": False,
                "playing": True,
                "ended": False,
            }

        main.player_instance = Player()
        main.current_track_info = {
            "id": "radio_48k",
            "source": "radio",
            "url": "https://radio.example/48k",
            "sample_rate_hz": 44100,
        }
        main.playback_transition_coordinator = type("Coordinator", (), {"transition_active": False})()
        main.measurement_sr_session = type("Measurement", (), {"active": False})()
        main._is_measurement_window_open = lambda: False
        main.get_samplerate_status = lambda: {
            "active_rate": 44100,
            "force_rate": 44100,
        }
        main.samplerate_drift_signature = None
        main.samplerate_drift_readbacks = 0

    async def asyncTearDown(self):
        for name, value in self.originals.items():
            setattr(main, name, value)

    async def test_two_matching_mismatch_readbacks_request_one_recovery(self):
        """MPV/track 44.1 with hardware at 48 kHz requests repair at 44.1."""
        recovery = AsyncMock()
        with patch.object(main, "_get_player_audio_samplerate", return_value=44100), patch.object(
            main,
            "get_samplerate_status",
            return_value={"active_rate": 48000, "force_rate": 48000},
        ), patch.object(
            main, "_request_coordinated_recovery", recovery
        ):
            await main._observe_playback_samplerate_drift()
            recovery.assert_not_awaited()
            await main._observe_playback_samplerate_drift()

        recovery.assert_awaited_once()
        self.assertEqual(recovery.await_args.args[1], "samplerate-drift-watcher")
        self.assertTrue(recovery.await_args.kwargs["reload_source"])
        self.assertEqual(recovery.await_args.args[0]["sample_rate_hz"], 44100)
        self.assertEqual(recovery.await_args.kwargs["diagnosis"]["mpv_rate"], 44100)
        self.assertEqual(recovery.await_args.kwargs["diagnosis"]["hardware_rate"], 48000)

    async def test_mpvliverate_is_authoritative_when_track_rate_is_stale(self):
        """MPV 48 kHz / track 44.1 / hardware 44.1 repairs to 48 kHz."""
        recovery = AsyncMock()
        with patch.object(main, "_get_player_audio_samplerate", return_value=48000), patch.object(
            main,
            "get_samplerate_status",
            return_value={"active_rate": 44100, "force_rate": 44100},
        ), patch.object(main, "_request_coordinated_recovery", recovery):
            await main._observe_playback_samplerate_drift()
            recovery.assert_not_awaited()
            await main._observe_playback_samplerate_drift()

        recovery.assert_awaited_once()
        self.assertEqual(recovery.await_args.args[0]["sample_rate_hz"], 48000)
        self.assertEqual(recovery.await_args.kwargs["diagnosis"]["track_rate"], 44100)
        self.assertEqual(recovery.await_args.kwargs["diagnosis"]["mpv_rate"], 48000)

    async def test_matching_mpv_track_and_hardware_rates_are_healthy(self):
        recovery = AsyncMock()
        with patch.object(main, "_get_player_audio_samplerate", return_value=44100), patch.object(
            main,
            "get_samplerate_status",
            return_value={"active_rate": 44100, "force_rate": None},
        ), patch.object(main, "_request_coordinated_recovery", recovery):
            await main._observe_playback_samplerate_drift()
            await main._observe_playback_samplerate_drift()

        recovery.assert_not_awaited()

    async def test_stable_mismatch_resets_when_source_changes(self):
        recovery = AsyncMock()
        with patch.object(main, "_get_player_audio_samplerate", return_value=48000), patch.object(
            main, "_request_coordinated_recovery", recovery
        ):
            await main._observe_playback_samplerate_drift()
            main.current_track_info = {
                "id": "radio_other",
                "source": "radio",
                "url": "https://radio.example/other",
                "sample_rate_hz": 44100,
            }
            main.player_instance.state["current_file"] = "https://radio.example/other"
            await main._observe_playback_samplerate_drift()

        recovery.assert_not_awaited()

    async def test_active_transition_or_measurement_never_requests_recovery(self):
        recovery = AsyncMock()
        with patch.object(main, "_get_player_audio_samplerate", return_value=48000), patch.object(
            main, "_request_coordinated_recovery", recovery
        ):
            main.playback_transition_coordinator.transition_active = True
            await main._observe_playback_samplerate_drift()
            main.playback_transition_coordinator.transition_active = False
            main.measurement_sr_session.active = True
            await main._observe_playback_samplerate_drift()

        recovery.assert_not_awaited()


class RadioPostLoadHandoffTests(unittest.IsolatedAsyncioTestCase):
    """Live radio-rate resolution under the Coordinator's output gate.

    Covers the post-loadfile phase-dependent handoff contract:
    - 48 -> 48 and 44.1 -> 44.1: no rate switch at all
    - 44.1 -> 48 and 48 -> 44.1: exactly one switch, playback stays paused
      until the handoff completed (the caller only releases pause after
      this function returned)
    - stale transition generation aborts the live-rate wait
    - missing live rate: safe RADIO_EXPECTED_SAMPLE_RATE_HZ fallback with
      clean error logging
    - local and spotify resolution paths stay unchanged (covered above)
    """

    async def asyncSetUp(self):
        self.originals = {
            name: getattr(main, name)
            for name in (
                "playback_transition_epoch", "subwoofer_runtime",
                "asyncio",
            )
        }
        main.playback_transition_epoch = 100
        main.subwoofer_runtime = object()

    async def asyncTearDown(self):
        for name, value in self.originals.items():
            setattr(main, name, value)

    async def _run_handoff(
        self,
        track,
        previous_rate,
        *,
        live_rates,
        active_rate,
        generation=100,
        timeout_ms=1000,
    ):
        """Run radio live-rate resolution inside the Coordinator.

        live_rates: list -> side_effect sequence; callable -> always used.
        The samplerate status is a mutable dict: the mocked ensure-force
        updates it, so the Coordinator's final verification sees the
        new rate (exactly one switch, no parallel rollback).
        """
        calls = {"force": [], "preset": [], "subwoofer": []}
        logs = []
        status = {"active_rate": active_rate}

        async def force(rate, reason, *, policy=None):
            calls["force"].append((rate, reason))
            status["active_rate"] = rate
            status["force_rate"] = rate
            return True

        async def preset_sync(**kwargs):
            calls["preset"].append(kwargs)

        async def helper_sync(*args, **kwargs):
            calls["subwoofer"].append((args, kwargs))

        async def noop_sleep(_delay):
            return None

        async def ee_ports_present(*_args):
            return (
                "ee_soe_output_level:output_FL\n"
                "ee_soe_output_level:output_FR\n"
                "ee_soe_output_level:output_FL -> alsa_output.pci-0000_00_1f.3.analog-stereo:playback_FL\n"
                "ee_soe_output_level:output_FR -> alsa_output.pci-0000_00_1f.3.analog-stereo:playback_FR\n"
            )

        rate_mock = (
            patch.object(main, "_get_player_audio_samplerate", side_effect=live_rates)
            if isinstance(live_rates, list)
            else patch.object(main, "_get_player_audio_samplerate", return_value=live_rates)
        )

        async def resolve_live_rate(_request):
            if live_rates is None:
                live_rate = None
            else:
                live_rate = await main._wait_for_radio_live_rate_after_load(
                    previous_rate,
                    transition_generation=generation,
                    timeout_ms=timeout_ms,
                )
            if not isinstance(live_rate, int) or live_rate <= 0:
                logs.append(("error", ("radio live rate unavailable",)))
                return main.RADIO_EXPECTED_SAMPLE_RATE_HZ
            return live_rate

        with rate_mock, patch.object(
            main, "get_samplerate_status", side_effect=lambda: dict(status)
        ), patch.object(main, "_ensure_playback_samplerate_force", force), patch.object(
            main, "_sync_easyeffects_preset_for_playback_samplerate", preset_sync
        ), patch.object(main, "_sync_subwoofer_runtime", helper_sync), patch.object(
            main, "_get_current_pipewire_force_rate", lambda: 0
        ), patch.object(main, "_run_pw_link_command", ee_ports_present), patch.object(
            main, "get_audio_output_overview",
            return_value={"output_mode": {"mode": "stereo", "effective_output_key": "alsa_output.pci-0000_00_1f.3.analog-stereo"}},
        ), patch.object(
            main.asyncio, "sleep", noop_sleep
        ), patch.object(main, "logger", type("L", (), {"info": lambda *a, **k: logs.append(("info", a)), "warning": lambda *a, **k: logs.append(("warning", a)), "error": lambda *a, **k: logs.append(("error", a)), "debug": lambda *a, **k: None})()):
            result, _runtime = await run_main_handoff_through_coordinator(
                target_rate=main.RADIO_EXPECTED_SAMPLE_RATE_HZ,
                generation=generation,
                source="radio",
                target_url=track["url"],
                detail="radio-post-load-handoff",
                resolver=resolve_live_rate,
            )
        return result.target_rate, calls, logs

    async def test_48_to_48_no_rate_switch(self):
        track = {"id": "radio_48b", "source": "radio", "url": "https://radio.example/48b"}
        # Same rate: the live rate needs RADIO_POST_LOAD_RATE_STABILITY_POLLS
        # stable polls before it is accepted, then no switch may happen.
        stable = [48000] * main.RADIO_POST_LOAD_RATE_STABILITY_POLLS
        result, calls, logs = await self._run_handoff(
            track, 48000, live_rates=stable, active_rate=48000,
        )
        self.assertEqual(result, 48000)
        self.assertEqual(calls["force"], [], "48->48 must not force a rate")
        self.assertEqual(calls["preset"], [], "48->48 must not bounce the preset")
        self.assertEqual(calls["subwoofer"], [], "48->48 must not touch the helper")

    async def test_44_to_44_no_rate_switch(self):
        track = {"id": "radio_44b", "source": "radio", "url": "https://radio.example/44b"}
        stable = [44100] * main.RADIO_POST_LOAD_RATE_STABILITY_POLLS
        result, calls, logs = await self._run_handoff(
            track, 44100, live_rates=stable, active_rate=44100,
        )
        self.assertEqual(result, 44100)
        self.assertEqual(calls["force"], [])
        self.assertEqual(calls["preset"], [])
        self.assertEqual(calls["subwoofer"], [])

    async def test_44_to_48_switches_exactly_once(self):
        track = {"id": "radio_48c", "source": "radio", "url": "https://radio.example/48c"}
        # Different rate is accepted immediately (the deviation is the signal
        # that the new stream decoded).
        result, calls, logs = await self._run_handoff(
            track, 44100, live_rates=[48000], active_rate=44100,
        )
        self.assertEqual(result, 48000)
        self.assertEqual(calls["force"], [(48000, "radio-post-load-handoff")])
        self.assertEqual(
            len(calls["preset"]),
            0,
            "a working EasyEffects graph must not reload its preset for rate alone",
        )
        # This fixture uses the stereo graph; no subwoofer helper is part of
        # that topology.  The Coordinator still performs the canonical graph
        # readback before opening the gate.
        self.assertEqual(len(calls["subwoofer"]), 0)

    async def test_48_to_44_switches_exactly_once(self):
        track = {"id": "radio_44c", "source": "radio", "url": "https://radio.example/44c"}
        result, calls, logs = await self._run_handoff(
            track, 48000, live_rates=[44100], active_rate=48000,
        )
        self.assertEqual(result, 44100)
        self.assertEqual(calls["force"], [(44100, "radio-post-load-handoff")])
        self.assertEqual(
            len(calls["preset"]),
            0,
            "a working EasyEffects graph must not reload its preset for rate alone",
        )
        self.assertEqual(len(calls["subwoofer"]), 0)

    async def test_first_radio_start_accepts_any_valid_live_rate(self):
        # previous_rate None (no prior stream): first valid live rate wins.
        track = {"id": "radio_48d", "source": "radio", "url": "https://radio.example/48d"}
        result, calls, logs = await self._run_handoff(
            track, None, live_rates=[48000], active_rate=44100,
        )
        self.assertEqual(result, 48000)
        self.assertEqual(calls["force"], [(48000, "radio-post-load-handoff")])

    async def test_stale_generation_aborts_live_rate_wait(self):
        # A rapid station switch bumps playback_transition_epoch while
        # the old handoff still waits; only the newest generation may win.
        # The generation must flip after the first stable same-rate poll
        # (during sleep), not before the first check.
        def bump_during_sleep(_delay):
            main.playback_transition_epoch += 1

        with patch.object(
            main, "_get_player_audio_samplerate", return_value=44100
        ), patch.object(main.asyncio, "sleep", side_effect=bump_during_sleep):
            rate = await main._wait_for_radio_live_rate_after_load(
                44100, transition_generation=100, timeout_ms=1000,
            )
        self.assertIsNone(rate, "stale generation must abort, not return a rate")

    async def test_stale_generation_aborts_complete_handoff_without_switch(self):
        # Even when a live rate becomes available, a stale generation must
        # abort the whole handoff: no force/preset/helper for an old
        # transition while a newer station switch already took over.
        track = {"id": "radio_stale", "source": "radio", "url": "https://radio.example/stale"}

        def bump_during_sleep(_delay):
            main.playback_transition_epoch += 1

        # Same-rate poll: only accepted after stability polls, so the sleep
        # (and the generation bump inside it) runs before a rate is returned.
        async def resolver(_request):
            rate = await main._wait_for_radio_live_rate_after_load(
                44100, transition_generation=100, timeout_ms=1000,
            )
            if rate is None:
                raise RuntimeError("stale transition generation")
            return rate

        with patch.object(
            main, "_get_player_audio_samplerate", return_value=44100
        ), patch.object(main.asyncio, "sleep", side_effect=bump_during_sleep):
            with self.assertRaises(RuntimeError) as ctx:
                await run_main_handoff_through_coordinator(
                    target_rate=44100,
                    generation=100,
                    source="radio",
                    target_url=track["url"],
                    detail="radio-post-load-handoff",
                    resolver=resolver,
                )
        self.assertIn("stale transition generation", str(ctx.exception))

    async def test_missing_live_rate_uses_safe_fallback_and_logs_error(self):
        track = {"id": "radio_x", "source": "radio", "url": "https://radio.example/x"}
        result, calls, logs = await self._run_handoff(
            track, 44100, live_rates=None, active_rate=48000, timeout_ms=100,
        )
        # Safe fallback to the configured radio rate, exactly one switch to it.
        self.assertEqual(result, main.RADIO_EXPECTED_SAMPLE_RATE_HZ)
        self.assertEqual(
            calls["force"], [(main.RADIO_EXPECTED_SAMPLE_RATE_HZ, "radio-post-load-handoff")]
        )
        self.assertTrue(
            any(kind == "error" for kind, _ in logs),
            "timeout without live rate must be logged as an error",
        )

    async def test_no_switch_when_fallback_matches_active_rate(self):
        track = {"id": "radio_y", "source": "radio", "url": "https://radio.example/y"}
        # Sink already on the safe fallback rate: timeout stays a no-op.
        result, calls, logs = await self._run_handoff(
            track, 48000, live_rates=None, active_rate=main.RADIO_EXPECTED_SAMPLE_RATE_HZ,
            timeout_ms=100,
        )
        self.assertEqual(result, main.RADIO_EXPECTED_SAMPLE_RATE_HZ)
        self.assertEqual(calls["force"], [])
        self.assertEqual(calls["preset"], [])
        self.assertEqual(calls["subwoofer"], [])


if __name__ == "__main__":
    unittest.main()
