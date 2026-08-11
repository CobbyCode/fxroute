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
        "single_track_loop",
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

    def _payload_shim(self, state):
        return {
            "playing": bool(state.get("playing")),
            "current_file": state.get("current_file"),
            "queue": main._queue_payload(),
        }

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

    async def test_native_shuffle_during_transition_is_rejected(self):
        queue_a = [_track(track_id) for track_id in ("a", "b", "c")]
        originals = self._install(queue_a, index=1, mode="native_mpv")
        coordinator = main.playback_transition_coordinator
        try:
            main.playback_transition_coordinator = SimpleNamespace(transition_active=True)
            with self.assertRaises(main.HTTPException) as ctx:
                await main._set_queue_shuffle(True)
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertEqual(main.playback_queue, queue_a)
            self.assertEqual(main.playback_queue_index, 1)
        finally:
            main.playback_transition_coordinator = coordinator
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
                self.assertEqual(
                    await main._advance_playback_queue(transition_reason="manual queue next"),
                    "advanced",
                )

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

    # -- Queue-Ende: terminaler Erfolgszustand statt clear-then-409 -------------

    async def test_manual_next_at_queue_end_commits_terminal_ended_state(self):
        queue_a = [_track("a"), _track("b")]
        originals = self._install(queue_a, index=1)
        try:
            main.player_instance.state.update({
                "current_file": "/music/b.flac",
                "paused": False,
                "playing": True,
                "ended": False,
                "position": 10.0,
            })
            state_before = dict(main.player_instance.state)

            async def unreachable(_request):
                self.fail("queue end must not start a transition")

            with self._patch_context(unreachable), patch.object(
                main, "build_playback_payload", self._payload_shim
            ):
                result = await main.next_playback()

            self.assertEqual(result["status"], "ok")
            self.assertIs(result["advanced"], False)
            self.assertIs(result["queue_ended"], True)
            self.assertEqual(main.playback_queue, [], "queue must be cleared")
            self.assertEqual(main.playback_queue_index, -1)
            self.assertEqual(
                main.current_track_info, _track("b"),
                "track context must survive the terminal end state",
            )
            self.assertEqual(
                main.player_instance.state, state_before,
                "the player must keep playing the last track",
            )
            self.assertEqual(
                result["playback"]["queue"]["index"], -1,
                "HTTP payload must expose the committed cleared queue state",
            )
            self.assertEqual(result["playback"]["queue"]["count"], 0)
            self.assertFalse(result["playback"]["queue"]["active"])
        finally:
            self._restore(originals)

    async def test_previous_at_queue_start_without_loop_is_409_without_mutation(self):
        queue_a = [_track("a"), _track("b")]
        originals = self._install(queue_a, index=0)
        try:
            async def unreachable(_request):
                self.fail("previous at queue start must not start a transition")

            with self._patch_context(unreachable):
                with self.assertRaises(main.HTTPException) as ctx:
                    await main.previous_playback()

            self.assertEqual(ctx.exception.status_code, 409)
            self.assertEqual(main.playback_queue, queue_a)
            self.assertEqual(main.playback_queue_index, 0)
            self.assertEqual(main.current_track_info, _track("a"))
        finally:
            self._restore(originals)

    async def test_previous_at_queue_start_with_loop_stays_409_without_mutation(self):
        queue_a = [_track("a"), _track("b")]
        originals = self._install(queue_a, index=0, loop=True)
        try:
            async def unreachable(_request):
                self.fail("previous at queue start must not start a transition")

            with self._patch_context(unreachable):
                with self.assertRaises(main.HTTPException) as ctx:
                    await main.previous_playback()

            self.assertEqual(ctx.exception.status_code, 409)
            self.assertEqual(main.playback_queue, queue_a)
            self.assertEqual(main.playback_queue_index, 0)
            self.assertEqual(main.current_track_info, _track("a"))
            self.assertTrue(main.playback_queue_loop)
        finally:
            self._restore(originals)

    async def test_next_at_queue_end_with_loop_wraps_to_first_track(self):
        queue_a = [_track("a"), _track("b")]
        originals = self._install(queue_a, index=1, loop=True)
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

            with self._patch_context(succeed), patch.object(
                main, "build_playback_payload", self._payload_shim
            ):
                result = await main.next_playback()

            self.assertEqual(result["status"], "playing")
            self.assertNotIn("queue_ended", result)
            self.assertEqual(main.playback_queue, queue_a)
            self.assertEqual(main.playback_queue_index, 0)
            self.assertEqual(main.current_track_info, _track("a"))
        finally:
            self._restore(originals)

    async def test_manual_next_shuffle_wrap_api_contract(self):
        queue_a = [_track(track_id) for track_id in ("a", "b", "c", "d")]
        originals = self._install(queue_a, index=3, shuffle=True)
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

            with self._patch_context(succeed, reverse_shuffle=True), patch.object(
                main, "build_playback_payload", self._payload_shim
            ):
                result = await main.next_playback()

            self.assertEqual(result["status"], "playing")
            self.assertNotIn("queue_ended", result)
            self.assertEqual(
                [item["id"] for item in main.playback_queue],
                ["d", "c", "b", "a"],
            )
            self.assertEqual(main.playback_queue_index, 1)
            self.assertTrue(main.playback_queue_shuffle)
            self.assertEqual(main.current_track_info, _track("c"))
        finally:
            self._restore(originals)

    async def test_native_next_at_queue_end_without_loop_is_409_without_mutation(self):
        queue_a = [_track(track_id, rate=48000) for track_id in ("a", "b", "c")]
        originals = self._install(queue_a, index=2, mode="native_mpv")
        try:
            async def unreachable(_request):
                self.fail("native queue end must not start a transition")

            with self._patch_context(unreachable):
                with self.assertRaises(main.HTTPException) as ctx:
                    await main.next_playback()

            self.assertEqual(ctx.exception.status_code, 409)
            self.assertEqual(main.playback_queue, queue_a)
            self.assertEqual(main.playback_queue_index, 2)
            self.assertEqual(main.playback_queue_mode, "native_mpv")
            self.assertEqual(main.current_track_info, _track("c", rate=48000))
        finally:
            self._restore(originals)

    async def test_native_previous_at_queue_start_is_409_without_mutation(self):
        queue_a = [_track(track_id, rate=48000) for track_id in ("a", "b", "c")]
        originals = self._install(queue_a, index=0, mode="native_mpv")
        try:
            async def unreachable(_request):
                self.fail("native previous at queue start must not start a transition")

            with self._patch_context(unreachable):
                with self.assertRaises(main.HTTPException) as ctx:
                    await main.previous_playback()

            self.assertEqual(ctx.exception.status_code, 409)
            self.assertEqual(main.playback_queue, queue_a)
            self.assertEqual(main.playback_queue_index, 0)
            self.assertEqual(main.playback_queue_mode, "native_mpv")
            self.assertEqual(main.current_track_info, _track("a", rate=48000))
        finally:
            self._restore(originals)

    async def test_next_coordinator_failure_is_500_and_never_ended(self):
        queue_a = [_track("a"), _track("b")]
        originals = self._install(queue_a, index=0)
        try:
            async def fail(_request):
                raise PlaybackTransitionFailure(
                    "navigation failed", transition_id="tr-test", stage="target-source-start"
                )

            with self._patch_context(fail):
                with self.assertRaises(main.HTTPException) as ctx:
                    await main.next_playback()

            self.assertEqual(ctx.exception.status_code, 500)
            self.assertEqual(main.playback_queue, queue_a)
            self.assertEqual(main.playback_queue_index, 0)
            self.assertEqual(main.current_track_info, _track("a"))
        finally:
            self._restore(originals)


if __name__ == "__main__":
    unittest.main()
