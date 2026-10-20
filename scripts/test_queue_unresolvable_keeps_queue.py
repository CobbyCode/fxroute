#!/usr/bin/env python3
"""An unresolvable TIDAL queue entry must not wipe the committed queue.

load_track() returning False maps to advance() "unavailable", whose contract
is "no navigation was possible and no authoritative queue state was changed".
The automatic advance (skip_unplayable) steps over such entries and continues
with the next playable one; a manual advance still stops at the first.
"""
import dataclasses
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException

from playback.queue import PlaybackQueue, PlaybackQueueDependencies
import playback.queue as playback_queue_module


def _deps(resolve_stream_url):
    return PlaybackQueueDependencies(
        player=lambda: None,
        run_transition=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not start a transition")),
        commit_coordinated_track=lambda *a, **k: None,
        get_current_track_info=lambda: None,
        set_track_context=lambda *a, **k: None,
        transition_is_active=lambda: False,
        player_is_running=lambda *a, **k: True,
        wait_for_player_current_file=None,
        coordinator_target_rate=lambda *a, **k: 48000,
        coordinator_rate_change=lambda *a: False,
        sample_rate_policy_is_auto=lambda: True,
        transition_error_http=lambda e: e,
        get_tracks=lambda: [],
        build_playback_payload=lambda *a, **k: {},
        resolve_stream_url=resolve_stream_url,
    )


class UnresolvableKeepsQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_tidal_unresolvable_keeps_queue(self):
        async def resolve_none(track):
            return None

        queue = PlaybackQueue(_deps(resolve_none))
        before = [
            {"id": "t1", "source": "tidal", "url": "http://x/1"},
            {"id": "t2", "source": "tidal", "url": ""},
        ]
        queue.tracks = [dict(t) for t in before]
        queue.original = [dict(t) for t in before]
        queue.index = 0
        queue.mode = "app_replace"

        self.assertFalse(await queue.load_track(1, transition_reason="test"))
        self.assertEqual(queue.tracks, before)
        self.assertEqual(queue.index, 0)
        self.assertEqual(queue.mode, "app_replace")
        self.assertEqual(await queue.advance(transition_reason="test"), "unavailable")
        self.assertEqual(queue.tracks, before)

    async def test_empty_url_keeps_queue(self):
        queue = PlaybackQueue(_deps(None))
        before = [
            {"id": "a", "source": "local", "url": "/music/a.flac"},
            {"id": "b", "source": "local", "url": ""},
        ]
        queue.tracks = [dict(t) for t in before]
        queue.original = [dict(t) for t in before]
        queue.index = 0

        self.assertFalse(await queue.load_track(1, transition_reason="test"))
        self.assertEqual(queue.tracks, before)
        self.assertEqual(queue.index, 0)


class AutoAdvanceSkipsUnplayableTests(unittest.IsolatedAsyncioTestCase):
    def _queue(self, dead, count, *, loop=False, index=0, on_resolve=None):
        """Queue of ``count`` TIDAL entries ``t0..``; ids in ``dead`` do not resolve."""
        self.resolved = []
        self.transitions = []
        self.committed = []

        async def resolve(track):
            self.resolved.append(track["id"])
            if on_resolve is not None:
                on_resolve(track["id"])
            if track["id"] in dead:
                return None
            track["url"] = f"https://cdn/{track['id']}.flac"
            return track["url"]

        async def run_transition(request):
            self.transitions.append(request.target_track["id"])
            return SimpleNamespace(committed=True, target_rate=44100, transition_id="tr")

        deps = dataclasses.replace(
            _deps(resolve),
            run_transition=run_transition,
            commit_coordinated_track=lambda track, **kwargs: self.committed.append(track["id"]),
        )
        queue = PlaybackQueue(deps)
        queue.tracks = [{"id": f"t{i}", "source": "tidal", "url": ""} for i in range(count)]
        queue.original = [dict(track) for track in queue.tracks]
        queue.index = index
        queue.mode = "app_replace"
        queue.loop = loop
        return queue

    async def test_unplayable_entry_is_skipped(self):
        queue = self._queue({"t1", "t2"}, 5)
        self.assertEqual(await queue.advance(transition_reason="test", skip_unplayable=True), "advanced")
        self.assertEqual(self.resolved, ["t1", "t2", "t3"])
        self.assertEqual(self.transitions, ["t3"])
        self.assertEqual(queue.index, 3)
        self.assertEqual(self.committed, ["t3"])

    async def test_manual_advance_still_stops_at_the_unplayable_entry(self):
        queue = self._queue({"t1"}, 3)
        self.assertEqual(await queue.advance(transition_reason="test"), "unavailable")
        self.assertEqual(self.resolved, ["t1"])
        self.assertEqual(self.transitions, [])
        self.assertEqual(queue.index, 0)

    async def test_unplayable_tail_keeps_the_queue(self):
        queue = self._queue({"t1", "t2"}, 3)
        before = [dict(track) for track in queue.tracks]
        self.assertEqual(await queue.advance(transition_reason="test", skip_unplayable=True), "unavailable")
        self.assertEqual(self.transitions, [])
        self.assertEqual((queue.tracks, queue.index), (before, 0))

    async def test_regular_queue_end_is_unchanged(self):
        queue = self._queue(set(), 3, index=2)
        self.assertEqual(await queue.advance(transition_reason="test", skip_unplayable=True), "ended")
        self.assertEqual(queue.tracks, [])
        self.assertEqual(self.resolved, [])

    async def test_loop_wraps_over_unplayable_entries(self):
        queue = self._queue({"t3", "t0"}, 4, loop=True, index=2)
        self.assertEqual(await queue.advance(transition_reason="test", skip_unplayable=True), "advanced")
        self.assertEqual(self.resolved, ["t3", "t0", "t1"])
        self.assertEqual(queue.index, 1)

    async def test_loop_over_only_unplayable_entries_ends_the_attempt(self):
        queue = self._queue({"t0", "t1", "t2"}, 3, loop=True)
        self.assertEqual(await queue.advance(transition_reason="test", skip_unplayable=True), "unavailable")
        self.assertEqual(sorted(self.resolved), ["t0", "t1", "t2"])
        self.assertEqual(self.transitions, [])

    async def test_skips_are_bounded(self):
        count = playback_queue_module._MAX_UNPLAYABLE_SKIPS + 5
        queue = self._queue({f"t{i}" for i in range(1, count - 1)}, count)
        self.assertEqual(await queue.advance(transition_reason="test", skip_unplayable=True), "unavailable")
        self.assertEqual(len(self.resolved), playback_queue_module._MAX_UNPLAYABLE_SKIPS)
        self.assertEqual(queue.index, 0)

    async def test_a_queue_change_while_skipping_stops_the_advance(self):
        def user_starts_something_else(track_id):
            queue._edit_generation += 1

        queue = self._queue({"t1"}, 4, on_resolve=user_starts_something_else)
        with self.assertRaises(HTTPException) as ctx:
            await queue.advance(transition_reason="test", skip_unplayable=True)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self.resolved, ["t1"])
        self.assertEqual(self.transitions, [])


if __name__ == "__main__":
    unittest.main()
