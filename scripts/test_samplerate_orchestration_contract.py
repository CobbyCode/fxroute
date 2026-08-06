#!/usr/bin/env python3
"""SR-ORCH-002 event-ledger contract tests.

These tests pin the current sample-rate orchestration order after SR-ORCH-002.
All PipeWire, mpv, EasyEffects, helper, and peak-monitor I/O is mocked; this
suite never invokes live audio commands.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import samplerate_orchestration


class EventLedger:
    def __init__(self) -> None:
        self.events: list[str] = []

    def add(self, name: str) -> None:
        self.events.append(name)

    def assert_exact(self, testcase: unittest.TestCase, expected: list[str]) -> None:
        testcase.assertEqual(self.events, expected)


class SamplerateOrchestrationContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_playback_handoff_shared_reconcile(self):
        """Local activation reconciles via the shared handoff (48k target).

        The local wrapper only determines the target rate; the shared
        _complete_playback_handoff performs sink/force-rate, EE preset +
        EE port readback, helper sync and final verification.
        """
        ledger = EventLedger()
        status = {"active_rate": 44100, "force_rate": 44100}

        def samplerate_status() -> dict:
            ledger.add("local.read-rate")
            return dict(status)

        async def ensure_force(rate, reason, *, policy=None) -> bool:
            ledger.add(f"local.ensure-force:{rate}")
            status["active_rate"] = rate
            status["force_rate"] = rate
            return True

        async def preset_sync(**_kwargs) -> None:
            ledger.add("local.preset-sync")

        async def helper_sync(*_args, **_kwargs) -> None:
            ledger.add("local.helper-sync")

        async def sleep(_delay: float) -> None:
            ledger.add("local.settle")

        async def pw_link(*_args: str) -> str:
            return (
                "ee_soe_output_level:output_FL\n"
                "ee_soe_output_level:output_FR\n"
                "ee_soe_output_level:output_FL -> alsa_output.pci-0000_00_1f.3.analog-stereo:playback_FL\n"
                "ee_soe_output_level:output_FR -> alsa_output.pci-0000_00_1f.3.analog-stereo:playback_FR\n"
            )

        main.playback_transition_generation = 40
        with patch.object(main, "get_samplerate_status", samplerate_status), patch.object(
            main, "_ensure_playback_samplerate_force", ensure_force
        ), patch.object(main, "_sync_easyeffects_preset_for_playback_samplerate", preset_sync), patch.object(
            main, "_sync_subwoofer_runtime", helper_sync
        ), patch.object(
            main, "_get_current_pipewire_force_rate", lambda: 44100
        ), patch.object(main, "_run_pw_link_command", pw_link), patch.object(
            main, "get_audio_output_overview",
            return_value={"output_mode": {"mode": "stereo", "effective_output_key": "alsa_output.pci-0000_00_1f.3.analog-stereo"}},
        ), patch.object(main.asyncio, "sleep", sleep), patch.object(
            main, "subwoofer_runtime", object(),
        ):
            await main._complete_local_playback_handoff(
                {"source": "local", "url": "/music/a.flac"}, 48000,
                transition_generation=40,
            )

        ledger.assert_exact(
            self,
            [
                "local.read-rate",
                "local.ensure-force:48000",
                "local.preset-sync",
                "local.helper-sync",
                "local.read-rate",
            ],
        )

    async def test_radio_post_load_handoff_switches_once(self):
        """Radio post-load handoff reconciles exactly once to the live rate.

        The radio wrapper only determines the live rate; the shared
        _complete_playback_handoff performs sink/force-rate, EE preset + EE
        port readback, helper sync and final verification.
        """
        ledger = EventLedger()
        status = {"active_rate": 44100, "force_rate": 44100}

        def samplerate_status() -> dict:
            ledger.add("radio.read-rate")
            return dict(status)

        async def ensure_force(rate, reason, *, policy=None) -> bool:
            ledger.add(f"radio.ensure-force:{rate}")
            status["active_rate"] = rate
            status["force_rate"] = rate
            return True

        async def preset_sync(**_kwargs) -> None:
            ledger.add("radio.preset-sync")

        async def helper_sync(*_args, **_kwargs) -> None:
            ledger.add("radio.helper-sync")

        async def sleep(_delay: float) -> None:
            ledger.add("radio.settle")

        async def pw_link(*_args: str) -> str:
            return (
                "ee_soe_output_level:output_FL\n"
                "ee_soe_output_level:output_FR\n"
                "ee_soe_output_level:output_FL -> alsa_output.pci-0000_00_1f.3.analog-stereo:playback_FL\n"
                "ee_soe_output_level:output_FR -> alsa_output.pci-0000_00_1f.3.analog-stereo:playback_FR\n"
            )

        track = {"id": "radio_48k", "source": "radio", "url": "https://radio.example/48k"}
        with patch.object(main, "_get_player_audio_samplerate", return_value=48000), patch.object(
            main, "get_samplerate_status", samplerate_status
        ), patch.object(main, "_ensure_playback_samplerate_force", ensure_force), patch.object(
            main, "_sync_easyeffects_preset_for_playback_samplerate", preset_sync
        ), patch.object(main, "_sync_subwoofer_runtime", helper_sync), patch.object(
            main.asyncio, "sleep", sleep
        ), patch.object(main, "_get_current_pipewire_force_rate", lambda: 44100), patch.object(
            main, "_run_pw_link_command", pw_link
        ), patch.object(
            main, "get_audio_output_overview",
            return_value={"output_mode": {"mode": "stereo", "effective_output_key": "alsa_output.pci-0000_00_1f.3.analog-stereo"}},
        ), patch.object(main, "subwoofer_runtime", object()):
            main.playback_transition_generation = 40
            result = await main._complete_radio_handoff_after_load(
                track, 44100, transition_generation=40,
            )

        self.assertEqual(result, 48000)
        ledger.assert_exact(
            self,
            [
                "radio.read-rate",
                "radio.ensure-force:48000",
                "radio.preset-sync",
                "radio.helper-sync",
                "radio.read-rate",
            ],
        )
        self.assertEqual(samplerate_orchestration.RADIO_POLICY.initial_alignment_timeout_ms, 400)
        self.assertEqual(samplerate_orchestration.RADIO_POLICY.post_pulse_alignment_timeout_ms, 1200)

    async def test_spotify_entry_prearm_play_alignment(self):
        """Spotify entry pauses the local side, prearms, then plays and verifies."""
        ledger = EventLedger()

        async def pause_local() -> None:
            ledger.add("spotify.pause-local")

        async def prearm(reason: str) -> None:
            ledger.add("spotify.force-rate")

        async def sleep(_delay: float) -> None:
            ledger.add("spotify.settle")

        async def play() -> dict:
            ledger.add("spotify.play")
            return {"status": "Playing"}

        async def alignment(*, timeout_ms: int = 0) -> tuple[bool, int, int]:
            ledger.add("spotify.alignment")
            return True, 44100, 44100

        with patch.object(main, "pause_local_playback_for_spotify_broadcast", pause_local), patch.object(
            main, "_prearm_spotify_samplerate", prearm
        ), patch.object(main.asyncio, "sleep", sleep), patch.object(
            main, "spotify_play", play
        ), patch.object(
            main, "_wait_for_pipewire_spotify_samplerate_alignment", alignment
        ), patch.object(main, "spotify_samplerate_recovery_active", False):
            result = await main._complete_spotify_entry_handoff()

        self.assertEqual(result, {"status": "Playing"})
        ledger.assert_exact(
            self,
            [
                "spotify.pause-local",
                "spotify.force-rate",
                "spotify.settle",
                "spotify.play",
                "spotify.alignment",
            ],
        )

    async def test_measurement_session_open_is_neutral_and_release_is_direct(self):
        """A job, not the window heartbeat, starts 48 kHz; release restores directly."""
        ledger = EventLedger()
        original_session = main.measurement_sr_session
        original_capture = main._capture_playback_state_before_measurement
        original_status = main.get_samplerate_status
        original_set_force = main._set_pipewire_force_rate
        original_current_force = main._get_current_pipewire_force_rate
        original_sync = main._sync_subwoofer_runtime_at_rate
        original_playback_snapshot = main._playback_state_before_measurement
        original_radio_snapshot = main._radio_state_before_measurement
        original_stale = main.playback_stream_stale_after_measurement
        original_radio_stale = main.radio_stream_stale_after_measurement

        force_rate = 44100

        def capture() -> None:
            ledger.add("measurement.capture-playback")

        def status() -> dict:
            ledger.add("measurement.read-rate")
            return {"force_rate": force_rate, "active_rate": force_rate}

        def set_force(rate: int) -> None:
            nonlocal force_rate
            force_rate = rate
            ledger.add(f"measurement.force-rate:{rate}")

        def current_force() -> int:
            ledger.add("measurement.read-force")
            return force_rate

        async def sync(rate: int, *, _rate_lock_held: bool = False) -> None:
            ledger.add(f"measurement.helper:{rate}")

        try:
            main.measurement_sr_session = main.MeasurementSampleRateSession()
            main._capture_playback_state_before_measurement = capture
            main.get_samplerate_status = status
            main._set_pipewire_force_rate = set_force
            main._get_current_pipewire_force_rate = current_force
            main._sync_subwoofer_runtime_at_rate = sync
            main._playback_state_before_measurement = None
            main._radio_state_before_measurement = None
            main.playback_stream_stale_after_measurement = False
            main.radio_stream_stale_after_measurement = False

            session = main.measurement_sr_session
            await session.request_open()  # rate-neutral while no session is active
            self.assertEqual(ledger.events, [])
            await session.register_manual_job("manual-1")
            await session.request_close()
            await session.unregister_manual_job("manual-1")
        finally:
            main.measurement_sr_session = original_session
            main._capture_playback_state_before_measurement = original_capture
            main.get_samplerate_status = original_status
            main._set_pipewire_force_rate = original_set_force
            main._get_current_pipewire_force_rate = original_current_force
            main._sync_subwoofer_runtime_at_rate = original_sync
            main._playback_state_before_measurement = original_playback_snapshot
            main._radio_state_before_measurement = original_radio_snapshot
            main.playback_stream_stale_after_measurement = original_stale
            main.radio_stream_stale_after_measurement = original_radio_stale

        ledger.assert_exact(
            self,
            [
                "measurement.capture-playback",
                "measurement.read-rate",
                "measurement.force-rate:48000",
                "measurement.read-force",
                "measurement.force-rate:44100",
                "measurement.helper:44100",
            ],
        )

    async def test_resume_helper_sync_is_generation_bound_after_settle(self):
        """SR-001 resume sync settles, resolves rate, aligns, then touches helper."""
        ledger = EventLedger()
        track = {"source": "local", "url": "/music/a.flac", "sample_rate_hz": 44100}
        original_generation = main.playback_transition_generation
        original_runtime = main.subwoofer_runtime
        original_marker_url = main.local_playback_handoff_completed_url
        original_marker_rate = main.local_playback_handoff_completed_rate

        class Runtime:
            def snapshot(self) -> dict:
                return {"active": False, "config": {"sample_rate": 48000}}

        async def settled(url: str | None, *, timeout_ms: int) -> bool:
            ledger.add("resume.player-settled")
            return True

        async def sleep(_delay: float) -> None:
            ledger.add("resume.settle")

        async def resolve(source: str, prefer_live_radio_rate: bool = False) -> int:
            ledger.add("resume.resolve-rate")
            return 44100

        async def alignment(rate: int, *, timeout_ms: int) -> bool:
            ledger.add("resume.alignment")
            return True

        def overview() -> dict:
            ledger.add("resume.read-output-mode")
            return {"output_mode": {"mode": "subwoofer-2.1"}}

        async def sync(*args, **kwargs) -> None:
            ledger.add("resume.helper")

        try:
            main.playback_transition_generation = 8
            main.subwoofer_runtime = Runtime()
            main.local_playback_handoff_completed_url = "/music/other.flac"
            main.local_playback_handoff_completed_rate = 44100
            with patch.object(main, "_wait_for_player_current_file", settled), patch.object(
                main.asyncio, "sleep", sleep
            ), patch.object(main, "_resolve_expected_playback_samplerate", resolve), patch.object(
                main, "_wait_for_samplerate_alignment", alignment
            ), patch.object(main, "get_audio_output_overview", overview), patch.object(
                main, "_sync_subwoofer_runtime", sync
            ), patch.object(main, "_current_track_matches", return_value=True), patch.object(
                main, "_playback_transition_context_is_current", return_value=True
            ):
                await main._sync_subwoofer_runtime_after_playback_transition(
                    track, transition_generation=8
                )
        finally:
            main.playback_transition_generation = original_generation
            main.subwoofer_runtime = original_runtime
            main.local_playback_handoff_completed_url = original_marker_url
            main.local_playback_handoff_completed_rate = original_marker_rate

        ledger.assert_exact(
            self,
            [
                "resume.player-settled",
                "resume.settle",
                "resume.resolve-rate",
                "resume.alignment",
                "resume.read-output-mode",
                "resume.helper",
            ],
        )

    async def test_status_repair_wrapper_logs_post_attempt_active_rate(self):
        statuses = iter((
            {"active_rate": 48000, "force_rate": 0},
            {"active_rate": 44100, "force_rate": 44100},
        ))

        def read_status():
            return next(statuses)

        async def wait(_rate: int, *, timeout_ms: int) -> bool:
            return False

        async def pulse(*, reason: str, force: bool) -> bool:
            return False

        with patch.object(main, "get_samplerate_status", side_effect=read_status), patch.object(
            main, "_set_pipewire_force_rate"
        ), patch.object(main, "_wait_for_samplerate_alignment", wait), patch.object(
            main, "_suspend_resume_playback_sink", pulse
        ), self.assertLogs("main", level="WARNING") as captured:
            result = await main._ensure_playback_samplerate_force(
                44100,
                "status-drift-repair:radio",
                policy=samplerate_orchestration.STATUS_DRIFT_REPAIR_POLICY,
            )

        self.assertFalse(result)
        self.assertTrue(any("active_rate=44100" in line for line in captured.output))

    async def test_status_repair_force_pulse_alignment_then_helper(self):
        """SR-001 status repair never syncs the helper before sink alignment."""
        ledger = EventLedger()

        rate_status = {"active_rate": 48000, "force_rate": 0}

        def read_status() -> dict:
            ledger.add("repair.read-rate")
            return dict(rate_status)

        def write_force(rate: int) -> None:
            rate_status["force_rate"] = rate
            ledger.add(f"repair.force-rate:{rate}")

        async def pulse(*, reason: str, force: bool) -> bool:
            ledger.add("repair.sink-pulse")
            return True

        async def alignment(rate: int, *, timeout_ms: int) -> bool:
            ledger.add(f"repair.alignment:{timeout_ms}")
            return timeout_ms == 1500

        async def sync(*args, **kwargs) -> None:
            ledger.add("repair.helper")

        original_runtime = main.subwoofer_runtime
        main.subwoofer_runtime = object()
        try:
            with patch.object(main, "get_samplerate_status", read_status), patch.object(
                main, "_set_pipewire_force_rate", write_force
            ), patch.object(main, "_suspend_resume_playback_sink", pulse), patch.object(
                main, "_wait_for_samplerate_alignment", alignment
            ), patch.object(main, "_sync_subwoofer_runtime", sync), patch.object(
                main, "_playback_transition_context_is_current", return_value=True
            ), patch.object(main, "_current_track_matches", return_value=True):
                await main._repair_active_app_samplerate_drift_locked(44100, "radio", 10)
        finally:
            main.subwoofer_runtime = original_runtime

        ledger.assert_exact(
            self,
            [
                "repair.read-rate",
                "repair.force-rate:44100",
                "repair.alignment:400",
                "repair.sink-pulse",
                "repair.alignment:1500",
                "repair.helper",
            ],
        )


if __name__ == "__main__":
    unittest.main()
