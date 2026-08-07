#!/usr/bin/env python3
"""Small regressions for peak, MPV and Coordinator-owned queue callbacks."""

import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import player
from peak_monitor import EasyEffectsPeakMonitor, MonitorTarget


class _FakeManager:
    async def broadcast(self, _message):
        return None


class _FakeTrack:
    def __init__(self, track_id):
        self.id = track_id

    def to_dict(self):
        return {
            "id": self.id,
            "source": "local",
            "url": f"/music/{self.id}.flac",
            "title": self.id,
            "artist": "Test",
            "sample_rate_hz": 44100,
        }


class PeakMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_relink_clears_previous_error(self):
        monitor = EasyEffectsPeakMonitor()
        monitor._running = True
        monitor._proc = SimpleNamespace(returncode=None)
        monitor._target = MonitorTarget("ee_soe_output_level", 1, "Output")
        monitor._capture_node_name = "fxroute_peak_capture"
        monitor._last_error = "stale link error"

        async def link_ok(_target, _capture):
            return None

        monitor._link_capture_stream = link_ok
        self.assertTrue(await monitor.relink())
        self.assertIsNone(monitor._last_error)

    def test_initial_link_failure_has_initialized_no_data_clock(self):
        source = (Path(__file__).resolve().parents[1] / "peak_monitor.py").read_text()
        spawn_end = source.index("assert self._proc.stdout is not None")
        loop_start = source.index("try:\n            try:", spawn_end)
        clock = source.index("last_data_at = time.monotonic()", spawn_end)
        self.assertLess(clock, loop_start)


class MPVCommandTests(unittest.TestCase):
    def test_empty_and_error_responses_are_failures(self):
        class FakeSocket:
            response = b""

            def connect(self, _path):
                return None

            def settimeout(self, _timeout):
                return None

            def sendall(self, raw):
                if b'"request_id"' in raw and b'"error"' in self.response:
                    request = json.loads(raw.decode())
                    payload = json.loads(self.response.decode())
                    payload["request_id"] = request["request_id"]
                    self.response = (json.dumps(payload) + "\n").encode()

            def recv(self, _size):
                response, self.response = self.response, b""
                return response

            def close(self):
                return None

        wrapper = player.MPVWrapper()
        wrapper._running = True
        empty = FakeSocket()
        with patch("player.socket.socket", return_value=empty):
            with self.assertRaises(player.MPVError):
                wrapper._send_command("set_property", "pause", True)

        error = FakeSocket()
        error.response = b'{"error":"command-error"}'
        with patch("player.socket.socket", return_value=error):
            with self.assertRaises(player.MPVError):
                wrapper._send_command("set_property", "pause", True)

    def test_failed_load_and_position_commands_do_not_mutate_state(self):
        wrapper = player.MPVWrapper()
        wrapper._running = True
        original = dict(wrapper._state)

        def fail(*_args, **_kwargs):
            raise player.MPVError("command failed")

        wrapper._send_command = fail
        with self.assertRaises(player.MPVError):
            wrapper.loadfile("/music/next.flac")
        self.assertEqual(wrapper._state, original)

    def test_send_command_reads_past_interleaved_mpv_events(self):
        # mpv can emit an event (e.g. start-file) on the command connection
        # before the command's response; the reader must keep consuming
        # until the matching request_id arrives (queue-jump path).
        class EventThenResponseSocket:
            def __init__(self):
                self.queue = []

            def connect(self, _path):
                return None

            def settimeout(self, _timeout):
                return None

            def sendall(self, raw):
                request = json.loads(raw.decode())
                self.queue = [
                    b'{"event": "start-file", "playlist_entry_id": 1}\n',
                    (json.dumps({"request_id": request["request_id"], "error": "success"}) + "\n").encode(),
                ]

            def recv(self, _size):
                return self.queue.pop(0) if self.queue else b""

            def close(self):
                return None

        wrapper = player.MPVWrapper()
        wrapper._running = True
        fake = EventThenResponseSocket()
        with patch("player.socket.socket", return_value=fake):
            result = wrapper._send_command("loadfile", "/music/a.flac", "replace")
        self.assertEqual(result["error"], "success")


class QueueCallbackOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_callback_does_not_commit_queue_track_from_playlist_events(self):
        names = (
            "playback_queue", "playback_queue_mode", "playback_queue_index",
            "queue_transition_target_url", "current_track_info", "last_track_info",
            "queue_advancing", "single_track_loop", "playback_transition_generation",
            "latest_player_state_seq_seen", "source_transition_lock", "manager",
            "peak_monitor", "build_playback_payload", "_schedule_radio_reconnect_if_needed",
            "sync_peak_monitor_for_playback_state",
        )
        originals = {name: getattr(main, name) for name in names}
        try:
            main.playback_queue = [
                {"id": "a", "source": "local", "url": "/music/a.flac"},
                {"id": "b", "source": "local", "url": "/music/b.flac"},
            ]
            main.playback_queue_mode = "app_replace"
            main.playback_queue_index = 0
            main.queue_transition_target_url = "/music/b.flac"
            main.current_track_info = dict(main.playback_queue[0])
            main.last_track_info = dict(main.playback_queue[0])
            main.queue_advancing = False
            main.single_track_loop = False
            main.playback_transition_generation = 2
            main.latest_player_state_seq_seen = 0
            main.source_transition_lock = None
            main.manager = _FakeManager()
            main.peak_monitor = None
            main.build_playback_payload = lambda state: state
            main._schedule_radio_reconnect_if_needed = lambda _state: None

            async def no_peak_sync(*_args, **_kwargs):
                return None

            main.sync_peak_monitor_for_playback_state = no_peak_sync

            await main.on_player_state_change({
                "_seq": 1, "current_file": "/music/a.flac", "playlist_pos": 1,
                "paused": True, "ended": False,
            })
            self.assertEqual(main.queue_transition_target_url, "/music/b.flac")
            self.assertEqual(main.current_track_info["url"], "/music/a.flac")
            self.assertEqual(main.playback_queue_index, 0)

            await main.on_player_state_change({
                "_seq": 2, "current_file": "/music/b.flac", "playlist_pos": 1,
                "paused": False, "ended": False,
            })
            # The Coordinator endpoint owns the queue-track commit.  A player
            # callback may clear the transition marker, but stale MPV
            # playlist metadata must not commit a different queue item.
            self.assertIsNone(main.queue_transition_target_url)
            self.assertEqual(main.current_track_info["url"], "/music/a.flac")
            self.assertEqual(main.playback_queue_index, 0)
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

    def test_selected_queue_order_is_exact_and_deduplicated(self):
        original_scanner = main.library_scanner
        try:
            main.library_scanner = SimpleNamespace(
                get_tracks=lambda: [_FakeTrack("a"), _FakeTrack("b"), _FakeTrack("c")],
            )
            main._prepare_local_queue("b", ["c", "missing", "a", "c", "b"], shuffle=False)
            self.assertEqual([item["id"] for item in main.playback_queue], ["c", "a", "b"])

            main._prepare_local_queue("b", ["c", "a", "b"], shuffle=True, reshuffle=False)
            self.assertEqual([item["id"] for item in main.playback_queue], ["c", "a", "b"])
        finally:
            main.library_scanner = original_scanner


class _FakePlayer:
    _running = True

    def __init__(self):
        self.state = {
            "current_file": None, "paused": False, "playing": False,
            "ended": False, "position": 0.0, "volume": 100,
        }

    def set_pause(self, paused):
        self.state["paused"] = paused

    def loadfile(self, path, mode="replace", start_paused=False):
        self.state["current_file"] = path
        self.state["paused"] = bool(start_paused)
        self.state["playing"] = not start_paused
        self.state["position"] = 1.0

    def set_volume(self, volume):
        self.state["volume"] = volume

    def set_loop_playlist(self, enabled):
        return None

    def set_loop_file(self, enabled):
        return None


class ApiPlayQueueOrderTests(unittest.IsolatedAsyncioTestCase):
    """/api/play (queue jump) must preserve the passed queue order exactly,
    even while shuffle mode stays active. A genuinely new queue order still
    receives the legacy fresh shuffle."""

    def _install(self):
        originals = {
            name: getattr(main, name)
            for name in (
                "player_instance", "library_scanner", "current_track_info",
                "last_track_info", "last_radio_track_info", "current_footer_owner",
                "playback_queue", "playback_queue_original", "playback_queue_index",
                "playback_queue_mode", "playback_queue_loop", "playback_queue_shuffle",
                "single_track_loop", "queue_transition_target_url", "peak_monitor",
                "source_transition_lock", "playback_stream_stale_after_measurement",
                "_playback_state_before_measurement", "radio_stream_stale_after_measurement",
                "_radio_state_before_measurement", "playback_transition_generation",
                "radio_reconnect_attempts", "radio_reconnect_url",
                "radio_reconnect_active_since",
            )
        }
        main.player_instance = _FakePlayer()
        main.library_scanner = SimpleNamespace(
            get_tracks=lambda: [_FakeTrack("a"), _FakeTrack("b"), _FakeTrack("c")],
        )
        main.current_track_info = None
        main.last_track_info = None
        main.last_radio_track_info = None
        main.current_footer_owner = "local"
        main.playback_queue = []
        main.playback_queue_original = []
        main.playback_queue_index = -1
        main.playback_queue_mode = "app_replace"
        main.playback_queue_loop = False
        main.playback_queue_shuffle = False
        main.single_track_loop = False
        main.queue_transition_target_url = None
        main.peak_monitor = None
        main.source_transition_lock = None
        main.playback_stream_stale_after_measurement = False
        main._playback_state_before_measurement = None
        main.radio_stream_stale_after_measurement = False
        main._radio_state_before_measurement = None
        main.playback_transition_generation = 0
        main.radio_reconnect_attempts = 0
        main.radio_reconnect_url = None
        main.radio_reconnect_active_since = 0.0
        self.transition_requests = []
        return originals

    def _restore(self, originals):
        for name, value in originals.items():
            setattr(main, name, value)

    def _patches(self, recording_shuffle):
        async def no_op(*_args, **_kwargs):
            return None

        async def coordinated(request):
            self.transition_requests.append(request)
            main.player_instance.state.update({
                "current_file": request.target_url,
                "paused": not request.should_play,
                "playing": request.should_play,
                "ended": False,
                "position": 1.0,
                "volume": 100,
            })
            return SimpleNamespace(target_rate=request.target_rate)

        return [
            patch.object(main, "_can_send_play_command", return_value=True),
            patch.object(main, "_run_coordinated_transition", coordinated),
            patch.object(main, "_maybe_recover_samplerate_mismatch", no_op),
            patch.object(main, "_schedule_silent_active_watch", lambda **_k: None),
            patch.object(main, "build_playback_payload", lambda state: {}),
            patch.object(main.random, "shuffle", new=recording_shuffle),
        ]

    async def test_queue_jump_preserves_shuffled_order_without_reshuffle(self):
        originals = self._install()
        # Already-shuffled active queue with shuffle mode active.
        main.playback_queue = [
            {"id": "c", "source": "local", "url": "/music/c.flac"},
            {"id": "a", "source": "local", "url": "/music/a.flac"},
            {"id": "b", "source": "local", "url": "/music/b.flac"},
        ]
        main.playback_queue_shuffle = True
        main.playback_queue_index = 0
        shuffle_calls = []
        real_shuffle = main.random.shuffle

        def recording_shuffle(sequence):
            shuffle_calls.append(list(sequence))
            real_shuffle(sequence)

        try:
            with self._patch_context(recording_shuffle):
                req = SimpleNamespace(
                    source="local", track_id="b", url=None,
                    queue_track_ids=["c", "a", "b"], shuffle=True, loop=False,
                )
                await main.play_track(req)

            self.assertEqual([item["id"] for item in main.playback_queue], ["c", "a", "b"])
            self.assertTrue(main.playback_queue_shuffle)
            self.assertEqual(main.playback_queue_index, 2)
            self.assertEqual(shuffle_calls, [])
        finally:
            self._restore(originals)

    async def test_new_queue_with_shuffle_still_reshuffles(self):
        originals = self._install()
        main.playback_queue = [
            {"id": "c", "source": "local", "url": "/music/c.flac"},
            {"id": "a", "source": "local", "url": "/music/a.flac"},
            {"id": "b", "source": "local", "url": "/music/b.flac"},
        ]
        main.playback_queue_shuffle = True
        main.playback_queue_index = 0
        shuffle_calls = []
        real_shuffle = main.random.shuffle

        def recording_shuffle(sequence):
            shuffle_calls.append(list(sequence))
            real_shuffle(sequence)

        try:
            with self._patch_context(recording_shuffle):
                # Different order than the active queue -> new queue start.
                req = SimpleNamespace(
                    source="local", track_id="b", url=None,
                    queue_track_ids=["a", "c", "b"], shuffle=True, loop=False,
                )
                await main.play_track(req)

            self.assertEqual(len(shuffle_calls), 1)
            self.assertEqual(len(shuffle_calls[0]), 2)
            self.assertEqual(main.playback_queue[0]["id"], "b")
            self.assertEqual({item["id"] for item in main.playback_queue[1:]}, {"a", "c"})
            self.assertTrue(main.playback_queue_shuffle)
            self.assertEqual(main.playback_queue_index, 0)
        finally:
            self._restore(originals)

    def _patch_context(self, recording_shuffle):
        from contextlib import ExitStack

        stack = ExitStack()
        for patcher in self._patches(recording_shuffle):
            stack.enter_context(patcher)
        return stack


class PlayerctlCleanupTests(unittest.IsolatedAsyncioTestCase):
    """playerctl subprocesses must be reaped (terminate/kill + wait) on
    timeout or cancellation: no orphaned processes."""

    async def test_run_terminates_process_on_timeout(self):
        from spotify import _run

        class FakeProc:
            def __init__(self):
                self.returncode = None
                self.terminated = False
                self.killed = False
                self.wait_calls = 0
                self._exited = asyncio.Event()

            async def communicate(self):
                await asyncio.Event().wait()

            def terminate(self):
                self.terminated = True
                self.returncode = 0
                self._exited.set()

            def kill(self):
                self.killed = True
                self.returncode = 0
                self._exited.set()

            async def wait(self):
                self.wait_calls += 1
                await self._exited.wait()
                return self.returncode

        fake = FakeProc()

        async def fake_spawn(*_a, **_k):
            return fake

        with patch("spotify._find_playerctl", return_value="/usr/bin/playerctl"), \
             patch("spotify.asyncio.create_subprocess_exec", new=fake_spawn):
            result = await _run("--player=spotify", "metadata", timeout=0.05)
        self.assertIsNone(result)
        self.assertTrue(fake.terminated)
        self.assertFalse(fake.killed)
        self.assertGreaterEqual(fake.wait_calls, 1)

    async def test_run_escalates_to_kill_when_terminate_fails_to_stop(self):
        from spotify import _run

        class StubbornProc:
            def __init__(self):
                self.returncode = None
                self.terminated = False
                self.killed = False
                self.wait_calls = 0

            async def communicate(self):
                await asyncio.sleep(3600)

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True
                self.returncode = 9

            async def wait(self):
                self.wait_calls += 1
                if self.killed:
                    return self.returncode
                await asyncio.sleep(3600)

        fake = StubbornProc()

        async def fake_spawn(*_a, **_k):
            return fake

        with patch("spotify._find_playerctl", return_value="/usr/bin/playerctl"), \
             patch("spotify.asyncio.create_subprocess_exec", new=fake_spawn):
            result = await _run("--player=spotify", "metadata", timeout=0.05)
        self.assertIsNone(result)
        self.assertTrue(fake.terminated)
        self.assertTrue(fake.killed)
        self.assertGreaterEqual(fake.wait_calls, 2)


class MPVListenerTests(unittest.TestCase):
    """MPV volume events with numeric zero and listener reconnect after a
    broken socket/read."""

    def test_volume_zero_event_not_discarded(self):
        wrapper = player.MPVWrapper()
        wrapper._state["volume"] = 100
        wrapper._handle_event({"event": "property-change", "name": "volume", "data": 0})
        self.assertEqual(wrapper._state["volume"], 0)

    def test_volume_event_updates_state(self):
        wrapper = player.MPVWrapper()
        wrapper._handle_event({"event": "property-change", "name": "volume", "data": 42.0})
        self.assertEqual(wrapper._state["volume"], 42)

    def test_listener_reconnects_after_socket_close(self):
        wrapper = player.MPVWrapper()
        tmpdir = tempfile.mkdtemp()
        socket_path = os.path.join(tmpdir, "mpv-test.sock")
        wrapper.socket_path = socket_path
        wrapper._observer_ids = {}
        received = []
        wrapper._handle_event = lambda event: received.append(event)
        wrapper._running = True

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(socket_path)
        server.listen(4)
        server.settimeout(5)

        thread = threading.Thread(target=wrapper._event_listener_loop, daemon=True)
        thread.start()

        def send_event(connection, event):
            connection.sendall((json.dumps(event) + "\n").encode())

        try:
            conn1, _ = server.accept()
            send_event(conn1, {"event": "property-change", "name": "pause", "data": True})
            deadline = time.monotonic() + 2
            while len(received) < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            conn1.close()  # abrupt close -> EOF -> bounded reconnect

            conn2, _ = server.accept()
            send_event(conn2, {"event": "property-change", "name": "volume", "data": 0})
            deadline = time.monotonic() + 2
            while len(received) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            wrapper._running = False
            for connection in (conn1, conn2):
                try:
                    connection.close()
                except Exception:
                    pass
            try:
                server.close()
            except Exception:
                pass
            thread.join(timeout=3)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(received), 2)
        self.assertEqual(received[1]["data"], 0)


class SilentActiveDiagnosisTests(unittest.IsolatedAsyncioTestCase):
    """The silent-active diagnosis must use the real peak_monitor.snapshot()
    structure and gate on sample freshness (vu_fresh), not on the peak-hold
    "detected" flag. Automatic recovery stays disabled (log-only)."""

    def _install(self):
        originals = {
            name: getattr(main, name)
            for name in (
                "peak_monitor", "player_instance", "current_track_info",
                "current_footer_owner", "silent_active_recovery_attempts",
                "easyeffects_preset_load_lock", "_current_track_matches",
                "_is_local_playback_active", "_list_mpv_sink_inputs",
                "_active_unmuted_sink_inputs", "get_output_volume_safe",
                "get_audio_output_overview", "_run_debug_command",
                "_silent_active_source_links_present", "_silent_active_snapshot",
                "_is_measurement_window_open", "_list_sink_inputs",
            )
        }
        main.peak_monitor = SimpleNamespace()
        main.player_instance = SimpleNamespace(
            _running=True,
            state={"current_file": "/music/t1.flac", "paused": False, "ended": False, "volume": 100},
        )
        main.current_track_info = {"id": "t1", "title": "T1", "url": "/music/t1.flac", "source": "local"}
        main.current_footer_owner = "local"
        main.silent_active_recovery_attempts = set()
        main.easyeffects_preset_load_lock = None
        main._current_track_matches = lambda track: True
        main._is_local_playback_active = lambda state: True
        main._list_mpv_sink_inputs = lambda: [
            {"id": "si1", "volume_percent": 100, "muted": False, "corked": False},
        ]
        main._active_unmuted_sink_inputs = lambda entries: entries
        main.get_output_volume_safe = lambda default: 100
        main.get_audio_output_overview = lambda: {"output_mode": {"mode": "stereo"}}
        main._run_debug_command = lambda cmd, timeout: {
            "stdout": "mpv:output_FL -> easyeffects_sink:playback_FL\n",
        }
        main._silent_active_source_links_present = lambda *_a, **_k: True
        main._silent_active_snapshot = lambda **_k: {"diagnosis": True}
        main._is_measurement_window_open = lambda: False
        main._list_sink_inputs = lambda: []
        return originals

    def _restore(self, originals):
        for name, value in originals.items():
            setattr(main, name, value)

    async def test_fresh_samples_reach_diagnosis_and_recovery_stays_suppressed(self):
        originals = self._install()
        try:
            main.peak_monitor.snapshot = lambda: {
                "available": True,
                "detected": True,
                "vu_db": -60.0,
                "vu_fresh": True,
                "vu_age_ms": 120,
            }
            with self.assertLogs("main", level="WARNING") as captured:
                await main._check_and_recover_silent_active(
                    source="local",
                    signature="sig-fresh",
                    track={"id": "t1", "title": "T1", "url": "/music/t1.flac", "source": "local"},
                )
            joined = "\n".join(captured.output)
            self.assertIn("Silent-active playback detected", joined)
            self.assertIn("recovery_suppressed", joined)
            self.assertIn("sig-fresh", main.silent_active_recovery_attempts)
        finally:
            self._restore(originals)

    async def test_stale_samples_skip_even_when_peak_hold_detected(self):
        originals = self._install()
        try:
            # Stale samples with the peak-hold flag set: freshness decides.
            main.peak_monitor.snapshot = lambda: {
                "available": True,
                "detected": True,
                "vu_db": -60.0,
                "vu_fresh": False,
                "vu_age_ms": 99999,
            }
            with self.assertLogs("main", level="INFO") as captured:
                await main._check_and_recover_silent_active(
                    source="local",
                    signature="sig-stale",
                    track={"id": "t1", "title": "T1", "url": "/music/t1.flac", "source": "local"},
                )
            joined = "\n".join(captured.output)
            self.assertIn("peak_samples_stale", joined)
            self.assertNotIn("Silent-active playback detected", joined)
        finally:
            self._restore(originals)


if __name__ == "__main__":
    unittest.main()
