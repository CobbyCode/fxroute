#!/usr/bin/env python3
"""An unresolvable TIDAL queue entry must not wipe the committed queue.

load_track() returning False maps to advance() "unavailable", whose contract
is "no navigation was possible and no authoritative queue state was changed".
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playback.queue import PlaybackQueue, PlaybackQueueDependencies


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


if __name__ == "__main__":
    unittest.main()
