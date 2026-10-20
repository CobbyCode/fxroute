#!/usr/bin/env python3
"""/api/play with ``queue_refs``: one queue of local ids and ``tidal:<id>``.

Covers the queue shape (``app_replace`` with a source per entry), which entry
starts, what is left out while TIDAL is not connected or a track is gone and
how that is reported, the all-local fallback to the native gapless queue,
shuffle semantics and that a failed transition leaves the old queue alone.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import playback.queue as playback_queue
from playback.transition import PlaybackTransitionFailure
from playback_queue_test_support import queue_state, restore_queue_state
import test_play_queue_transactional as base


def _tidal_entry(track_id, *, with_url=False):
    return {
        "id": str(track_id), "title": f"T{track_id}", "artist": "Tidal Artist",
        "album": "Tidal Album", "source": "tidal",
        "url": f"https://cdn/{track_id}.flac" if with_url else "",
        "sample_rate_hz": 44100 if with_url else None,
    }


class _Provider:
    def __init__(self, connected=True):
        self.connected = connected

    async def is_authenticated(self):
        return self.connected


class MixedQueuePlayTests(unittest.IsolatedAsyncioTestCase):
    GLOBALS = base._QUEUE_TEST_GLOBALS
    _install = base.PlayQueueTransactionalTests._install
    _restore = base.PlayQueueTransactionalTests._restore
    _patches = base.PlayQueueTransactionalTests._patches
    _patch_context = base.PlayQueueTransactionalTests._patch_context

    def _play(self, *, track_id, refs, source="local", shuffle=False, loop=False):
        return main.play_track(main.PlayRequest(
            source=source, track_id=track_id, queue_refs=refs, shuffle=shuffle, loop=loop,
        ))

    def _run(self, requests):
        async def run(request):
            requests.append(request)
            return SimpleNamespace(target_rate=44100, committed=True, transition_id="tr-mixed")
        return run

    def _tidal_patches(self, *, connected=True, unresolvable=(), start_error=None):
        self.stream_mock = AsyncMock(side_effect=start_error) if start_error else AsyncMock(
            side_effect=lambda track_id: _tidal_entry(track_id, with_url=True))

        async def meta(track_id):
            if str(track_id) in unresolvable:
                raise main.HTTPException(status_code=404, detail="gone")
            return _tidal_entry(track_id)

        self.meta_mock = AsyncMock(side_effect=meta)
        return [
            patch.object(main, "_tidal_provider", return_value=_Provider(connected)),
            patch.object(main, "_resolve_tidal_track", new=self.stream_mock),
            patch.object(main, "_resolve_tidal_track_meta", new=self.meta_mock),
            patch.object(main, "_schedule_tidal_prefetch", lambda: None),
            patch.object(main, "_coordinator_rate_change", return_value=False),
        ]

    async def _play_with(self, requests, patches, **kwargs):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            stack.enter_context(self._patch_context(self._run(requests)))
            return await self._play(**kwargs)

    async def test_local_start_keeps_the_mixed_queue_in_order(self):
        originals = self._install([base._track("x")], index=0)
        requests = []
        try:
            result = await self._play_with(
                requests, self._tidal_patches(),
                track_id="a", refs=["a", "tidal:101", "b", "tidal:102"],
            )
            self.assertEqual(result["status"], "playing")
            queue = playback_queue.queue
            self.assertEqual(queue.mode, "app_replace")
            self.assertEqual([(t["source"], t["id"]) for t in queue.tracks],
                             [("local", "a"), ("tidal", "101"), ("local", "b"), ("tidal", "102")])
            self.assertEqual(queue.index, 0)
            self.assertEqual(requests[0].source, "local")
            self.assertEqual(result["queue_summary"], {"requested": 4, "queued": 4, "skipped": {}})
        finally:
            self._restore(originals)

    async def test_tidal_start_resolves_the_stream_for_that_entry_only(self):
        originals = self._install([base._track("x")], index=0)
        requests = []
        patches = self._tidal_patches()
        try:
            result = await self._play_with(
                requests, patches,
                track_id="102", source="tidal", refs=["a", "tidal:101", "tidal:102", "b"],
            )
            queue = playback_queue.queue
            self.assertEqual(requests[0].source, "tidal")
            self.assertEqual(result["track"]["url"], "https://cdn/102.flac")
            self.assertEqual(queue.tracks[queue.index]["id"], "102")
            # Only the starting entry gets a stream URL now; the others are
            # resolved just before their own transition.
            self.assertEqual(queue.tracks[1]["url"], "")
            self.assertEqual(self.stream_mock.await_count, 1)
        finally:
            self._restore(originals)

    async def test_not_connected_leaves_tidal_out_and_plays_the_local_queue(self):
        originals = self._install([base._track("x")], index=0)
        requests = []
        try:
            result = await self._play_with(
                requests, self._tidal_patches(connected=False),
                track_id="b", refs=["a", "tidal:101", "b", "c"],
            )
            queue = playback_queue.queue
            self.assertEqual([t["id"] for t in queue.tracks], ["a", "b", "c"])
            self.assertEqual(queue.mode, "native_mpv")
            self.assertEqual(result["queue_summary"],
                             {"requested": 4, "queued": 3, "skipped": {"tidal_not_connected": 1}})
            self.meta_mock.assert_not_awaited()
        finally:
            self._restore(originals)

    async def test_skipped_start_entry_moves_to_the_next_playable_one(self):
        originals = self._install([base._track("x")], index=0)
        requests = []
        try:
            result = await self._play_with(
                requests, self._tidal_patches(connected=False),
                track_id="101", source="tidal", refs=["a", "tidal:101", "b", "c"],
            )
            queue = playback_queue.queue
            self.assertEqual(result["track"]["id"], "b")
            self.assertEqual(queue.tracks[queue.index]["id"], "b")
            self.assertEqual(requests[0].source, "local")
        finally:
            self._restore(originals)

    async def test_a_failing_tidal_status_check_counts_as_not_connected(self):
        originals = self._install([base._track("x")], index=0)
        requests = []

        class _Broken:
            async def is_authenticated(self):
                raise RuntimeError("tidalapi missing")

        try:
            patches = self._tidal_patches()
            patches[0] = patch.object(main, "_tidal_provider", return_value=_Broken())
            result = await self._play_with(
                requests, patches, track_id="a", refs=["a", "tidal:101", "b"],
            )
            self.assertEqual([t["id"] for t in playback_queue.queue.tracks], ["a", "b"])
            self.assertEqual(result["queue_summary"]["skipped"], {"tidal_not_connected": 1})
        finally:
            self._restore(originals)

    async def test_only_tidal_entries_and_not_connected_is_unauthorized(self):
        originals = self._install([base._track("x")], index=0)
        requests = []
        try:
            with self.assertRaises(main.HTTPException) as ctx:
                await self._play_with(
                    requests, self._tidal_patches(connected=False),
                    track_id="101", source="tidal", refs=["tidal:101", "tidal:102"],
                )
            self.assertEqual(ctx.exception.status_code, 401)
            self.assertEqual(requests, [])
            self.assertEqual([t["id"] for t in playback_queue.queue.tracks], ["x"])
        finally:
            self._restore(originals)

    async def test_unresolvable_tidal_and_missing_local_entries_are_counted(self):
        originals = self._install([base._track("x")], index=0)
        requests = []
        try:
            result = await self._play_with(
                requests, self._tidal_patches(unresolvable={"102"}),
                track_id="a", refs=["a", "tidal:101", "tidal:102", "gone", "b"],
            )
            queue = playback_queue.queue
            self.assertEqual([t["id"] for t in queue.tracks], ["a", "101", "b"])
            self.assertEqual(result["queue_summary"], {
                "requested": 5, "queued": 3, "skipped": {"unavailable": 1, "missing": 1},
            })
        finally:
            self._restore(originals)

    async def test_duplicate_refs_are_queued_once(self):
        originals = self._install([base._track("x")], index=0)
        requests = []
        try:
            await self._play_with(
                requests, self._tidal_patches(),
                track_id="a", refs=["a", "tidal:101", "tidal:101", "a", "b"],
            )
            self.assertEqual([t["id"] for t in playback_queue.queue.tracks], ["a", "101", "b"])
        finally:
            self._restore(originals)

    async def test_shuffle_fixes_the_current_entry_and_keeps_the_original_order(self):
        originals = self._install([base._track("x")], index=0)
        requests = []
        try:
            with patch.object(playback_queue.random, "shuffle", side_effect=lambda values: values.reverse()):
                await self._play_with(
                    requests, self._tidal_patches(),
                    track_id="b", refs=["a", "tidal:101", "b", "tidal:102"], shuffle=True,
                )
            queue = playback_queue.queue
            self.assertTrue(queue.shuffle)
            self.assertEqual([t["id"] for t in queue.original], ["a", "101", "b", "102"])
            self.assertEqual(queue.tracks[queue.index]["id"], "b")
            self.assertEqual(queue.index, 2)
            self.assertEqual([t["id"] for t in queue.tracks], ["102", "101", "b", "a"])
        finally:
            self._restore(originals)

    async def test_failed_transition_keeps_the_old_queue(self):
        queue_a = [base._track("a"), base._track("b"), base._track("c")]
        originals = self._install(queue_a, index=1, mode="app_replace")
        before = queue_state()
        requests = []

        async def fail(_request):
            raise PlaybackTransitionFailure("transition failed", transition_id="tr-x", stage="target-source-start")

        from contextlib import ExitStack
        try:
            with ExitStack() as stack:
                for patcher in self._tidal_patches():
                    stack.enter_context(patcher)
                stack.enter_context(self._patch_context(fail))
                with self.assertRaises(main.HTTPException):
                    await self._play(track_id="a", refs=["a", "tidal:101", "b"])
            self.assertEqual(queue_state(), before)
        finally:
            self._restore(originals)

    async def test_failing_start_stream_is_reported_not_swallowed(self):
        originals = self._install([base._track("x")], index=0)
        requests = []
        try:
            with self.assertRaises(main.HTTPException) as ctx:
                await self._play_with(
                    requests,
                    self._tidal_patches(start_error=main.HTTPException(status_code=404, detail="TIDAL track unavailable")),
                    track_id="101", source="tidal", refs=["tidal:101", "a"],
                )
            self.assertEqual(ctx.exception.status_code, 404)
            self.assertEqual(requests, [])
        finally:
            self._restore(originals)

    async def test_plain_local_and_tidal_requests_do_not_report_a_summary(self):
        originals = self._install([base._track("x")], index=0)
        requests = []
        try:
            from contextlib import ExitStack
            with ExitStack() as stack:
                stack.enter_context(self._patch_context(self._run(requests)))
                result = await main.play_track(main.PlayRequest(
                    source="local", track_id="a", queue_track_ids=["a", "b"]))
            self.assertNotIn("queue_summary", result)
        finally:
            self._restore(originals)


class AppQueueCandidateTests(unittest.TestCase):
    def test_single_entry_has_no_queue_and_loops_as_single_track(self):
        entry = _tidal_entry("1", with_url=True)
        candidate = playback_queue.app_queue_candidate([entry], 0, loop=True)
        self.assertEqual(candidate.queue, [])
        self.assertEqual(candidate.index, -1)
        self.assertTrue(candidate.single_track_loop)

    def test_same_id_in_two_sources_resolves_to_the_start_entry(self):
        local = {"id": "7", "source": "local", "url": "/m/7.flac"}
        tidal = _tidal_entry("7")
        start = dict(tidal, url="https://cdn/7.flac")
        candidate = playback_queue.app_queue_candidate([local, tidal], 1, track=start)
        self.assertEqual(candidate.index, 1)
        self.assertEqual(candidate.track["url"], "https://cdn/7.flac")

    def test_empty_entries_are_rejected(self):
        with self.assertRaises(ValueError):
            playback_queue.app_queue_candidate([], 0)


if __name__ == "__main__":
    unittest.main()
