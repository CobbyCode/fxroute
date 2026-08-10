#!/usr/bin/env python3
"""Transactional boundaries for queue navigation and shuffle state.

The prepared queue state (order, index, shuffle/loop flags, track context) is
published only after a committed transition; failure or an uncommitted
outcome leaves the previously committed state fully intact.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from playback_transition import PlaybackTransitionFailure


def _track(track_id: str, *, rate: int = 48000) -> dict:
    return {
        "id": track_id,
        "source": "local",
        "url": f"/music/{track_id}.flac",
        "title": track_id,
        "artist": "Test",
        "sample_rate_hz": rate,
    }


class _ScannerTrack:
    def __init__(self, track_id: str) -> None:
        self.id = track_id

    def to_dict(self) -> dict:
        return _track(self.id)


class _Scanner:
    def __init__(self, track_ids: list[str]) -> None:
        self._tracks = [_ScannerTrack(track_id) for track_id in track_ids]

    def get_tracks(self) -> list[_ScannerTrack]:
        return list(self._tracks)


class _FakePlayer:
    _running = True

    def __init__(self) -> None:
        self.state = {
            "current_file": None,
            "paused": False,
            "playing": False,
            "ended": False,
            "position": 0.0,
            "volume": 100,
        }


class QueueNavigationTransactionalTests(unittest.IsolatedAsyncioTestCase):
    GLOBALS = (
        "player_instance", "library_scanner", "current_track_info",
        "last_track_info", "current_footer_owner",
        "playback_queue", "playback_queue_original", "playback_queue_index",
        "playback_queue_mode", "playback_queue_loop", "playback_queue_shuffle",
        "single_track_loop", "queue_transition_target_url",
    )

    def _install(self, queue_a: list[dict], *, index: int, mode: str = "app_replace",
                 loop: bool = False, shuffle: bool = False) -> dict:
        originals = {name: getattr(main, name) for name in self.GLOBALS}
        main.player_instance = _FakePlayer()
        main.library_scanner = _Scanner(["a", "b", "c", "d"])
        main.current_track_info = dict(queue_a[index])
        main.last_track_info = dict(queue_a[index])
        main.current_footer_owner = "local"
        main.playback_queue = [dict(track) for track in queue_a]
        main.playback_queue_original = [dict(track) for track in queue_a]
        main.playback_queue_index = index
        main.playback_queue_mode = mode
        main.playback_queue_loop = loop
        main.playback_queue_shuffle = shuffle
        main.single_track_loop = False
        main.queue_transition_target_url = None
        return originals

    def _restore(self, originals: dict) -> None:
        for name, value in originals.items():
            setattr(main, name, value)

    def _patch_context(self, transition, *, reverse_shuffle: bool = False):
        from contextlib import ExitStack

        stack = ExitStack()
        stack.enter_context(patch.object(main, "_run_coordinated_transition", transition))
        stack.enter_context(patch.object(main, "_record_local_track_started", lambda *_a, **_k: None))
        stack.enter_context(patch.object(main, "_sample_rate_policy_is_auto", return_value=False))
        if reverse_shuffle:
            stack.enter_context(patch.object(main.random, "shuffle", side_effect=lambda values: values.reverse()))
        return stack

    async def test_load_queue_track_uncommitted_keeps_index_and_track(self):
        queue_a = [_track("a"), _track("b"), _track("c")]
        originals = self._install(queue_a, index=0)
        try:
            async def uncommitted(_request):
                return SimpleNamespace(target_rate=48000, committed=False)

            with self._patch_context(uncommitted):
                with self.assertRaises(main.HTTPException) as ctx:
                    await main._load_queue_track(1, transition_reason="queue navigation")
        finally:
            self.assertEqual(ctx.exception.status_code, 500)
            self.assertEqual(main.playback_queue_index, 0, "index must stay on the old track")
            self.assertEqual(main.current_track_info, _track("a"))
            self.assertEqual(main.playback_queue, queue_a)
            self._restore(originals)

    async def test_load_queue_track_failure_keeps_index_and_track(self):
        queue_a = [_track("a"), _track("b"), _track("c")]
        originals = self._install(queue_a, index=0)
        try:
            async def fail(_request):
                raise PlaybackTransitionFailure(
                    "navigation failed", transition_id="tr-test", stage="target-source-start"
                )

            with self._patch_context(fail):
                with self.assertRaises(main.HTTPException) as ctx:
                    await main._load_queue_track(1, transition_reason="queue navigation")
        finally:
            self.assertEqual(ctx.exception.status_code, 500)
            self.assertEqual(main.playback_queue_index, 0)
            self.assertEqual(main.current_track_info, _track("a"))
            self.assertEqual(main.playback_queue, queue_a)
            self._restore(originals)

    async def test_load_queue_track_success_commits_index_once(self):
        queue_a = [_track("a"), _track("b"), _track("c")]
        originals = self._install(queue_a, index=0)
        try:
            async def succeed(request):
                main.player_instance.state.update({
                    "current_file": request.target_url,
                    "paused": False,
                    "playing": True,
                    "ended": False,
                    "position": 1.0,
                })
                return SimpleNamespace(target_rate=request.target_rate, committed=True)

            with self._patch_context(succeed):
                self.assertTrue(await main._load_queue_track(1, transition_reason="queue navigation"))

            self.assertEqual(main.playback_queue_index, 1)
            self.assertEqual(main.current_track_info, _track("b"))
            self.assertEqual(main.last_track_info, _track("b"))
            self.assertEqual(main.playback_queue, queue_a)
        finally:
            self._restore(originals)

    async def test_native_shuffle_uncommitted_keeps_queue(self):
        queue_a = [_track(track_id) for track_id in ("a", "b", "c", "d")]
        originals = self._install(queue_a, index=1, mode="native_mpv")
        try:
            async def uncommitted(_request):
                return SimpleNamespace(target_rate=48000, committed=False)

            with self._patch_context(uncommitted, reverse_shuffle=True):
                with self.assertRaises(main.HTTPException) as ctx:
                    await main._set_queue_shuffle(True)
        finally:
            self.assertEqual(ctx.exception.status_code, 500)
            self.assertEqual(main.playback_queue, queue_a, "queue order must stay intact")
            self.assertEqual(main.playback_queue_index, 1)
            self.assertFalse(main.playback_queue_shuffle)
            self.assertEqual(main.current_track_info, _track("b"))
            self._restore(originals)

    async def test_native_shuffle_failure_keeps_queue(self):
        queue_a = [_track(track_id) for track_id in ("a", "b", "c", "d")]
        originals = self._install(queue_a, index=1, mode="native_mpv")
        try:
            async def fail(_request):
                raise PlaybackTransitionFailure(
                    "shuffle failed", transition_id="tr-test", stage="target-source-start"
                )

            with self._patch_context(fail, reverse_shuffle=True):
                with self.assertRaises(PlaybackTransitionFailure):
                    await main._set_queue_shuffle(True)
        finally:
            self.assertEqual(main.playback_queue, queue_a)
            self.assertEqual(main.playback_queue_index, 1)
            self.assertFalse(main.playback_queue_shuffle)
            self.assertEqual(main.current_track_info, _track("b"))
            self._restore(originals)

    async def test_native_shuffle_success_commits_prepared_queue(self):
        queue_a = [_track(track_id) for track_id in ("a", "b", "c", "d")]
        originals = self._install(queue_a, index=1, mode="native_mpv")
        try:
            async def succeed(request):
                self.assertEqual([item["id"] for item in request.native_queue], ["b", "d", "c", "a"])
                main.player_instance.state.update({
                    "current_file": request.target_url,
                    "paused": False,
                    "playing": True,
                    "ended": False,
                    "position": 1.0,
                })
                return SimpleNamespace(target_rate=request.target_rate, committed=True)

            with self._patch_context(succeed, reverse_shuffle=True):
                self.assertTrue(await main._set_queue_shuffle(True))

            self.assertEqual([item["id"] for item in main.playback_queue], ["b", "d", "c", "a"])
            self.assertEqual(main.playback_queue_index, 0)
            self.assertTrue(main.playback_queue_shuffle)
            self.assertEqual(main.current_track_info["id"], "b")
            self.assertEqual(main.last_track_info["id"], "b")
        finally:
            self._restore(originals)

    async def test_manual_shuffle_wrap_failure_keeps_queue_order_and_index(self):
        queue_a = [_track(track_id) for track_id in ("a", "b", "c", "d")]
        originals = self._install(queue_a, index=3, shuffle=True)
        try:
            async def fail(_request):
                raise PlaybackTransitionFailure(
                    "wrap transition failed", transition_id="tr-test", stage="target-source-start"
                )

            with self._patch_context(fail, reverse_shuffle=True):
                with self.assertRaises(main.HTTPException):
                    await main._advance_playback_queue(transition_reason="manual queue next")
        finally:
            self.assertEqual(
                [item["id"] for item in main.playback_queue],
                ["a", "b", "c", "d"],
                "the committed queue order must survive a failed wrap",
            )
            self.assertEqual(main.playback_queue_index, 3)
            self.assertTrue(main.playback_queue_shuffle)
            self.assertEqual(main.current_track_info, _track("d"))
            self._restore(originals)

    async def test_manual_shuffle_wrap_success_commits_shuffled_queue_once(self):
        queue_a = [_track(track_id) for track_id in ("a", "b", "c", "d")]
        originals = self._install(queue_a, index=3, shuffle=True)
        commits = []
        real_commit = main._commit_queue_state
        try:
            async def succeed(request):
                main.player_instance.state.update({
                    "current_file": request.target_url,
                    "paused": False,
                    "playing": True,
                    "ended": False,
                    "position": 1.0,
                })
                return SimpleNamespace(target_rate=request.target_rate, committed=True)

            def recording_commit(candidate):
                commits.append(candidate)
                real_commit(candidate)

            from contextlib import ExitStack

            with ExitStack() as stack:
                for patcher in (
                    patch.object(main, "_run_coordinated_transition", succeed),
                    patch.object(main, "_record_local_track_started", lambda *_a, **_k: None),
                    patch.object(main, "_sample_rate_policy_is_auto", return_value=False),
                    patch.object(main.random, "shuffle", side_effect=lambda values: values.reverse()),
                    patch.object(main, "_commit_queue_state", side_effect=recording_commit),
                ):
                    stack.enter_context(patcher)
                self.assertTrue(await main._advance_playback_queue(transition_reason="manual queue next"))

            self.assertEqual(len(commits), 1, "the prepared wrap queue is committed exactly once")
            self.assertEqual(
                [item["id"] for item in main.playback_queue],
                ["d", "c", "b", "a"],
            )
            self.assertEqual(main.playback_queue_index, 1)
            self.assertTrue(main.playback_queue_shuffle)
            self.assertEqual(main.current_track_info, _track("c"))
            self.assertEqual(main.last_track_info, _track("c"))
        finally:
            self._restore(originals)


if __name__ == "__main__":
    unittest.main()
