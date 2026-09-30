#!/usr/bin/env python3
"""STDIN source selection persists without hardware and stays readable."""

import asyncio
from dataclasses import replace
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audio.samplerate as samplerate
import audio.samplerate.overview as overview_mod
from dsp.peak_monitor import DSPPeakMonitor, MonitorTarget
import main


def _empty_commands(args):
    if args[:2] == ["pactl", "info"]:
        return "Server Name: PulseAudio\n"
    if args == ["pactl", "list", "sources", "short"]:
        return ""
    if args == ["pactl", "list", "sources"]:
        return ""
    raise AssertionError(f"unexpected command: {args}")


def _bluetooth_stub():
    return {"roles": {}, "receiver_session": {}}


class StdinSourceSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.dict("os.environ", {"XDG_CONFIG_HOME": self.temp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        overview_mod.configure_stdin_input_status(lambda: {
            "available": True, "selectable": True, "state": "waiting",
            "selected": False, "session_id": None, "format": None,
            "rate": None, "channels": None, "left": None, "right": None,
            "routed": False, "frames_received": 0, "error": None,
            "measurement_active": False,
        })
        self.addCleanup(lambda: overview_mod.configure_stdin_input_status(None))

    def _overview(self):
        with patch.object(overview_mod, "_run_command", side_effect=_empty_commands), patch.object(
            overview_mod, "get_bluetooth_audio_overview", return_value=_bluetooth_stub()
        ):
            return overview_mod.get_audio_source_overview()

    def test_stdin_selectable_without_hardware_inputs(self):
        with patch.object(overview_mod, "_run_command", side_effect=_empty_commands), patch.object(
            overview_mod, "get_bluetooth_audio_overview", return_value=_bluetooth_stub()
        ):
            result = overview_mod.set_audio_source_selection("stdin-input")
        self.assertEqual(result["mode"], "stdin-input")
        self.assertIn({"key": "stdin-input", "label": "STDIN", "selectable": True}, result["modes"])
        self.assertEqual(result["stdin"]["state"], "waiting")
        self.assertEqual(samplerate._load_audio_source_selection()["mode"], "stdin-input")
        self.assertEqual(result["inputs"], [])

    def test_unavailable_stdin_rejects_new_selection(self):
        overview_mod.configure_stdin_input_status(lambda: {
            "available": False, "selectable": False, "state": "unavailable",
            "selected": False, "session_id": None, "format": None,
            "rate": None, "channels": None, "left": None, "right": None,
            "routed": False, "frames_received": 0, "error": None,
            "measurement_active": False,
        })
        with patch.object(overview_mod, "_run_command", side_effect=_empty_commands), patch.object(
            overview_mod, "get_bluetooth_audio_overview", return_value=_bluetooth_stub()
        ):
            with self.assertRaises(ValueError):
                overview_mod.set_audio_source_selection("stdin-input")


def _stdin_snapshot(**overrides):
    snapshot = {
        "available": True, "selectable": True, "state": "waiting",
        "selected": False, "session_id": None, "format": None,
        "rate": None, "channels": None, "left": None, "right": None,
        "routed": False, "frames_received": 0, "error": None,
        "measurement_active": False,
    }
    snapshot.update(overrides)
    return snapshot


class StdinPeakMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from scripts.test_peak_monitor_line_source_arming import make_coordinator
        from scripts.test_peak_monitor_stereo import _FakeProc, _stereo_chunk

        self.coordinator, _ = make_coordinator()
        self.monitor = DSPPeakMonitor()
        self.coordinator._deps = replace(
            self.coordinator._deps, get_peak_monitor=lambda: self.monitor)

        async def spawn(*args, **kwargs):
            proc = _FakeProc()
            proc.stdout.feed_data(_stereo_chunk(0.0, 0.0))
            return proc

        # Keep the lifecycle, PCM reader and snapshot real; stub PipeWire I/O.
        for patcher in (
            patch.object(self.monitor, "_discover_target", AsyncMock(
                return_value=MonitorTarget("fxroute_dsp", 42, "Output Level"))),
            patch.object(self.monitor, "_link_capture_stream", AsyncMock()),
            patch("dsp.peak_monitor.asyncio.create_subprocess_exec", side_effect=spawn),
            patch("dsp.peak_monitor._resolve_capture_rate", return_value=48000),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addAsyncCleanup(self.monitor.stop)

    async def _assert_fresh_silence(self):
        self.assertTrue(self.coordinator.armed)
        async with asyncio.timeout(1):
            while not self.monitor.snapshot()["vu_fresh"]:
                await asyncio.sleep(0.001)
        snapshot = self.monitor.snapshot()
        self.assertTrue(snapshot["available"])
        self.assertTrue(snapshot["vu_fresh"])
        self.assertEqual(snapshot["vu_db"], -60.0)

    async def test_fresh_selected_waiting_stdin_has_real_silence_vu(self):
        overview = {"mode": "stdin-input", "stdin": _stdin_snapshot(selected=True)}
        await self.coordinator.sync_stdin_state(overview)
        await self._assert_fresh_silence()
        self.assertFalse(overview["stdin"]["routed"])

    async def test_writer_eof_restores_fresh_waiting_vu_for_each_session(self):
        for session_id in ("abc", "def"):
            overview = {"mode": "stdin-input", "stdin": _stdin_snapshot(
                selected=True, session_id=session_id, state="streaming", routed=True)}
            await self.coordinator.sync_stdin_state(overview)
            await self._assert_fresh_silence()
            for state in ("draining", "waiting"):
                overview["stdin"] = _stdin_snapshot(
                    selected=True, state=state, routed=state == "draining",
                    session_id=session_id if state == "draining" else None)
                await self.coordinator.sync_stdin_state(overview)
                await self._assert_fresh_silence()
            self.assertFalse(overview["stdin"]["routed"])

    async def test_waiting_resync_keeps_the_same_capture(self):
        overview = {"mode": "stdin-input", "stdin": _stdin_snapshot(selected=True)}
        await self.coordinator.sync_stdin_state(overview)
        await self._assert_fresh_silence()
        task = self.monitor._task
        for _ in range(3):
            await self.coordinator.sync_stdin_state(overview)
        self.assertIs(self.monitor._task, task)
        self.assertTrue(self.monitor.snapshot()["vu_fresh"])

    async def test_leaving_waiting_stdin_releases_capture(self):
        overview = {"mode": "stdin-input", "stdin": _stdin_snapshot(selected=True)}
        await self.coordinator.sync_stdin_state(overview)
        await self._assert_fresh_silence()
        await self.coordinator.sync_stdin_state({"mode": "app-playback"})
        self.assertFalse(self.coordinator.armed)
        self.assertFalse(self.monitor.snapshot()["available"])
        self.assertIsNone(self.monitor._task)

    async def test_unavailable_or_measurement_owned_waiting_stdin_releases_capture(self):
        for overrides in ({"available": False}, {"measurement_active": True}, {"state": "error"}):
            with self.subTest(overrides=overrides):
                await self.coordinator.sync_stdin_state({
                    "mode": "stdin-input", "stdin": _stdin_snapshot(
                        selected=True, state="streaming", routed=True, session_id="abc")})
                await self._assert_fresh_silence()
                await self.coordinator.sync_stdin_state({
                    "mode": "stdin-input", "stdin": _stdin_snapshot(selected=True, **overrides)})
                self.assertFalse(self.coordinator.armed)
                self.assertFalse(self.monitor.snapshot()["available"])

    async def test_unselected_waiting_stdin_does_not_arm_capture(self):
        await self.coordinator.sync_stdin_state({
            "mode": "app-playback", "stdin": _stdin_snapshot()})
        self.assertFalse(self.coordinator.armed)
        self.assertFalse(self.monitor.snapshot()["available"])


class StdinPowerTests(unittest.TestCase):
    def test_streaming_stdin_switches_amp_on(self):
        stdin = SimpleNamespace(
            snapshot=lambda: _stdin_snapshot(
                selected=True, routed=True, state="streaming"),
            active_for_power=AsyncMock(return_value=True),
        )
        with patch.object(main.runtime, "player_instance", None), patch.object(
            main.playback_state, "latest_spotify_state", {}
        ), patch.object(main.playback_state, "latest_qobuz_state", {}), patch.object(
            main, "last_measurement_window_seen_at", 0.0
        ), patch.object(main, "bluetooth_input", SimpleNamespace(input_source_name=None)), patch.object(
            main, "external_input", SimpleNamespace(loopback_source_name=None)
        ), patch.object(main.runtime, "stdin_input", stdin):
            payload = main._build_power_state_payload()
        self.assertTrue(payload["amp_should_be_on"])
        self.assertEqual(payload["reason"], "stdin")

    def test_waiting_stdin_stays_idle(self):
        stdin = SimpleNamespace(
            snapshot=lambda: _stdin_snapshot(
                selected=True, routed=False, state="waiting"),
            active_for_power=AsyncMock(return_value=False),
        )
        with patch.object(main.runtime, "player_instance", None), patch.object(
            main.playback_state, "latest_spotify_state", {}
        ), patch.object(main.playback_state, "latest_qobuz_state", {}), patch.object(
            main, "last_measurement_window_seen_at", 0.0
        ), patch.object(main, "bluetooth_input", SimpleNamespace(input_source_name=None)), patch.object(
            main, "external_input", SimpleNamespace(loopback_source_name=None)
        ), patch.object(main.runtime, "stdin_input", stdin):
            payload = main._build_power_state_payload()
        self.assertEqual(payload["reason"], "idle")
        self.assertFalse(payload["amp_should_be_on"])


class StdinPowerAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_power_requires_live_readback(self):
        stdin = SimpleNamespace(
            snapshot=lambda: _stdin_snapshot(
                selected=True, routed=True, state="streaming"),
            active_for_power=AsyncMock(return_value=True),
        )
        with patch.object(main.runtime, "player_instance", None), patch.object(
            main.playback_state, "latest_spotify_state", {}
        ), patch.object(main.playback_state, "latest_qobuz_state", {}), patch.object(
            main, "last_measurement_window_seen_at", 0.0
        ), patch.object(main, "bluetooth_input", SimpleNamespace(input_source_name=None)), patch.object(
            main, "external_input", SimpleNamespace(loopback_source_name=None)
        ), patch.object(main.runtime, "stdin_input", stdin):
            payload = await main._build_power_state_payload_async()
        self.assertEqual(payload["reason"], "stdin")


class StdinTransitionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._playback_snapshot = dict(main.playback_state.__dict__)

    def tearDown(self):
        main.playback_state.__dict__.clear()
        main.playback_state.__dict__.update(self._playback_snapshot)

    async def test_entering_stdin_pauses_players_and_activates_service(self):
        calls = []
        stdin = SimpleNamespace(
            set_selected=AsyncMock(side_effect=lambda selected: calls.append(("stdin", selected))),
        )
        overview = {"mode": "stdin-input", "selected_input": None,
                    "stdin": _stdin_snapshot(selectable=True)}
        with patch.object(main.samplerate, "_load_audio_source_selection",
                           return_value={"mode": "app-playback", "selected_input_key": None}), patch.object(
            main, "set_audio_source_selection", return_value=dict(overview)
        ), patch.object(main.external_input, "sync", AsyncMock(side_effect=lambda result: result)), patch.object(
            main.bluetooth_input, "sync", AsyncMock(side_effect=lambda result: result)
        ), patch.object(main, "_pause_all_app_playback_for_external_input",
                         AsyncMock(side_effect=lambda: calls.append(("pause", True)))), patch.object(
            main.peak_monitor_coordinator, "sync_source_mode_state",
            AsyncMock(side_effect=lambda result: calls.append(("peak", True)))), patch.object(
            main, "get_audio_source_overview", return_value=dict(overview)
        ), patch.object(main.runtime, "stdin_input", stdin):
            before = main.playback_state.source_generation
            result = await main.source_transitions.apply_selection("stdin-input", None)
        self.assertEqual(result["mode"], "stdin-input")
        self.assertEqual(main.playback_state.source_generation, before + 1)
        self.assertEqual(calls, [("pause", True), ("stdin", True), ("peak", True)])

    async def test_leaving_stdin_stops_service_before_other_routing(self):
        calls = []
        stdin = SimpleNamespace(
            set_selected=AsyncMock(side_effect=lambda selected: calls.append(("stdin", selected))),
        )
        overview = {"mode": "app-playback", "selected_input": None,
                    "stdin": _stdin_snapshot(selectable=True)}
        with patch.object(main.samplerate, "_load_audio_source_selection",
                           return_value={"mode": "stdin-input", "selected_input_key": None}), patch.object(
            main, "set_audio_source_selection", return_value=dict(overview)
        ), patch.object(main.external_input, "sync", AsyncMock(side_effect=lambda result: result)), patch.object(
            main.bluetooth_input, "sync", AsyncMock(side_effect=lambda result: result)
        ), patch.object(main.peak_monitor_coordinator, "sync_source_mode_state",
                         AsyncMock(return_value=None)), patch.object(
            main.runtime, "stdin_input", stdin):
            await main.source_transitions.apply_selection("app-playback", None)
        self.assertEqual(calls, [("stdin", False)])

    async def test_failed_activation_restores_previous_and_republishes_generation(self):
        stdin = SimpleNamespace(set_selected=AsyncMock(
            side_effect=[RuntimeError("boom"), None]))
        restored = {"mode": "app-playback", "selected_input": None,
                    "stdin": _stdin_snapshot(selectable=True)}
        with patch.object(main.samplerate, "_load_audio_source_selection",
                           return_value={"mode": "app-playback", "selected_input_key": None}), patch.object(
            main, "set_audio_source_selection", side_effect=[{"mode": "stdin-input", "selected_input": None}, restored]
        ), patch.object(main.external_input, "sync", AsyncMock(side_effect=lambda result: result)), patch.object(
            main.bluetooth_input, "sync", AsyncMock(side_effect=lambda result: result)
        ), patch.object(main, "_pause_all_app_playback_for_external_input",
                         AsyncMock(return_value=None)), patch.object(
            main.runtime, "stdin_input", stdin):
            before = main.playback_state.source_generation
            with self.assertRaises(RuntimeError):
                await main.source_transitions.apply_selection("stdin-input", None)
            # Entering stdin bumped once, rollback republished the restored mode.
            self.assertEqual(main.playback_state.source_generation, before + 2)
            self.assertEqual(main.playback_state.source_mode, "app-playback")


class StdinMeasurementWiringTests(unittest.TestCase):
    def test_measurement_services_bind_stdin_callbacks(self):
        stdin = SimpleNamespace(
            set_measurement_active=AsyncMock(),
        )
        with patch.object(main.runtime, "stdin_input", stdin):
            services = main._make_measurement_services()
        self.assertTrue(callable(services.stdin_measurement_acquire))
        self.assertTrue(callable(services.stdin_measurement_release))


if __name__ == "__main__":
    unittest.main()
