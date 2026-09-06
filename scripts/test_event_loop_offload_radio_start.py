#!/usr/bin/env python3
"""Regression test: heavy PipeWire/BlueZ builders must stay off the event loop.

The .125 radio start delay (~20 s per play request) was caused by synchronous
``get_samplerate_status`` / ``get_audio_output_overview`` /
``get_audio_source_overview`` subprocess pipelines running directly on the
asyncio main thread: the periodic link watcher rebuilt the full output
overview every 2 s, the Bluetooth input monitor every 3 s, and each
transition snapshot / gate-mute readback re-ran the status pipeline inline.
Every await in a 17-stage playback transition then queued behind those
blocking windows.

These tests pin the fix: wherever production code awaits these builders they
must execute inside a worker thread, never on the thread running the event
loop.
"""

import asyncio
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import audio.bluetooth as bluetooth_module
import main
from audio.bluetooth import BluetoothInputMonitor
from dsp.orchestration import DspOrchestrationDeps, DspOrchestrator
from playback_transition_test_support import make_transition_runtime
from playback.runtime.mute import _RuntimeMuteMixin
from playback.runtime.snapshot import _RuntimeSnapshotMixin
from playback.transition import TransitionRequest


MAIN_THREAD = threading.current_thread()


def _assert_off_loop(self, fn):
    """Wrap fn so the test fails if it runs on the event-loop thread."""

    def probe(*args, **kwargs):
        self.assertNotEqual(
            threading.current_thread(),
            MAIN_THREAD,
            "heavy audio builder ran on the event-loop thread",
        )
        self.assertIsInstance(
            threading.current_thread().name,
            str,
        )
        return {"output_mode": {}, "notes": []}

    return probe


class EventLoopOffloadTest(unittest.IsolatedAsyncioTestCase):
    async def test_pause_route_runs_mpv_command_off_loop(self):
        seen = []

        class Player:
            _running = True
            state = {
                "current_file": "/music/current.flac",
                "playing": True,
                "paused": False,
                "ended": False,
            }

            def pause(self):
                seen.append(threading.current_thread() is MAIN_THREAD)
                self.state = {**self.state, "playing": False, "paused": True}

        with patch.object(main.runtime, "player_instance", Player()), patch.object(
            main, "_mark_player_state_authoritative"
        ), patch.object(main, "_mark_playback_intent_changed"):
            await main.pause_playback()

        self.assertEqual(seen, [False])

    async def test_runtime_player_command_runs_off_loop(self):
        seen = []

        class Player:
            _running = True
            state = {"paused": False, "playing": True, "current_file": "/music/current.flac"}

            def set_pause(self, paused):
                seen.append(threading.current_thread() is MAIN_THREAD)
                self.state = {**self.state, "paused": paused, "playing": not paused}

        request = TransitionRequest(
            operation="output-mode-switch",
            source="local",
            target_rate=44100,
            should_play=False,
            reload_source=False,
        )
        with patch.object(main.runtime, "player_instance", Player()):
            await make_transition_runtime().prepare_target_source(request)

        self.assertEqual(seen, [False])

    async def test_audio_samplerate_poll_runs_mpv_property_read_off_loop(self):
        seen = []

        class Player:
            _running = True
            state = {"current_file": "/music/current.flac"}

            def get_property(self, name):
                seen.append(threading.current_thread() is MAIN_THREAD)
                self.assertEqual(name, "audio-params")
                return {"samplerate": 48000}

        player = Player()
        player.assertEqual = self.assertEqual
        with patch.object(main.runtime, "player_instance", player):
            rate = await main._wait_for_player_audio_samplerate(
                expected_url="/music/current.flac"
            )

        self.assertEqual(rate, 48000)
        self.assertEqual(seen, [False])

    async def test_runtime_link_watch_loop_builds_overview_off_loop(self):
        seen = []

        def overview_builder():
            seen.append(threading.current_thread() is MAIN_THREAD)
            # Stereo mode: the loop must stop after the mode check.
            return {"output_mode": {"mode": "stereo"}}

        ticks = 0

        async def one_tick_then_cancel(_delay):
            # First tick proceeds to the overview build; second tick ends the loop.
            nonlocal ticks
            ticks += 1
            if ticks < 2:
                return
            raise asyncio.CancelledError

        deps = SimpleNamespace(
            sleep=one_tick_then_cancel,
            measurement_audio_graph_owned=lambda: False,
            observe_playback_samplerate_drift=unittest.mock.AsyncMock(),
            get_dsp_runtime=lambda: object(),
            get_audio_output_overview=overview_builder,
            playback_transition_is_active=lambda: True,
            sync_peak_monitor_for_source_mode_state=None,
        )
        orchestrator = DspOrchestrator(_watcher_deps(deps))
        task = asyncio.create_task(orchestrator.runtime_link_watch_loop())
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(seen, "watcher tick did not build the overview")
        self.assertFalse(
            seen[0],
            "runtime_link_watch_loop built the audio overview on the event loop",
        )

    async def test_bluetooth_monitor_loop_builds_overview_off_loop(self):
        seen = []
        monitor = BluetoothInputMonitor(
            SimpleNamespace(sync_peak_monitor_for_source_mode_state=None)
        )

        async def one_sleep(_delay):
            raise asyncio.CancelledError

        def overview_builder():
            seen.append(threading.current_thread() is MAIN_THREAD)
            return {"mode": "app_playback", "bluetooth": {}}

        with patch.object(
            bluetooth_module, "get_audio_source_overview", overview_builder
        ), patch.object(bluetooth_module.asyncio, "sleep", one_sleep):
            task = asyncio.create_task(monitor.run_monitor_loop())
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(seen, "bluetooth monitor tick did not build the overview")
        self.assertFalse(
            seen[0],
            "run_monitor_loop built the source overview on the event loop",
        )

    async def test_bluetooth_monitor_skips_overview_when_source_mode_is_app_playback(self):
        seen = []
        monitor = BluetoothInputMonitor(
            SimpleNamespace(
                sync_peak_monitor_for_source_mode_state=None,
                get_persisted_source_mode=lambda: "app-playback",
            )
        )
        sleeps = 0

        def overview_builder():
            seen.append(True)
            return {"mode": "app_playback", "bluetooth": {}}

        async def two_sleeps_then_cancel(_delay):
            nonlocal sleeps
            sleeps += 1
            if sleeps >= 2:
                raise asyncio.CancelledError

        with patch.object(
            bluetooth_module, "get_audio_source_overview", overview_builder
        ), patch.object(bluetooth_module.asyncio, "sleep", two_sleeps_then_cancel):
            task = asyncio.create_task(monitor.run_monitor_loop())
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(seen, [], "app-playback ticks must not build the source overview")

    async def test_bluetooth_monitor_without_mode_provider_keeps_legacy_overview(self):
        seen = []
        monitor = BluetoothInputMonitor(
            SimpleNamespace(sync_peak_monitor_for_source_mode_state=None)
        )

        async def one_sleep(_delay):
            raise asyncio.CancelledError

        def overview_builder():
            seen.append(True)
            return {"mode": "app_playback", "bluetooth": {}}

        with patch.object(
            bluetooth_module, "get_audio_source_overview", overview_builder
        ), patch.object(bluetooth_module.asyncio, "sleep", one_sleep):
            task = asyncio.create_task(monitor.run_monitor_loop())
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(
            seen, [True], "without a mode provider the legacy tick must build the overview"
        )

    async def test_read_transition_snapshot_builds_status_and_overview_off_loop(self):
        seen = []

        class _Deps:
            @staticmethod
            def get_samplerate_status():
                seen.append(("status", threading.current_thread() is MAIN_THREAD))
                return {"active_rate": 44100, "force_rate": 44100}

            @staticmethod
            def get_audio_output_overview(status=None):
                seen.append(("overview", threading.current_thread() is MAIN_THREAD))
                assert status is not None, "snapshot must pass its status for reuse"
                return {"output_mode": {"mode": "stereo"}}

            @staticmethod
            def get_current_track_info():
                return {}

            @staticmethod
            def get_playback_intent_generation():
                return 0

        class _Adapter(_RuntimeSnapshotMixin):
            def __init__(self):
                self._deps = _Deps()
                self._player = None
                self._staged_target_url = None

        request = SimpleNamespace(
            operation="play", source="radio", target_url="http://x", audio_overview=None
        )
        await _Adapter().read_transition_snapshot(request)
        kinds = {kind for kind, _ in seen}
        self.assertEqual(kinds, {"status", "overview"})
        self.assertFalse(
            any(on_loop for _, on_loop in seen),
            "read_transition_snapshot ran the PipeWire/BlueZ pipelines on the event loop",
        )

    async def test_read_hardware_mute_resolves_sink_off_loop(self):
        seen = []

        class _Deps:
            @staticmethod
            def get_samplerate_status():
                seen.append(threading.current_thread() is MAIN_THREAD)
                return {
                    "relevant_sink": {"name": "alsa_output.test"},
                    "active_rate": 44100,
                    "force_rate": 0,
                }

            @staticmethod
            def get_audio_output_overview():
                seen.append(threading.current_thread() is MAIN_THREAD)
                return {"output_mode": {}}

        class _Adapter(_RuntimeMuteMixin):
            def __init__(self):
                self._deps = _Deps()
                self._output_key = None

        with patch(
            "playback.runtime.mute._read_hardware_sink_mute",
            lambda output_key: False,
        ):
            muted = await _Adapter().read_hardware_mute()
        self.assertFalse(muted)
        self.assertTrue(seen, "mute readback did not resolve the hardware sink")
        self.assertFalse(
            any(seen),
            "_hardware_sink_for_transition resolved the gate sink on the event loop",
        )


def _watcher_deps(overrides) -> DspOrchestrationDeps:
    """Minimal DspOrchestrationDeps; unused members point at harmless stubs."""
    stub = lambda *a, **k: None  # noqa: E731
    async def _none():
        return None
    return DspOrchestrationDeps(
        get_dsp_runtime=overrides.get_dsp_runtime,
        get_dsp_manager=lambda: None,
        get_audio_output_overview=overrides.get_audio_output_overview,
        get_samplerate_status=lambda: {},
        get_measurement_sr_session=lambda: None,
        get_player_instance=lambda: None,
        get_current_track_info=lambda: {},
        get_peak_monitor=lambda: None,
        peak_monitor_playback_armed=lambda: False,
        set_peak_monitor_context_signature=stub,
        get_spotify_ui_state=_none,
        get_qobuz_ui_state=_none,
        sync_peak_monitor_for_playback_state=stub,
        sync_peak_monitor_for_spotify_state=stub,
        sync_peak_monitor_for_qobuz_state=stub,
        load_dsp_preset=stub,
        broadcast=stub,
        wait_for_samplerate_alignment=stub,
        wait_for_selected_output_effective_rate=stub,
        measurement_audio_graph_owned=lambda: False,
        observe_playback_samplerate_drift=overrides.observe_playback_samplerate_drift,
        playback_transition_is_active=lambda: True,
        coordinator_target_rate=lambda *_a, **_k: 44100,
        playback_graph_diagnosis=stub,
        request_coordinated_recovery=stub,
        create_lifecycle_background_task=stub,
        peak_monitor_restart_settle_ms=320.0,
        sleep=overrides.sleep,
    )


if __name__ == "__main__":
    unittest.main()
