#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Rolling TIDAL playlist boundaries and stale prefetch protection."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playback.queue import PlaybackQueue, QueueCandidate


def track(track_id, rate=44100, *, resolved=True):
    return {"id": track_id, "source": "tidal", "title": track_id,
            "url": f"/cache/{track_id}.mp4" if resolved else "",
            "sample_rate_hz": rate if resolved else None}


class PlaylistPlayer:
    """Deterministic JSON-IPC boundary; appending never changes the source."""

    _running = True

    def __init__(self):
        self.state = {"current_file": "/cache/a.mp4", "playing": True,
                      "paused": False, "ended": False, "position": 20,
                      "playlist_pos": 0, "playlist_entry_id": 1, "file_loaded": True}
        self.playlist = ["/cache/a.mp4"]
        self.entry_ids = [1]
        self.next_entry_id = 2
        self.calls = []
        self.rate = 44100

    def get_property(self, name):
        return {"path": self.state["current_file"],
                "audio-params": {"samplerate": self.rate, "channels": "stereo"},
                "playlist": [{"filename": url, "id": entry_id,
                              "current": entry_id == self.state.get("playlist_entry_id"),
                              "playing": entry_id == self.state.get("playlist_entry_id")}
                             for url, entry_id in zip(self.playlist, self.entry_ids)],
                "idle-active": self.state["ended"]}[name]

    def clear_playlist(self):
        self.calls.append(("clear",))
        self.playlist = [self.state["current_file"]] if self.state["current_file"] else []
        self.entry_ids = [self.state["playlist_entry_id"]] if self.playlist else []
        self.state["playlist_pos"] = 0 if self.playlist else None

    def loadfile(self, url, mode="replace"):
        self.calls.append(("loadfile", url, mode))
        if mode == "append":
            self.playlist.append(url)
            self.entry_ids.append(self.next_entry_id)
        else:
            self.playlist = [url]
            self.entry_ids = [self.next_entry_id]
            self.state.update(current_file=url, position=0, ended=False, playing=True,
                              playlist_entry_id=self.next_entry_id)
        self.next_entry_id += 1

    def set_playlist_pos(self, index):
        self.calls.append(("playlist-pos", index))
        self.state.update(current_file=self.playlist[index], playlist_pos=index, position=0)

    def set_loop_playlist(self, enabled):
        self.calls.append(("loop-playlist", enabled))

    def set_loop_file(self, enabled):
        self.calls.append(("loop-file", enabled))

    def set_shuffle(self, enabled):
        self.calls.append(("shuffle", enabled))

    def natural_boundary(self):
        self.state.update(current_file=self.playlist[1], playlist_pos=1, position=0,
                          playlist_entry_id=self.entry_ids[1])
        return dict(self.state)


class TidalGaplessQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.player = PlaylistPlayer()
        self.current = track("a")
        self.owner = "tidal"
        self.epoch = 0
        self.source_generation = 0
        self.transition_active = False
        self.lock = asyncio.Lock()
        self.resolving = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.next_rate = 44100
        self.fail_resolution = False
        self.resolved_ids = []
        self.transitions = []

        async def resolve(entry):
            self.resolved_ids.append(entry["id"])
            self.resolving.set()
            await self.release.wait()
            if self.fail_resolution:
                return None
            entry.update(track(entry["id"], self.next_rate))
            return entry["url"]

        async def transition(request):
            self.transitions.append(request)
            self.player.loadfile(request.target_url)
            return SimpleNamespace(committed=True, target_rate=request.target_rate,
                                   transition_id="test-commit")

        def context(entry, _last=None, **_kwargs):
            self.current = dict(entry)

        @asynccontextmanager
        async def guard():
            async with self.lock:
                yield

        def ensure(token):
            if token != self.source_generation:
                raise RuntimeError("Source changed")

        self.queue = PlaybackQueue(SimpleNamespace(
            player=lambda: self.player, run_transition=transition,
            commit_coordinated_track=context, get_current_track_info=lambda: self.current,
            set_track_context=context,
            transition_is_active=lambda: self.transition_active or self.lock.locked(),
            player_is_running=lambda: self.player._running,
            coordinator_target_rate=lambda _source, entry: entry.get("sample_rate_hz"),
            coordinator_rate_change=lambda _rate: False,
            sample_rate_policy_is_auto=lambda: True,
            resolve_stream_url=resolve, capture_source_intent=lambda: self.source_generation,
            ensure_source_intent_current=ensure, stream_transport_guard=guard,
            stream_prefetch_epoch=lambda: self.epoch,
            get_playback_owner=lambda: self.owner,
            create_background_task=lambda coro, **kwargs: asyncio.create_task(coro, **kwargs),
        ))
        entries = [track("a"), track("b", resolved=False), track("c", resolved=False),
                   track("d", resolved=False)]
        self.queue.commit(QueueCandidate(entries, entries, 0, "app_replace", False,
                                         False, False, entries[0]))

    def prefetch(self):
        prefetch = getattr(self.queue, "stream_prefetch", None)
        self.assertIsNotNone(prefetch, "TIDAL needs a prepared MPV successor before EOF")
        return prefetch

    async def prepare(self):
        task = self.prefetch().schedule()
        if task is not None:
            await task

    async def test_prepares_next_before_eof_without_interrupting_current_audio(self):
        original = dict(self.player.state)
        await self.prepare()
        self.assertEqual(self.player.playlist, ["/cache/a.mp4", "/cache/b.mp4"])
        self.assertEqual(self.player.state, original)
        self.assertEqual(self.queue.index, 0)
        self.assertEqual(self.queue.tracks[1]["sample_rate_hz"], 44100)
        self.assertEqual(self.transitions, [])
        await self.prepare()
        self.assertEqual(self.resolved_ids, ["b"])

    async def test_multiple_native_boundaries_do_not_replace_pause_or_gate(self):
        for expected_index, expected_id in ((1, "b"), (2, "c"), (3, "d")):
            await self.prepare()
            synced = self.queue.sync_index_from_mpv(self.player.natural_boundary())
            self.assertIsNotNone(synced)
            index, entry = synced
            self.current = dict(entry)
            self.assertEqual((index, entry["id"]), (expected_index, expected_id))
        self.assertEqual(self.queue.index, 3)
        self.assertEqual(self.transitions, [])
        self.assertTrue(all(call[0] == "clear" or call[2] == "append" for call in self.player.calls))

    async def test_unknown_or_different_decoded_rate_uses_guarded_navigation(self):
        for rate in (None, 48000):
            self.next_rate = rate
            self.prefetch().invalidate()
            await self.prepare()
            self.assertEqual(self.player.playlist, ["/cache/a.mp4"])
        self.next_rate = 48000
        self.assertTrue(await self.queue.load_track(1))
        self.assertEqual(self.transitions[-1].source, "tidal")
        self.assertEqual(self.transitions[-1].target_rate, 48000)
        self.assertEqual(self.current["id"], "b")

    async def test_delayed_resolution_cannot_append_after_queue_reset(self):
        self.release.clear()
        task = self.prefetch().schedule()
        await self.resolving.wait()
        self.queue.reset()
        self.release.set()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.player.playlist, ["/cache/a.mp4"])
        self.assertEqual(self.queue.tracks, [])

    async def test_source_owner_epoch_and_live_path_changes_discard_delayed_result(self):
        for change in (lambda: setattr(self, "owner", "spotify"),
                       lambda: setattr(self, "epoch", self.epoch + 1),
                       lambda: setattr(self, "source_generation", self.source_generation + 1),
                       lambda: self.player.state.update(current_file="/music/new.flac")):
            self.owner = "tidal"
            self.player.state["current_file"] = "/cache/a.mp4"
            self.prefetch().invalidate()
            self.resolving.clear()
            self.release.clear()
            task = self.prefetch().schedule()
            await self.resolving.wait()
            change()
            self.release.set()
            await task
            self.assertEqual(self.player.playlist, ["/cache/a.mp4"])
            self.assertEqual(self.queue.tracks[1]["url"], "")

    async def test_owner_change_while_waiting_for_transport_lock_is_rechecked(self):
        @asynccontextmanager
        async def changed_owner_guard():
            async with self.lock:
                self.owner = "qobuz"
                yield

        self.queue._deps.stream_transport_guard = changed_owner_guard
        task = self.prefetch().schedule()
        await task
        self.assertEqual(self.player.playlist, ["/cache/a.mp4"])

    async def test_resolution_failure_keeps_queue_and_manual_retry_usable(self):
        self.fail_resolution = True
        original = self.queue.snapshot()
        await self.prepare()
        self.assertEqual(self.queue.snapshot(), original)
        self.assertEqual(self.player.playlist, ["/cache/a.mp4"])
        self.fail_resolution = False
        self.assertTrue(await self.queue.load_track(1))
        self.assertEqual(self.current["id"], "b")

    async def test_playlist_confirmation_ipc_failure_removes_unconfirmed_successor(self):
        get_property = self.player.get_property

        def read(name):
            if name == "playlist" and len(self.player.playlist) > 1:
                raise RuntimeError("MPV IPC read timed out")
            return get_property(name)

        with patch.object(self.player, "get_property", side_effect=read), \
                self.assertLogs("playback.stream_queue", level="WARNING"):
            await self.prepare()
        self.assertEqual(self.player.playlist, ["/cache/a.mp4"])
        self.assertTrue(self.player.state["playing"])
        self.assertFalse(self.prefetch().has_prepared)
        self.assertFalse(self.prefetch().has_native_entries)
        self.assertEqual(self.queue.index, 0)
        self.assertEqual(self.queue.tracks[1]["url"], "")

    async def test_uncertain_append_failure_removes_native_successor(self):
        loadfile = self.player.loadfile

        def append_then_fail(url, mode="replace"):
            loadfile(url, mode)
            raise RuntimeError("MPV append response was lost")

        with patch.object(self.player, "loadfile", side_effect=append_then_fail), \
                self.assertLogs("playback.stream_queue", level="WARNING"):
            await self.prepare()
        self.assertEqual(self.player.playlist, ["/cache/a.mp4"])
        self.assertFalse(self.prefetch().has_prepared)
        self.assertFalse(self.prefetch().has_native_entries)

    async def test_reset_reconciles_successor_that_starts_during_playlist_clear(self):
        await self.prepare()
        clear = self.player.clear_playlist

        def crossing_clear():
            if len(self.player.playlist) > 1:
                self.player.natural_boundary()
            clear()

        with patch.object(self.player, "clear_playlist", side_effect=crossing_clear):
            self.queue.reset()
        self.assertEqual(self.player.playlist, ["/cache/b.mp4"])
        self.assertEqual(self.current["id"], "b")
        self.assertEqual(self.queue.tracks, [])

    async def test_loop_reconciles_successor_that_starts_during_playlist_clear(self):
        await self.prepare()
        clear = self.player.clear_playlist

        def crossing_clear():
            self.player.natural_boundary()
            clear()

        with patch.object(self.player, "clear_playlist", side_effect=crossing_clear):
            self.queue.set_loop(True)
        self.assertEqual(self.current["id"], "b")
        self.assertEqual(self.queue.index, 1)
        await self.prepare()
        self.assertEqual(self.player.playlist, ["/cache/b.mp4", "/cache/c.mp4"])

    async def test_shuffle_keeps_successor_that_starts_during_playlist_clear(self):
        await self.prepare()
        clear = self.player.clear_playlist

        def crossing_clear():
            self.player.natural_boundary()
            clear()

        with patch.object(self.player, "clear_playlist", side_effect=crossing_clear):
            await self.queue.set_shuffle(True)
        self.assertEqual(self.current["id"], "b")
        self.assertEqual(self.queue.tracks[self.queue.index]["id"], "b")

    async def test_cleanup_reconciles_successor_started_before_append_response_failure(self):
        loadfile = self.player.loadfile

        def crossing_append(url, mode="replace"):
            loadfile(url, mode)
            self.player.natural_boundary()
            raise RuntimeError("MPV append response was lost")

        with patch.object(self.player, "loadfile", side_effect=crossing_append), \
                self.assertLogs("playback.stream_queue", level="WARNING"):
            await self.prepare()
        self.assertEqual(self.player.playlist, ["/cache/b.mp4"])
        self.assertEqual(self.current["id"], "b")
        self.assertEqual(self.queue.index, 1)
        self.assertFalse(self.prefetch().has_native_entries)
        self.assertEqual(await self.queue.advance(), "advanced")
        self.assertEqual(self.current["id"], "c")

    async def test_trim_readback_failure_keeps_identity_for_retry(self):
        await self.prepare()
        with patch.object(self.player, "get_property", side_effect=RuntimeError("MPV read timed out")):
            with self.assertRaises(RuntimeError):
                self.queue.reset()
        self.assertTrue(self.prefetch().has_native_entries)
        self.assertTrue(self.prefetch().has_prepared)
        self.assertEqual(self.queue.index, 0)
        self.queue.reset()
        self.assertFalse(self.prefetch().has_native_entries)

    async def test_retained_entry_readback_reconciles_before_listener_snapshot_catches_up(self):
        await self.prepare()

        def clear_before_event_delivery():
            self.player.playlist = [self.player.playlist[1]]
            self.player.entry_ids = [self.player.entry_ids[1]]

        with patch.object(self.player, "clear_playlist", side_effect=clear_before_event_delivery):
            self.queue.reset()
        self.assertEqual(self.player.state["current_file"], "/cache/a.mp4")
        self.assertEqual(self.player.playlist, ["/cache/b.mp4"])
        self.assertEqual(self.current["id"], "b")

    async def test_unconfirmed_cleanup_failure_can_be_retired_by_later_reset(self):
        loadfile = self.player.loadfile
        clear = self.player.clear_playlist

        def append_then_fail(url, mode="replace"):
            loadfile(url, mode)
            raise RuntimeError("MPV append response was lost")

        def fail_cleanup():
            if len(self.player.playlist) > 1:
                raise RuntimeError("MPV clear failed")
            clear()

        with patch.object(self.player, "loadfile", side_effect=append_then_fail), \
                patch.object(self.player, "clear_playlist", side_effect=fail_cleanup), \
                self.assertLogs("playback.stream_queue", level="WARNING"):
            await self.prepare()
        self.assertTrue(self.prefetch().has_native_entries)
        self.assertFalse(self.prefetch().has_prepared)
        self.queue.reset()
        self.assertEqual(self.player.playlist, ["/cache/a.mp4"])
        self.assertFalse(self.prefetch().has_native_entries)

    async def test_reset_after_process_crash_retires_native_ownership_without_ipc(self):
        await self.prepare()
        self.player._running = False
        with patch.object(self.player, "get_property", side_effect=RuntimeError("MPV is not running")):
            self.queue.reset()
        self.assertEqual(self.queue.tracks, [])
        self.assertFalse(self.prefetch().has_native_entries)
        self.assertFalse(self.prefetch().has_prepared)

    async def test_reset_does_not_clear_a_replacement_players_playlist(self):
        await self.prepare()
        self.player = PlaylistPlayer()
        self.player.playlist = ["/music/new.flac", "/music/next.flac"]
        with patch.object(self.player, "get_property", side_effect=RuntimeError("Foreign playlist IPC")):
            self.queue.reset()
        self.assertEqual(self.player.playlist, ["/music/new.flac", "/music/next.flac"])
        self.assertEqual(self.queue.tracks, [])
        self.assertFalse(self.prefetch().has_native_entries)

    async def test_reset_trims_prepared_successor_without_stopping_current(self):
        await self.prepare()
        self.queue.reset()
        self.assertEqual(self.player.playlist, ["/cache/a.mp4"])
        self.assertTrue(self.player.state["playing"])

    async def test_paused_track_can_prepare_without_resuming(self):
        self.player.state.update(paused=True, playing=False)
        await self.prepare()
        self.assertEqual(self.player.playlist, ["/cache/a.mp4", "/cache/b.mp4"])
        self.assertTrue(self.player.state["paused"])
        self.assertFalse(self.player.state["playing"])

    async def test_loop_prepares_first_track_and_disabling_loop_removes_it(self):
        self.queue.index = 3
        self.current = track("d")
        self.queue.tracks[3] = dict(self.current)
        self.player.state["current_file"] = "/cache/d.mp4"
        self.player.playlist = ["/cache/d.mp4"]
        self.queue.loop = True
        await self.prepare()
        self.assertEqual(self.player.playlist, ["/cache/d.mp4", "/cache/a.mp4"])
        self.queue.set_loop(False)
        self.assertEqual(self.player.playlist, ["/cache/d.mp4"])

    async def test_shuffle_drops_old_prepared_order(self):
        await self.prepare()
        await self.queue.set_shuffle(True)
        self.assertEqual(self.player.playlist, ["/cache/a.mp4"])
        self.assertTrue(self.queue.shuffle)

    async def test_duplicate_path_does_not_advance_before_its_native_entry_starts(self):
        self.queue.tracks[1] = track("a", resolved=False)
        await self.prepare()
        self.assertIsNone(self.queue.sync_index_from_mpv(dict(self.player.state)))
        synced = self.queue.sync_index_from_mpv(self.player.natural_boundary())
        self.assertEqual(synced[0], 1)
        self.assertEqual(synced[1]["id"], "a")

    async def test_non_reloading_epoch_change_keeps_confirmed_successor_identity(self):
        await self.prepare()
        self.epoch += 1
        synced = self.queue.sync_index_from_mpv(self.player.natural_boundary())
        self.assertIsNotNone(synced)
        self.assertEqual(synced[0], 1)
        self.current = dict(synced[1])
        await self.prepare()
        self.assertEqual(self.player.playlist, ["/cache/b.mp4", "/cache/c.mp4"])

    async def test_source_change_rejects_queued_boundary_publication(self):
        await self.prepare()
        event = self.player.natural_boundary()
        self.owner = "spotify"
        self.source_generation += 1
        self.player.state.update(current_file=None, playing=False)
        self.assertIsNone(self.queue.sync_index_from_mpv(event))
        self.assertEqual(self.queue.index, 0)

    async def test_loop_toggle_reconciles_a_boundary_before_discarding_its_mapping(self):
        await self.prepare()
        self.player.natural_boundary()
        self.queue.set_loop(True)
        self.assertEqual(self.queue.index, 1)
        self.assertEqual(self.current["id"], "b")
        await self.prepare()
        self.assertEqual(self.player.playlist, ["/cache/b.mp4", "/cache/c.mp4"])

    async def test_shuffle_reconciles_a_boundary_before_reordering(self):
        await self.prepare()
        self.player.natural_boundary()
        await self.queue.set_shuffle(True)
        self.assertEqual(self.queue.tracks[self.queue.index]["id"], "b")
        self.assertEqual(self.current["id"], "b")

    async def test_library_prune_does_not_retire_streaming_queue_or_successor(self):
        await self.prepare()
        result = self.queue.prune_removed_tracks(["local-track"])
        self.assertEqual(result["removed"], 0)
        self.assertEqual(len(self.queue.tracks), 4)
        self.assertEqual(self.player.playlist, ["/cache/a.mp4", "/cache/b.mp4"])

    async def test_decoder_rate_readiness_can_retry_without_a_new_track_commit(self):
        self.player.rate = None
        with patch("playback.stream_queue.monotonic", return_value=100):
            await self.prepare()
        self.player.rate = 44100
        with patch("playback.stream_queue.monotonic", return_value=101):
            await self.prepare()
        self.assertEqual(self.player.playlist, ["/cache/a.mp4", "/cache/b.mp4"])

    async def test_decoder_readiness_retries_are_bounded_per_context(self):
        self.player.rate = None
        for now in (100, 101, 102, 103):
            with patch("playback.stream_queue.monotonic", return_value=now):
                await self.prepare()
        self.assertEqual(self.resolved_ids, ["b", "b", "b"])
        self.assertEqual(self.player.playlist, ["/cache/a.mp4"])
        self.epoch += 1
        self.player.rate = 44100
        await self.prepare()
        self.assertEqual(self.player.playlist, ["/cache/a.mp4", "/cache/b.mp4"])

    async def test_manual_next_reconciles_an_already_started_successor(self):
        await self.prepare()
        self.player.natural_boundary()
        self.assertEqual(await self.queue.advance(), "advanced")
        self.assertEqual(self.current["id"], "c")

    async def test_reset_keeps_metadata_of_an_already_started_successor(self):
        await self.prepare()
        self.player.natural_boundary()
        self.queue.reset()
        self.assertEqual(self.current["id"], "b")
        self.assertEqual(self.player.playlist, ["/cache/b.mp4"])


if __name__ == "__main__":
    unittest.main()
