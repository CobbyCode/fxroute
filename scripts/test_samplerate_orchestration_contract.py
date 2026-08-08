#!/usr/bin/env python3
"""Focused sample-rate contracts for the single playback-transition owner."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import samplerate_orchestration
from playback_transition_test_support import run_main_handoff_through_coordinator


class EventLedger:
    def __init__(self) -> None:
        self.events: list[str] = []


class SamplerateOrchestrationContractTests(unittest.IsolatedAsyncioTestCase):
    async def _run_core_handoff(
        self,
        *,
        source: str,
        target_rate: int,
        operation: str = "play",
        resolver=None,
        rate_change: bool | None = None,
        use_core: bool = True,
    ):
        ledger = EventLedger()
        status = {"active_rate": 44100, "force_rate": 44100}

        def samplerate_status() -> dict:
            ledger.events.append(f"{source}.read-rate")
            return dict(status)

        async def ensure_force(rate, reason, *, policy=None) -> bool:
            ledger.events.append(f"{source}.ensure-force:{rate}")
            status["active_rate"] = rate
            status["force_rate"] = rate
            return True

        async def preset_sync(**_kwargs) -> None:
            ledger.events.append(f"{source}.preset-sync")

        async def helper_sync(*_args, **_kwargs) -> None:
            ledger.events.append(f"{source}.helper-sync")

        async def sleep(_delay: float) -> None:
            ledger.events.append(f"{source}.settle")

        async def pw_link(*_args: str) -> str:
            return (
                "ee_soe_output_level:output_FL\n"
                "ee_soe_output_level:output_FR\n"
                "ee_soe_output_level:output_FL -> alsa_output.pci-0000_00_1f.3.analog-stereo:playback_FL\n"
                "ee_soe_output_level:output_FR -> alsa_output.pci-0000_00_1f.3.analog-stereo:playback_FR\n"
            )

        main.playback_transition_epoch = 40
        with patch.object(main, "get_samplerate_status", samplerate_status), patch.object(
            main, "_ensure_playback_samplerate_force", ensure_force
        ), patch.object(main, "_sync_easyeffects_preset_for_playback_samplerate", preset_sync), patch.object(
            main, "_sync_subwoofer_runtime", helper_sync
        ), patch.object(main, "_get_current_pipewire_force_rate", lambda: status["force_rate"]), patch.object(
            main, "_run_pw_link_command", pw_link
        ), patch.object(
            main,
            "get_audio_output_overview",
            return_value={"output_mode": {"mode": "stereo", "effective_output_key": "alsa_output.pci-0000_00_1f.3.analog-stereo"}},
        ), patch.object(main.asyncio, "sleep", sleep), patch.object(main, "subwoofer_runtime", object()):
            result, runtime = await run_main_handoff_through_coordinator(
                target_rate=target_rate,
                generation=40,
                source=source,
                operation=operation,
                detail=f"{source}-coordinator-contract",
                resolver=resolver,
                rate_change=rate_change,
                use_core=use_core,
                events=ledger.events,
            )
        return result, runtime, ledger

    async def test_local_playback_handoff_shared_reconcile(self):
        """Local activation owns gate, rate, graph, and commit through one coordinator."""
        result, runtime, ledger = await self._run_core_handoff(
            source="local", target_rate=48000,
        )
        self.assertTrue(result.committed)
        self.assertLess(ledger.events.index("gate.set:True"), ledger.events.index("local.ensure-force:48000"))
        self.assertLess(ledger.events.index("local.ensure-force:48000"), ledger.events.index("effects-helper-links"))
        self.assertNotIn("local.preset-sync", ledger.events)
        self.assertLess(ledger.events.index("effects-helper-links"), ledger.events.index("graph-readback"))
        self.assertLess(ledger.events.index("graph-readback"), ledger.events.index("source-volume:100"))
        self.assertLess(ledger.events.index("source-volume:100"), ledger.events.index("dsp-stabilize"))
        self.assertLess(ledger.events.index("dsp-stabilize"), ledger.events.index("commit-readback"))
        self.assertLess(ledger.events.index("commit-readback"), ledger.events.index("gate.set:False"))
        # d513424 contract: the audible gate open ends with set_hardware_mute(False)
        # followed only by the required hardware-mute readbacks (post-set readback
        # plus the before/after-gate-open audible checks).
        gate_open_index = ledger.events.index("gate.set:False")
        self.assertTrue(
            all(event == "gate.read" for event in ledger.events[gate_open_index + 1 :]),
            "audible gate open must be followed only by hardware-mute readbacks",
        )
        self.assertFalse(runtime.muted)

    async def test_radio_post_load_handoff_switches_once(self):
        """Radio decodes its live rate under the gate before commit."""
        async def resolve(_request):
            return 48000

        result, runtime, ledger = await self._run_core_handoff(
            source="radio", target_rate=44100, resolver=resolve,
        )
        self.assertEqual(result.target_rate, 48000)
        self.assertEqual(ledger.events.count("radio.ensure-force:48000"), 1)
        self.assertLess(ledger.events.index("gate.set:True"), ledger.events.index("radio.ensure-force:48000"))
        self.assertLess(ledger.events.index("effects-helper-links"), ledger.events.index("graph-readback"))
        self.assertLess(ledger.events.index("graph-readback"), ledger.events.index("source-volume:100"))
        self.assertLess(ledger.events.index("source-volume:100"), ledger.events.index("dsp-stabilize"))
        self.assertLess(ledger.events.index("dsp-stabilize"), ledger.events.index("commit-readback"))
        self.assertFalse(runtime.muted)
        self.assertEqual(samplerate_orchestration.RADIO_POLICY.initial_alignment_timeout_ms, 400)
        self.assertEqual(samplerate_orchestration.RADIO_POLICY.post_pulse_alignment_timeout_ms, 1200)

    async def test_spotify_entry_uses_coordinator_gate_and_commit(self):
        """Spotify has the same gate/commit contract and no direct prearm path."""
        result, runtime, ledger = await self._run_core_handoff(
            source="spotify", target_rate=44100, operation="spotify-play",
            use_core=False, rate_change=True,
        )
        self.assertTrue(result.committed)
        self.assertLess(ledger.events.index("gate.set:True"), ledger.events.index("start"))
        self.assertLess(ledger.events.index("commit-readback"), ledger.events.index("gate.set:False"))
        self.assertFalse(runtime.muted)

    async def test_measurement_session_open_is_neutral_and_release_uses_coordinator(self):
        """A window heartbeat is neutral; active playback restore has one owner."""
        ledger = EventLedger()
        original = {
            name: getattr(main, name)
            for name in (
                "measurement_sr_session", "playback_transition_coordinator",
                "_playback_state_before_measurement",
                "current_track_info", "player_instance",
            )
        }

        class FakeCoordinator:
            async def restore_measurement(self, **kwargs):
                ledger.events.append(
                    f"restore:{kwargs['source']}:{kwargs['target_rate']}:{kwargs['should_play']}"
                )
                return SimpleNamespace(committed=True)

        try:
            session = main.MeasurementSampleRateSession()
            main.measurement_sr_session = session
            main.playback_transition_coordinator = FakeCoordinator()
            main._playback_state_before_measurement = {
                "source": "local", "url": "/music/a.flac", "current_file": "/music/a.flac",
                "expected_rate": 44100,
                "was_playing": True,
            }
            main.current_track_info = {"source": "local", "url": "/music/a.flac"}
            main.player_instance = SimpleNamespace(
                state={"current_file": "/music/a.flac", "ended": False}
            )
            await session.request_open()
            self.assertEqual(ledger.events, [])
            session.active = True
            session.close_requested = True
            session._rate_changed = True
            session.original_force_rate = 44100
            await session._release()
        finally:
            for name, value in original.items():
                setattr(main, name, value)

        self.assertEqual(ledger.events, ["restore:local:44100:True"])

    async def test_resume_uses_coordinator_gate_and_confirmed_commit(self):
        result, runtime, ledger = await self._run_core_handoff(
            source="local", target_rate=44100, operation="resume",
            rate_change=False, use_core=False,
        )
        self.assertTrue(result.committed)
        self.assertLess(ledger.events.index("gate.set:True"), ledger.events.index("start"))
        self.assertLess(ledger.events.index("commit-readback"), ledger.events.index("gate.set:False"))
        self.assertFalse(runtime.muted)

    async def test_status_repair_wrapper_logs_post_attempt_active_rate(self):
        statuses = iter((
            {"active_rate": 48000, "force_rate": 0},
            {"active_rate": 44100, "force_rate": 44100},
        ))

        with patch.object(main, "get_samplerate_status", side_effect=lambda: next(statuses)), patch.object(
            main, "_set_pipewire_force_rate"
        ), patch.object(main, "_wait_for_samplerate_alignment", return_value=False), patch.object(
            main, "_suspend_resume_playback_sink", return_value=False
        ), self.assertLogs("main", level="WARNING") as captured:
            result = await main._ensure_playback_samplerate_force(
                44100,
                "status-drift-repair:radio",
                policy=samplerate_orchestration.STATUS_DRIFT_REPAIR_POLICY,
            )

        self.assertFalse(result)
        self.assertTrue(any("active_rate=44100" in line for line in captured.output))

    async def test_status_recovery_submits_only_to_coordinator(self):
        calls = []

        class FakeCoordinator:
            transition_active = False
            last_successful_commit_id = "tr-status-repair"

            def recovery_context_is_current(self, context_id):
                return context_id == self.last_successful_commit_id

            async def run_recovery(self, **kwargs):
                if await kwargs["validate"]():
                    return await kwargs["execute"]()
                return None

        async def run(request):
            calls.append(request)
            return SimpleNamespace(target_rate=request.target_rate)

        track = {"source": "radio", "url": "https://radio.example/live", "sample_rate_hz": 44100}
        with patch.object(main, "playback_transition_coordinator", FakeCoordinator()), patch.object(
            main, "player_instance", SimpleNamespace(state={
                "current_file": "https://radio.example/live",
                "playing": True,
                "paused": False,
                "ended": False,
            })
        ), patch.object(main, "current_track_info", dict(track)), patch.object(
            main, "coordinator_last_successful_commit_id", "tr-status-repair"
        ), patch.object(main, "_run_coordinated_transition", run):
            await main._request_coordinated_recovery(track, "status-drift-repair")

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].operation, "recovery")
        self.assertEqual(calls[0].source, "radio")
        self.assertEqual(calls[0].target_rate, 44100)


if __name__ == "__main__":
    unittest.main()
