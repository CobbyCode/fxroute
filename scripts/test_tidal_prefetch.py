# SPDX-License-Identifier: AGPL-3.0-only
"""TIDAL cache prefetch compatibility and app/native-boundary integration."""

import asyncio
from contextlib import asynccontextmanager, contextmanager, ExitStack
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from playback.queue import PlaybackQueue, QueueCandidate
from playback.player import MPVWrapper


class TestPrefetchStream(unittest.TestCase):
    def test_prefetch_resolves_next_track(self):
        from streaming.tidal.playback import prefetch_stream
        with mock.patch("streaming.tidal.playback.resolve_stream_for_id") as resolve:
            prefetch_stream("12345")
        resolve.assert_called_once_with("12345")

    def test_prefetch_failure_does_not_break_playback(self):
        from streaming.tidal.playback import prefetch_stream, TidalStreamError
        with mock.patch("streaming.tidal.playback.resolve_stream_for_id",
                        side_effect=TidalStreamError("auth", "expired")):
            prefetch_stream("12345")


class TestTidalNativeBoundaryIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mark_authoritative = main._mark_player_state_authoritative
        self.player = MPVWrapper()
        self.playlist = ["/cache/a.mp4"]
        self.entry_ids = [1]
        self.next_entry_id = 2
        self.commands = []
        self.player._running = True
        self.player._state.update(current_file="/cache/a.mp4", paused=False, playing=True,
                                  playlist_entry_id=1)
        self.player._state_snapshot = dict(self.player._state)

        def command(name, *args):
            self.commands.append((name, *args))
            if name == "get_property":
                return {"data": {"path": self.player.state["current_file"],
                                 "playlist": [{"filename": url, "id": entry_id}
                                              for url, entry_id in zip(self.playlist, self.entry_ids)],
                                 "audio-params": {"samplerate": 44100}}[args[0]]}
            if name == "playlist-clear":
                self.playlist[:] = [self.player.state["current_file"]]
                self.entry_ids[:] = [self.player.state["playlist_entry_id"]]
            if name == "loadfile":
                if args[1] == "append":
                    self.playlist.append(args[0])
                    self.entry_ids.append(self.next_entry_id)
                else:
                    self.playlist[:] = [args[0]]
                    self.entry_ids[:] = [self.next_entry_id]
                self.next_entry_id += 1
            return {"error": "success"}

        async def resolve(entry):
            entry.update(url=f"/cache/{entry['id']}.mp4", sample_rate_hz=44100)
            return entry["url"]

        @asynccontextmanager
        async def guard():
            yield

        deps = SimpleNamespace(**vars(main.playback_queue.queue._deps))
        deps.resolve_stream_url = resolve
        deps.player_is_running = lambda: True
        deps.stream_transport_guard = guard
        deps.capture_source_intent = lambda: None
        deps.ensure_source_intent_current = lambda _token: None
        deps.stream_prefetch_epoch = lambda: 0
        deps.transition_is_active = lambda: False
        deps.create_background_task = lambda coro, **kwargs: asyncio.create_task(coro, **kwargs)
        self.queue = PlaybackQueue(deps)
        self.tracks = [{"id": name, "source": "tidal",
                        "url": "/cache/a.mp4" if name == "a" else "",
                        "sample_rate_hz": 44100 if name == "a" else None}
                       for name in ("a", "b", "c")]
        self.queue.commit(QueueCandidate(self.tracks, self.tracks, 0, "app_replace",
                                         False, False, False, self.tracks[0]))
        patches = [
            mock.patch.object(self.player, "_send_command", side_effect=command),
            mock.patch.object(main.runtime, "player_instance", self.player),
            mock.patch.object(main.playback_queue, "queue", self.queue),
            mock.patch.object(main.playback_state, "current_track_info", dict(self.tracks[0])),
            mock.patch.object(main.playback_state, "last_track_info", dict(self.tracks[0])),
            mock.patch.object(main.playback_state, "current_playback_owner", "tidal"),
            mock.patch.object(main.playback_state, "latest_player_state_seq_seen", 0),
            mock.patch.object(main, "_playback_transition_is_active", return_value=False),
            mock.patch.object(main, "_coordinator_rate_change", return_value=False),
            mock.patch.object(main, "_sample_rate_policy_is_auto", return_value=True),
            mock.patch.object(main, "_current_playback_commit_id", return_value="test"),
            mock.patch.object(main, "_publish_playback_context_commit"),
            mock.patch.object(main, "_mark_playback_intent_changed"),
            mock.patch.object(main, "_mark_player_state_authoritative"),
            mock.patch.object(main, "_capture_playback_transition_epoch", return_value=0),
            mock.patch.object(main, "_playback_transition_context_is_current", return_value=True),
            mock.patch.object(main.runtime, "source_transition_lock", None),
            mock.patch.object(main.peak_monitor_coordinator, "sync_playback_state", new=mock.AsyncMock()),
            mock.patch.object(main.radio_reconnect, "schedule"),
            mock.patch.object(main.manager, "broadcast", new=mock.AsyncMock()),
            mock.patch.object(main, "build_playback_payload", side_effect=lambda state: state),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.queue.stream_prefetch.invalidate)

    def start_successor(self):
        self.player._handle_event({"event": "start-file", "playlist_entry_id": self.entry_ids[1]})
        self.player._handle_event({"event": "property-change", "name": "path", "data": "/cache/b.mp4"})
        return self.player.state

    @contextmanager
    def manual_transport(self):
        lock = asyncio.Lock()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(main, "playback_transition_coordinator",
                                                  SimpleNamespace(lock=lock)))
            stack.enter_context(mock.patch.object(main, "_playback_transition_is_active",
                                                  side_effect=lock.locked))
            stack.enter_context(mock.patch.object(main, "_mark_player_state_authoritative",
                                                  side_effect=self.mark_authoritative))
            stack.enter_context(mock.patch.object(main, "_player_is_running", return_value=True))
            stack.enter_context(mock.patch.object(main, "_can_send_play_command", return_value=True))
            stack.enter_context(mock.patch.object(main, "_route_global_control",
                                                  new=mock.AsyncMock(return_value=None)))
            self.queue._deps.transition_is_active = lock.locked
            yield

    async def test_pause_publishes_started_successor_before_discarding_older_callback(self):
        await self.queue.stream_prefetch.schedule()
        event = self.start_successor()
        with self.manual_transport():
            await main.pause_playback()
            await main.on_player_state_change(event)
        self.assertTrue(self.player.state["paused"])
        self.assertEqual(self.queue.index, 1)
        self.assertEqual(main.playback_state.current_track_info["id"], "b")

    async def test_toggle_pause_publishes_started_successor_under_transport_lock(self):
        await self.queue.stream_prefetch.schedule()
        event = self.start_successor()
        with self.manual_transport():
            await main.toggle_playback()
            await main.on_player_state_change(event)
        self.assertTrue(self.player.state["paused"])
        self.assertEqual(self.queue.index, 1)
        self.assertEqual(main.playback_state.current_track_info["id"], "b")

    async def test_toggle_resume_publishes_started_successor_under_transport_lock(self):
        await self.queue.stream_prefetch.schedule()
        self.start_successor()
        self.player.set_pause(True)
        event = self.player.state
        with self.manual_transport():
            await main.toggle_playback()
            await main.on_player_state_change(event)
        self.assertFalse(self.player.state["paused"])
        self.assertEqual(self.queue.index, 1)
        self.assertEqual(main.playback_state.current_track_info["id"], "b")

    async def test_stop_retains_started_successor_for_replay(self):
        await self.queue.stream_prefetch.schedule()
        event = self.start_successor()
        with self.manual_transport(), mock.patch.object(main.radio_reconnect, "reset", new=mock.AsyncMock()), \
                mock.patch.object(main, "_measurement_session_owns_live_rate", return_value=True):
            await main.stop_playback()
            await main.on_player_state_change(event)
        self.assertIsNone(main.playback_state.current_track_info)
        self.assertIsNone(self.player.state["current_file"])
        self.assertEqual(main.playback_state.last_track_info["id"], "b")
        self.assertEqual(self.queue.tracks, [])

    async def test_single_track_play_trims_successor_prepared_during_rate_read(self):
        to_thread = asyncio.to_thread
        requests = []

        async def delayed_rate_read(func, *args, **kwargs):
            if func is main._coordinator_rate_change:
                await self.queue.stream_prefetch.schedule()
                return False
            return await to_thread(func, *args, **kwargs)

        async def transition(request):
            requests.append(request)
            return SimpleNamespace(committed=True, target_rate=44100, transition_id="single")

        provider = SimpleNamespace(is_authenticated=mock.AsyncMock(return_value=True))
        with mock.patch.object(main, "_player_is_running", return_value=True), \
                mock.patch.object(main, "_can_send_play_command", return_value=True), \
                mock.patch.object(main, "_tidal_provider", return_value=provider), \
                mock.patch.object(main, "_resolve_tidal_track", new=mock.AsyncMock(return_value=dict(self.tracks[0]))), \
                mock.patch.object(main, "_run_coordinated_transition", side_effect=transition), \
                mock.patch.object(asyncio, "to_thread", side_effect=delayed_rate_read):
            await main.play_track(main.PlayRequest(source="tidal", track_id="a"))
        self.assertFalse(requests[0].reload_source)
        self.assertEqual(self.queue.tracks, [])
        self.assertEqual(self.playlist, ["/cache/a.mp4"])

    async def test_failed_single_track_trim_retains_cleanup_ownership_and_reports_failure(self):
        await self.queue.stream_prefetch.schedule()
        provider = SimpleNamespace(is_authenticated=mock.AsyncMock(return_value=True))
        result = SimpleNamespace(committed=True, target_rate=44100, transition_id="single")
        with mock.patch.object(main, "_player_is_running", return_value=True), \
                mock.patch.object(main, "_can_send_play_command", return_value=True), \
                mock.patch.object(main, "_tidal_provider", return_value=provider), \
                mock.patch.object(main, "_resolve_tidal_track", new=mock.AsyncMock(return_value=dict(self.tracks[0]))), \
                mock.patch.object(main, "_run_coordinated_transition", new=mock.AsyncMock(return_value=result)), \
                mock.patch.object(self.player, "clear_playlist", side_effect=RuntimeError("MPV clear failed")), \
                self.assertLogs(level="WARNING"):
            with self.assertRaises(main.HTTPException) as caught:
                await main.play_track(main.PlayRequest(source="tidal", track_id="a"))
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(self.playlist, ["/cache/a.mp4", "/cache/b.mp4"])
        self.assertTrue(self.queue.stream_prefetch.has_native_entries)
        self.assertEqual(len(self.queue.tracks), 3)
        self.queue.reset()
        self.assertEqual(self.playlist, ["/cache/a.mp4"])
        self.assertFalse(self.queue.stream_prefetch.has_native_entries)

    async def test_same_target_play_reports_boundary_race_instead_of_publishing_wrong_track(self):
        await self.queue.stream_prefetch.schedule()
        clear = self.player.clear_playlist

        def crossing_clear():
            self.start_successor()
            clear()

        provider = SimpleNamespace(is_authenticated=mock.AsyncMock(return_value=True))
        result = SimpleNamespace(committed=True, target_rate=44100, transition_id="single")
        with mock.patch.object(main, "_player_is_running", return_value=True), \
                mock.patch.object(main, "_can_send_play_command", return_value=True), \
                mock.patch.object(main, "_tidal_provider", return_value=provider), \
                mock.patch.object(main, "_resolve_tidal_track", new=mock.AsyncMock(return_value=dict(self.tracks[0]))), \
                mock.patch.object(main, "_run_coordinated_transition", new=mock.AsyncMock(return_value=result)), \
                mock.patch.object(self.player, "clear_playlist", side_effect=crossing_clear):
            with self.assertRaises(main.HTTPException) as caught:
                await main.play_track(main.PlayRequest(source="tidal", track_id="a"))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.playlist, ["/cache/b.mp4"])
        self.assertEqual(main.playback_state.current_track_info["id"], "b")
        self.assertEqual(self.queue.index, 1)
        self.assertEqual(len(self.queue.tracks), 3)

    async def test_restart_with_reused_entry_ids_prepares_successor_again(self):
        await self.queue.stream_prefetch.schedule()
        self.player._forget_previous_process_state()
        self.playlist[:] = ["/cache/a.mp4"]
        self.entry_ids[:] = [1]
        self.next_entry_id = 2
        self.player._handle_event({"event": "start-file", "playlist_entry_id": 1})
        self.player._handle_event({"event": "property-change", "name": "path", "data": "/cache/a.mp4"})
        await self.queue.stream_prefetch.schedule()
        self.assertEqual(self.playlist, ["/cache/a.mp4", "/cache/b.mp4"])
        self.assertEqual(self.queue.index, 0)
        self.assertEqual(main.playback_state.current_track_info["id"], "a")

    async def test_restart_rejects_old_process_successor_identity(self):
        await self.queue.stream_prefetch.schedule()
        event = self.start_successor()
        self.player._forget_previous_process_state()
        self.player._handle_event({"event": "start-file", "playlist_entry_id": 2})
        self.player._handle_event({"event": "property-change", "name": "path", "data": "/cache/b.mp4"})
        self.assertIsNone(self.queue.sync_index_from_mpv(event))
        self.assertEqual(self.queue.index, 0)

    async def test_library_switch_after_process_crash_retires_streaming_queue(self):
        await self.queue.stream_prefetch.schedule()
        self.player._running = False
        retired = SimpleNamespace(cancel_refresh=mock.Mock())
        selected = SimpleNamespace(prepare_scan_status=mock.Mock())
        with mock.patch.object(self.queue._deps, "player_is_running", return_value=False), \
                mock.patch.object(self.player, "get_property", side_effect=RuntimeError("MPV is not running")), \
                mock.patch.object(main.runtime.music_library, "scanner", retired), \
                mock.patch.object(main.runtime, "library_refresh_tasks", set()), \
                mock.patch.object(main.runtime, "library_scan_task", None), \
                mock.patch.object(main, "_queue_reconcile_generation", main._queue_reconcile_generation), \
                mock.patch.object(main, "_library_scanner_for", return_value=selected), \
                mock.patch.object(main, "_create_library_refresh_task", return_value=None):
            await main._commit_music_library_switch(retired, root=Path("/music/new"), library_id="local")
            self.assertIs(main.runtime.music_library.scanner, selected)
        self.assertEqual(self.queue.tracks, [])
        self.assertFalse(self.queue.stream_prefetch.has_native_entries)

    def successor_eof_event(self):
        self.player._handle_event({"event": "property-change", "name": "idle-active", "data": False})
        self.player._handle_event({"event": "end-file", "reason": "eof", "playlist_entry_id": 1})
        self.player._handle_event({"event": "property-change", "name": "path", "data": None})
        return self.player.state

    async def test_loop_reconciliation_rejects_delayed_predecessor_eof(self):
        await self.queue.stream_prefetch.schedule()
        event = self.successor_eof_event()
        self.start_successor()
        self.queue.set_loop(True)
        with mock.patch.object(main, "_run_coordinated_transition",
                               side_effect=AssertionError("Old EOF must not replace B")):
            await main.on_player_state_change(event, event_commit_id="test")
        self.assertEqual(main.playback_state.current_track_info["id"], "b")
        self.assertEqual(self.queue.index, 1)

    async def test_shuffle_reconciliation_rejects_delayed_predecessor_eof(self):
        await self.queue.stream_prefetch.schedule()
        event = self.successor_eof_event()
        self.start_successor()
        await self.queue.set_shuffle(True)
        with mock.patch.object(main, "_run_coordinated_transition",
                               side_effect=AssertionError("Old EOF must not replace B")):
            await main.on_player_state_change(event, event_commit_id="test")
        self.assertEqual(main.playback_state.current_track_info["id"], "b")
        self.assertEqual(self.queue.tracks[self.queue.index]["id"], "b")

    async def test_eof_rechecks_live_successor_after_transition_waiter(self):
        await self.queue.stream_prefetch.schedule()
        self.successor_eof_event()
        self.player._handle_event({"event": "property-change", "name": "idle-active", "data": True})
        event = self.player.state

        async def settled():
            self.start_successor()

        with mock.patch.object(main, "_wait_playback_transition_settled", side_effect=settled), \
                mock.patch.object(main, "_run_coordinated_transition",
                                  side_effect=AssertionError("EOF became stale while waiting")):
            await main.on_player_state_change(event, event_commit_id="test")
        self.assertEqual(self.player.state["current_file"], "/cache/b.mp4")
        self.assertFalse(any(command[0] == "loadfile" and command[2] == "replace"
                             for command in self.commands))

    async def test_commit_prepares_successor_without_replacing_current(self):
        main._commit_coordinated_track(self.tracks[0], source="tidal", commit_token="test")
        await self.queue.stream_prefetch._task
        self.assertEqual(self.playlist, ["/cache/a.mp4", "/cache/b.mp4"])
        self.assertEqual(self.player.state["current_file"], "/cache/a.mp4")
        self.assertTrue(self.player.state["playing"])

    async def test_natural_boundary_updates_context_and_prepares_another_successor(self):
        main._schedule_tidal_prefetch()
        await self.queue.stream_prefetch._task
        self.player._handle_event({"event": "start-file", "playlist_entry_id": self.entry_ids[1]})
        self.player._handle_event({"event": "property-change", "name": "path", "data": "/cache/b.mp4"})
        await main.on_player_state_change(self.player.state)
        await self.queue.stream_prefetch._task
        self.assertEqual(main.playback_state.current_track_info["id"], "b")
        self.assertEqual(self.queue.index, 1)
        self.assertEqual(self.playlist, ["/cache/b.mp4", "/cache/c.mp4"])
        self.assertFalse(any(command[0] == "set_property" for command in self.commands))

    async def test_transient_path_clear_does_not_restart_prepared_successor(self):
        main._schedule_tidal_prefetch()
        await self.queue.stream_prefetch._task
        self.player._handle_event({"event": "property-change", "name": "idle-active", "data": False})
        self.player._handle_event({"event": "end-file", "reason": "eof", "playlist_entry_id": 1})
        self.player._handle_event({"event": "property-change", "name": "path", "data": None})
        with mock.patch.object(main, "_run_coordinated_transition",
                               side_effect=AssertionError("Native EOF must not replace the successor")):
            await main.on_player_state_change(self.player.state, event_commit_id="test")
        self.player._handle_event({"event": "start-file", "playlist_entry_id": self.entry_ids[1]})
        self.player._handle_event({"event": "property-change", "name": "path", "data": "/cache/b.mp4"})
        await main.on_player_state_change(self.player.state, event_commit_id="test")
        await self.queue.stream_prefetch._task
        self.assertEqual(self.queue.index, 1)
        self.assertEqual(main.playback_state.current_track_info["id"], "b")
        self.assertFalse(any(command[0] == "loadfile" and command[2] == "replace"
                             for command in self.commands))

    async def test_terminal_idle_after_failed_successor_still_runs_eof_fallback(self):
        main._schedule_tidal_prefetch()
        await self.queue.stream_prefetch._task
        self.player._handle_event({"event": "property-change", "name": "idle-active", "data": False})
        self.player._handle_event({"event": "end-file", "reason": "eof", "playlist_entry_id": 1})
        self.player._handle_event({"event": "property-change", "name": "path", "data": None})
        requests = []

        async def transition(request):
            requests.append(request)
            self.player.loadfile(request.target_url)
            self.player._handle_event({"event": "start-file", "playlist_entry_id": self.entry_ids[0]})
            return SimpleNamespace(committed=True, target_rate=44100, transition_id="fallback")

        with mock.patch.object(main, "_run_coordinated_transition", side_effect=transition):
            await main.on_player_state_change(self.player.state, event_commit_id="test")
            self.assertEqual(requests, [])
            old_sequence = self.player.state["_seq"]
            self.player._handle_event({"event": "property-change", "name": "idle-active", "data": True})
            self.assertGreater(self.player.state["_seq"], old_sequence)
            await main.on_player_state_change(self.player.state, event_commit_id="test")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].detail, "queue auto-advance")
        self.assertEqual(main.playback_state.current_track_info["id"], "b")


if __name__ == "__main__":
    unittest.main()
