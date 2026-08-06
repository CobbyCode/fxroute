#!/usr/bin/env python3
"""Focused regressions for phase-dependent radio live-rate resolution.

Covers the SR radio samplerate recovery contract:
- pre-loadfile radio handoff keeps RADIO_EXPECTED_SAMPLE_RATE_HZ (44100)
- post-loadfile callers may opt into the live mpv rate via
  prefer_live_radio_rate=True and fall back to 44100 while mpv has no
  valid rate yet
- station switches are followed (44.1 -> 48 -> 44.1), no stale rate reuse
- local and spotify resolution paths stay unchanged
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


class RadioLiveSamplerateResolutionTests(unittest.IsolatedAsyncioTestCase):
    """_resolve_expected_playback_samplerate phase behavior."""

    async def test_radio_without_option_uses_fixed_44100(self):
        # Pre-loadfile handoff must keep the configured radio rate even when
        # the player still exposes (stale) audio params.
        with patch.object(main, "_get_player_audio_samplerate", return_value=48000):
            rate = await main._resolve_expected_playback_samplerate("radio")
        self.assertEqual(rate, 44100)

    async def test_radio_prefer_live_uses_player_rate(self):
        with patch.object(main, "_get_player_audio_samplerate", return_value=48000):
            rate = await main._resolve_expected_playback_samplerate(
                "radio", prefer_live_radio_rate=True
            )
        self.assertEqual(rate, 48000)

    async def test_radio_prefer_live_falls_back_when_unavailable(self):
        with patch.object(main, "_get_player_audio_samplerate", return_value=None):
            rate = await main._resolve_expected_playback_samplerate(
                "radio", prefer_live_radio_rate=True
            )
        self.assertEqual(rate, 44100)

    async def test_radio_prefer_live_falls_back_on_invalid_rate(self):
        for invalid in (0, -1, "48000", {}):
            with patch.object(main, "_get_player_audio_samplerate", return_value=invalid):
                rate = await main._resolve_expected_playback_samplerate(
                    "radio", prefer_live_radio_rate=True
                )
            self.assertEqual(rate, 44100, f"invalid rate {invalid!r} must fall back")

    async def test_radio_prefer_live_tracks_44_1_to_48_to_44_1(self):
        # Each switch must be followed; the resolved rate is always the
        # current player rate, never a cached/stale one.
        with patch.object(
            main, "_get_player_audio_samplerate", side_effect=[44100, 48000, 44100]
        ):
            rates = [
                await main._resolve_expected_playback_samplerate(
                    "radio", prefer_live_radio_rate=True
                ),
                await main._resolve_expected_playback_samplerate(
                    "radio", prefer_live_radio_rate=True
                ),
                await main._resolve_expected_playback_samplerate(
                    "radio", prefer_live_radio_rate=True
                ),
            ]
        self.assertEqual(rates, [44100, 48000, 44100])

    async def test_local_path_unchanged_waits_for_player_rate(self):
        # Local playback must keep waiting for the player rate; the radio
        # option must not alter local behavior.
        async def wait(timeout_ms: int = 900) -> int:
            return 96000

        with patch.object(main, "_wait_for_player_audio_samplerate", side_effect=wait):
            rate = await main._resolve_expected_playback_samplerate(
                "local", prefer_live_radio_rate=True
            )
        self.assertEqual(rate, 96000)

    async def test_spotify_path_unchanged_waits_for_player_rate(self):
        # Spotify is not radio: resolution keeps the generic player-rate wait.
        async def wait(timeout_ms: int = 900) -> int:
            return 48000

        with patch.object(main, "_wait_for_player_audio_samplerate", side_effect=wait):
            rate = await main._resolve_expected_playback_samplerate(
                "spotify", prefer_live_radio_rate=True
            )
        self.assertEqual(rate, 48000)


class RadioMismatchRecoveryLiveRateTests(unittest.IsolatedAsyncioTestCase):
    """_maybe_recover_samplerate_mismatch opts into the live radio rate."""

    async def asyncSetUp(self):
        self.originals = {
            name: getattr(main, name)
            for name in (
                "easyeffects_manager", "player_instance", "source_transition_lock",
                "playback_transition_generation", "current_track_info",
                "asyncio",
            )
        }
        main.easyeffects_manager = object()
        main.player_instance = type("Player", (), {"_running": True})()
        main.source_transition_lock = None
        main.playback_transition_generation = 42
        main.current_track_info = {
            "id": "radio_48k", "source": "radio", "url": "https://radio.example/48k"
        }

    async def asyncTearDown(self):
        for name, value in self.originals.items():
            setattr(main, name, value)

    async def test_recovery_forces_live_48k_when_sink_is_44_1(self):
        track = {"id": "radio_48k", "source": "radio", "url": "https://radio.example/48k"}
        resolved = []
        forced = []

        async def resolve(source: str, prefer_live_radio_rate: bool = False) -> int:
            resolved.append((source, prefer_live_radio_rate))
            return 48000

        async def force(rate: int, reason: str, policy=None) -> bool:
            forced.append((rate, reason))
            return True

        async def noop_sleep(_delay: float) -> None:
            return None

        with patch.object(main, "_resolve_expected_playback_samplerate", resolve), patch.object(
            main, "_ensure_playback_samplerate_force", force
        ), patch.object(main, "get_samplerate_status", return_value={"active_rate": 44100}), patch.object(
            main, "_playback_transition_context_is_current", return_value=True
        ), patch.object(main, "_current_track_matches", return_value=True), patch.object(
            main, "_sync_easyeffects_preset_for_playback_samplerate", AsyncMock()
        ), patch.object(main, "refresh_peak_monitor_after_effects_change", AsyncMock()), patch.object(
            main.asyncio, "sleep", noop_sleep
        ):
            await main._maybe_recover_samplerate_mismatch(
                track, transition_generation=42
            )

        # Post-loadfile recovery must resolve the live radio rate (48 kHz)
        # and force the sink to it, instead of assuming 44.1 kHz.
        self.assertEqual(resolved, [("radio", True)])
        self.assertEqual(forced, [(48000, "radio-samplerate-mismatch")])

    async def test_recovery_noop_when_live_rate_matches_sink(self):
        track = {"id": "radio_44k", "source": "radio", "url": "https://radio.example/44k"}

        async def resolve(source: str, prefer_live_radio_rate: bool = False) -> int:
            return 44100

        force = AsyncMock()
        preset_sync = AsyncMock()
        peak_refresh = AsyncMock()
        with patch.object(main, "_resolve_expected_playback_samplerate", resolve), patch.object(
            main, "_ensure_playback_samplerate_force", force
        ), patch.object(main, "get_samplerate_status", return_value={"active_rate": 44100}), patch.object(
            main, "_playback_transition_context_is_current", return_value=True
        ), patch.object(main, "_current_track_matches", return_value=True), patch.object(
            main, "_sync_easyeffects_preset_for_playback_samplerate", preset_sync
        ), patch.object(main, "refresh_peak_monitor_after_effects_change", peak_refresh), patch.object(
            main.asyncio, "sleep", AsyncMock()
        ):
            await main._maybe_recover_samplerate_mismatch(
                track, transition_generation=42
            )
            # 44.1 kHz stream on a 44.1 kHz sink: no force, no preset bounce.
            force.assert_not_awaited()
            preset_sync.assert_not_awaited()
            peak_refresh.assert_not_awaited()

    async def test_recovery_fallback_44100_when_player_rate_unavailable(self):
        track = {"id": "radio_unknown", "source": "radio", "url": "https://radio.example/x"}

        async def resolve(source: str, prefer_live_radio_rate: bool = False) -> int:
            return 44100

        force = AsyncMock()
        preset_sync = AsyncMock()
        with patch.object(main, "_resolve_expected_playback_samplerate", resolve), patch.object(
            main, "_ensure_playback_samplerate_force", force
        ), patch.object(main, "get_samplerate_status", return_value={"active_rate": 44100}), patch.object(
            main, "_playback_transition_context_is_current", return_value=True
        ), patch.object(main, "_current_track_matches", return_value=True), patch.object(
            main, "_sync_easyeffects_preset_for_playback_samplerate", preset_sync
        ), patch.object(main, "refresh_peak_monitor_after_effects_change", AsyncMock()), patch.object(
            main.asyncio, "sleep", AsyncMock()
        ):
            await main._maybe_recover_samplerate_mismatch(
                track, transition_generation=42
            )
            # Safe 44.1 kHz fallback: sink already at 44100 -> consistent no-op,
            # never an invalid force-rate.
            force.assert_not_awaited()
            preset_sync.assert_not_awaited()


class RadioPostLoadHandoffTests(unittest.IsolatedAsyncioTestCase):
    """_wait_for_radio_live_rate_after_load / _complete_radio_handoff_after_load.

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
                "playback_transition_generation", "subwoofer_runtime",
                "asyncio",
            )
        }
        main.playback_transition_generation = 100
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
        """Run _complete_radio_handoff_after_load with mocked primitives.

        live_rates: list -> side_effect sequence; callable -> always used.
        The samplerate status is a mutable dict: the mocked ensure-force
        updates it, so the shared handoff's final verification sees the
        new rate (exactly one switch, no rollback).
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
            result = await main._complete_radio_handoff_after_load(
                track, previous_rate,
                transition_generation=generation,
                timeout_ms=timeout_ms,
            )
        return result, calls, logs

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
        self.assertEqual(len(calls["preset"]), 1)
        self.assertEqual(len(calls["subwoofer"]), 1)

    async def test_48_to_44_switches_exactly_once(self):
        track = {"id": "radio_44c", "source": "radio", "url": "https://radio.example/44c"}
        result, calls, logs = await self._run_handoff(
            track, 48000, live_rates=[44100], active_rate=48000,
        )
        self.assertEqual(result, 44100)
        self.assertEqual(calls["force"], [(44100, "radio-post-load-handoff")])
        self.assertEqual(len(calls["preset"]), 1)
        self.assertEqual(len(calls["subwoofer"]), 1)

    async def test_first_radio_start_accepts_any_valid_live_rate(self):
        # previous_rate None (no prior stream): first valid live rate wins.
        track = {"id": "radio_48d", "source": "radio", "url": "https://radio.example/48d"}
        result, calls, logs = await self._run_handoff(
            track, None, live_rates=[48000], active_rate=44100,
        )
        self.assertEqual(result, 48000)
        self.assertEqual(calls["force"], [(48000, "radio-post-load-handoff")])

    async def test_stale_generation_aborts_live_rate_wait(self):
        # A rapid station switch bumps playback_transition_generation while
        # the old handoff still waits; only the newest generation may win.
        # The generation must flip after the first stable same-rate poll
        # (during sleep), not before the first check.
        def bump_during_sleep(_delay):
            main.playback_transition_generation += 1

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
            main.playback_transition_generation += 1

        # Same-rate poll: only accepted after stability polls, so the sleep
        # (and the generation bump inside it) runs before a rate is returned.
        with patch.object(
            main, "_get_player_audio_samplerate", return_value=44100
        ), patch.object(main.asyncio, "sleep", side_effect=bump_during_sleep):
            result = await main._complete_radio_handoff_after_load(
                track, 44100, transition_generation=100, timeout_ms=1000,
            )
        self.assertIsNone(result)

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
