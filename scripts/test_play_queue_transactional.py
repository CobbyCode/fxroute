#!/usr/bin/env python3
"""/api/play must commit the queue state only after a committed transition.

The active queue stays untouched (order, index, mode, shuffle, loop) while a
new play request is prepared and executed; the prepared candidate is
published exactly once after the playback transition committed.
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


def _track(track_id: str, *, rate: int = 44100) -> dict:
    return {
        "id": track_id,
        "source": "local",
        "url": f"/music/{track_id}.flac",
        "title": track_id,
        "artist": "Test",
        "sample_rate_hz": rate,
    }


class _ScannerTrack:
    def __init__(self, track_id: str, *, rate: int = 44100) -> None:
        self.id = track_id
        self._rate = rate

    def to_dict(self) -> dict:
        return _track(self.id, rate=self._rate)


class _Scanner:
    def __init__(self, track_ids: list[str], *, rate: int = 44100) -> None:
        self._tracks = [_ScannerTrack(track_id, rate=rate) for track_id in track_ids]

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


class _Station:
    def __init__(self, station_id: str) -> None:
        self.id = station_id
        self.name = f"Station {station_id}"
        self.stream_url = f"https://stream.example/{station_id}"


class PlayQueueTransactionalTests(unittest.IsolatedAsyncioTestCase):
    GLOBALS = (
        "player_instance", "library_scanner", "current_track_info",
        "last_track_info", "last_radio_track_info", "current_footer_owner",
        "playback_queue", "playback_queue_original", "playback_queue_index",
        "playback_queue_mode", "playback_queue_loop", "playback_queue_shuffle",
        "single_track_loop",
    )

    def _install(self, queue_a: list[dict], *, index: int, mode: str = "app_replace",
                 loop: bool = False, shuffle: bool = False) -> dict:
        originals = {name: getattr(main, name) for name in self.GLOBALS}
        main.player_instance = _FakePlayer()
        main.library_scanner = _Scanner(["a", "b", "c", "d"])
        main.current_track_info = dict(queue_a[index]) if queue_a and index >= 0 else None
        main.last_track_info = dict(queue_a[index]) if queue_a and index >= 0 else None
        main.last_radio_track_info = None
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

    def _patches(self, transition, *, radio_stations: list[_Station] | None = None):
        async def no_op(*_args, **_kwargs):
            return None

        def no_op_sync(*_args, **_kwargs):
            return None

        return [
            patch.object(main, "_can_send_play_command", return_value=True),
            patch.object(main, "_run_coordinated_transition", transition),
            patch.object(main, "_record_local_track_started", no_op_sync),
            patch.object(main, "build_playback_payload", lambda state: {}),
        ] + ([patch.object(main, "get_stations", return_value=radio_stations)] if radio_stations is not None else [])

    def _play(self, *, track_id: str, queue_track_ids=None, shuffle: bool = False,
              loop: bool = False, source: str = "local"):
        return main.play_track(main.PlayRequest(
            source=source,
            track_id=track_id,
            queue_track_ids=queue_track_ids,
            shuffle=shuffle,
            loop=loop,
        ))

    def _assert_queue_a_intact(self, queue_a: list[dict], *, index: int, mode: str,
                               loop: bool, shuffle: bool) -> None:
        self.assertEqual(main.playback_queue, queue_a)
        self.assertEqual(main.playback_queue_original, queue_a)
        self.assertEqual(main.playback_queue_index, index)
        self.assertEqual(main.playback_queue_mode, mode)
        self.assertEqual(main.playback_queue_loop, loop)
        self.assertEqual(main.playback_queue_shuffle, shuffle)

    def _failure(self, message: str = "transition failed", *, stage: str = "target-source-start"):
        async def fail(_request):
            raise PlaybackTransitionFailure(
                message, transition_id="tr-test", stage=stage
            )
        return fail

    async def test_failed_play_keeps_queue_a_unchanged(self):
        queue_a = [_track("a"), _track("b"), _track("c")]
        originals = self._install(queue_a, index=1, mode="app_replace", loop=True)
        try:
            with patch.object(main.random, "shuffle", side_effect=lambda values: values.reverse()):
                with self._patch_context(self._failure()):
                    with self.assertRaises(main.HTTPException) as ctx:
                        await self._play(track_id="d", queue_track_ids=["c", "d", "a"])
        finally:
            self._assert_queue_a_intact(queue_a, index=1, mode="app_replace", loop=True, shuffle=False)
            self.assertEqual(main.current_track_info, _track("b"))
            self.assertEqual(main.last_track_info, _track("b"))
            self.assertEqual(ctx.exception.status_code, 500)
            self._restore(originals)

    async def test_failed_play_with_shuffle_keeps_queue_a_unchanged(self):
        queue_a = [_track("a"), _track("b"), _track("c"), _track("d")]
        originals = self._install(queue_a, index=2, mode="app_replace", shuffle=True)
        try:
            with patch.object(main.random, "shuffle", side_effect=lambda values: values.reverse()):
                with self._patch_context(self._failure(stage="commit-readback")):
                    with self.assertRaises(main.HTTPException):
                        await self._play(track_id="b", queue_track_ids=["c", "a", "b"], shuffle=True)
        finally:
            self._assert_queue_a_intact(queue_a, index=2, mode="app_replace", loop=False, shuffle=True)
            self.assertEqual(main.playback_queue_index, 2)
            self._restore(originals)

    async def test_failed_play_from_native_queue_keeps_transport_queue_navigable(self):
        queue_a = [_track("a", rate=48000), _track("b", rate=48000), _track("c", rate=48000)]
        originals = self._install(queue_a, index=1, mode="native_mpv")
        reduce_calls = []
        reset_calls = []
        navigation_requests = []
        try:
            def reduce():
                reduce_calls.append(True)

            def reset():
                reset_calls.append(True)

            async def navigate(request):
                navigation_requests.append(request)
                main.player_instance.state.update({
                    "current_file": request.target_url,
                    "paused": False,
                    "playing": True,
                    "ended": False,
                    "position": 1.0,
                })
                return SimpleNamespace(target_rate=request.target_rate, committed=True)

            with patch.object(main, "_reduce_native_mpv_playlist_to_current", side_effect=reduce), \
                 patch.object(main, "_reset_mpv_loop_state", side_effect=reset), \
                 patch.object(main, "_sample_rate_policy_is_auto", return_value=False), \
                 self._patch_context(self._failure()):
                with self.assertRaises(main.HTTPException):
                    await self._play(track_id="d")

            # The failed play must not trim MPV's transport playlist: the
            # committed native queue and its transport stay consistent.
            self.assertEqual(reduce_calls, [], "transport playlist must stay intact until the commit")
            self.assertEqual(reset_calls, [])
            self._assert_queue_a_intact(queue_a, index=1, mode="native_mpv", loop=False, shuffle=False)

            # The retained queue must remain functionally navigable: a native
            # queue jump still commits and updates the index.
            with self._patch_context(navigate):
                self.assertTrue(await main._load_queue_track(2, transition_reason="queue navigation"))
            self.assertEqual(main.playback_queue_index, 2)
            self.assertEqual(len(navigation_requests), 1)
            self.assertTrue(navigation_requests[0].native_queue)
            self.assertEqual(navigation_requests[0].native_queue_jump, 2)
        finally:
            self._restore(originals)

    async def test_successful_play_commits_queue_b_exactly_once(self):
        queue_a = [_track("a"), _track("b"), _track("c")]
        originals = self._install(queue_a, index=0)
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

            with self._patch_context(succeed), patch.object(
                main, "_commit_queue_state", side_effect=recording_commit
            ):
                result = await self._play(track_id="b", queue_track_ids=["d", "b", "a"])

            self.assertEqual(result["status"], "playing")
            self.assertEqual(len(commits), 1, "the candidate must be committed exactly once")
            self.assertEqual(
                [item["id"] for item in main.playback_queue],
                ["d", "b", "a"],
            )
            self.assertEqual(main.playback_queue_index, 1)
            self.assertEqual(main.playback_queue_original, [_track("d"), _track("b"), _track("a")])
            self.assertEqual(main.current_track_info["id"], "b")
        finally:
            self._restore(originals)

    async def test_native_candidate_b_commits_once_with_mpv_mode(self):
        queue_a = [_track("a", rate=44100), _track("b", rate=44100)]
        originals = self._install(queue_a, index=0, mode="app_replace")
        requests = []
        commits = []
        real_commit = main._commit_queue_state
        try:
            async def succeed(request):
                requests.append(request)
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

            with self._patch_context(succeed), patch.object(
                main, "_commit_queue_state", side_effect=recording_commit
            ):
                await self._play(track_id="c", queue_track_ids=["c", "d"])

            request = requests[0]
            self.assertEqual(
                [item["id"] for item in request.native_queue],
                ["c", "d"],
                "the prepared candidate snapshot is carried by the request",
            )
            self.assertEqual(len(commits), 1)
            self.assertEqual(main.playback_queue_mode, "native_mpv")
            self.assertEqual([item["id"] for item in main.playback_queue], ["c", "d"])
            self.assertEqual(main.playback_queue_index, 0)
        finally:
            self._restore(originals)

    async def test_uncommitted_transition_result_is_failure_and_keeps_queue_a(self):
        queue_a = [_track("a"), _track("b"), _track("c")]
        originals = self._install(queue_a, index=1)
        try:
            async def uncommitted(_request):
                return SimpleNamespace(target_rate=48000, committed=False)

            with self._patch_context(uncommitted):
                with self.assertRaises(main.HTTPException) as ctx:
                    await self._play(track_id="d")
        finally:
            self.assertEqual(ctx.exception.status_code, 500)
            self._assert_queue_a_intact(queue_a, index=1, mode="app_replace", loop=False, shuffle=False)
            self._restore(originals)

    async def test_exception_during_playback_start_keeps_queue_a(self):
        queue_a = [_track("a"), _track("b"), _track("c")]
        originals = self._install(queue_a, index=0)
        try:
            async def boom(_request):
                raise RuntimeError("source start exploded")

            with self._patch_context(boom):
                with self.assertRaises(RuntimeError):
                    await self._play(track_id="d")
        finally:
            self._assert_queue_a_intact(queue_a, index=0, mode="app_replace", loop=False, shuffle=False)
            self._restore(originals)

    async def test_radio_unknown_station_keeps_queue_a(self):
        queue_a = [_track("a"), _track("b")]
        originals = self._install(queue_a, index=1)
        try:
            with self._patch_context(self._failure(), radio_stations=[_Station("s1")]):
                with self.assertRaises(main.HTTPException) as ctx:
                    await self._play(track_id="s2", source="radio")
        finally:
            self.assertEqual(ctx.exception.status_code, 404)
            self._assert_queue_a_intact(queue_a, index=1, mode="app_replace", loop=False, shuffle=False)
            self._restore(originals)

    async def test_radio_success_commits_cleared_queue(self):
        queue_a = [_track("a"), _track("b")]
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
                return SimpleNamespace(target_rate=44100, committed=True)

            with self._patch_context(succeed, radio_stations=[_Station("s1")]):
                result = await self._play(track_id="s1", source="radio")

            self.assertEqual(result["status"], "playing")
            self.assertEqual(main.playback_queue, [])
            self.assertEqual(main.playback_queue_original, [])
            self.assertEqual(main.playback_queue_index, -1)
            self.assertEqual(main.playback_queue_mode, "app_replace")
            self.assertFalse(main.playback_queue_shuffle)
            self.assertFalse(main.playback_queue_loop)
            self.assertEqual(main.current_track_info["id"], "radio_s1")
        finally:
            self._restore(originals)

    def _patch_context(self, transition, *, radio_stations=None):
        from contextlib import ExitStack

        stack = ExitStack()
        for patcher in self._patches(transition, radio_stations=radio_stations):
            stack.enter_context(patcher)
        return stack


class QueueSelectionTransactionalTests(unittest.IsolatedAsyncioTestCase):
    """/api/playback/selection: prepare candidate, commit it, keep track dict."""

    GLOBALS = (
        "player_instance", "library_scanner", "current_track_info",
        "last_track_info", "current_footer_owner",
        "playback_queue", "playback_queue_original", "playback_queue_index",
        "playback_queue_mode", "playback_queue_loop", "playback_queue_shuffle",
        "single_track_loop",
    )

    def _install(self, queue_a: list[dict], *, index: int, mode: str = "app_replace",
                 shuffle: bool = False) -> dict:
        originals = {name: getattr(main, name) for name in self.GLOBALS}
        main.player_instance = _FakePlayer()
        main.library_scanner = _Scanner(["a", "b", "c", "d"])
        main.player_instance.state["current_file"] = queue_a[index]["url"]
        main.current_track_info = dict(queue_a[index])
        main.last_track_info = dict(queue_a[index])
        main.current_footer_owner = "local"
        main.playback_queue = [dict(track) for track in queue_a]
        main.playback_queue_original = [dict(track) for track in queue_a]
        main.playback_queue_index = index
        main.playback_queue_mode = mode
        main.playback_queue_loop = False
        main.playback_queue_shuffle = shuffle
        main.single_track_loop = False
        return originals

    def _restore(self, originals: dict) -> None:
        for name, value in originals.items():
            setattr(main, name, value)

    async def test_selection_commits_prepared_queue_and_track_dict(self):
        queue_a = [_track("a"), _track("b"), _track("c")]
        originals = self._install(queue_a, index=1)
        try:
            payload = main._sync_active_local_queue_selection(
                ["c", "b", "a"], shuffle=True, loop=True
            )

            self.assertEqual(
                [item["id"] for item in main.playback_queue],
                ["c", "b", "a"],
                "the prepared selection becomes the committed queue",
            )
            self.assertEqual(
                main.playback_queue_original,
                [_track("c"), _track("b"), _track("a")],
            )
            self.assertEqual(main.playback_queue_index, 1)
            self.assertEqual(main.playback_queue_mode, "app_replace")
            self.assertTrue(main.playback_queue_shuffle)
            self.assertTrue(main.playback_queue_loop)
            self.assertFalse(main.single_track_loop)

            self.assertEqual(main.current_track_info["id"], "b")
            self.assertEqual(main.last_track_info["id"], "b")
            self.assertIsInstance(main.current_track_info, dict)
            self.assertNotIsInstance(main.current_track_info, main._QueueCandidate)
            self.assertEqual(main.current_track_info.get("url"), "/music/b.flac")
            self.assertEqual(payload["queue"]["index"], 1)
            self.assertEqual(payload["queue"]["mode"], "app_replace")
            self.assertEqual(payload["queue"]["count"], 3)
            self.assertIs(payload["queue"]["shuffle"], True)
            self.assertIs(payload["queue"]["loop"], True)
        finally:
            self._restore(originals)

    async def test_selection_from_native_queue_becomes_app_owned(self):
        queue_a = [_track("a", rate=48000), _track("b", rate=48000)]
        originals = self._install(queue_a, index=0, mode="native_mpv")
        reduce_calls = []
        reset_calls = []
        try:
            def reduce():
                reduce_calls.append(True)

            def reset():
                reset_calls.append(True)

            with patch.object(main, "_reduce_native_mpv_playlist_to_current", side_effect=reduce), \
                 patch.object(main, "_reset_mpv_loop_state", side_effect=reset):
                payload = main._sync_active_local_queue_selection(
                    ["b", "a"], shuffle=False, loop=False
                )

            self.assertEqual(reduce_calls, [True], "old native future entries are trimmed")
            self.assertGreaterEqual(len(reset_calls), 1)
            self.assertEqual(main.playback_queue_mode, "app_replace")
            self.assertEqual([item["id"] for item in main.playback_queue], ["b", "a"])
            self.assertEqual(main.playback_queue_index, 1)
            self.assertEqual(main.current_track_info["id"], "a")
            self.assertIsInstance(main.current_track_info, dict)
        finally:
            self._restore(originals)


if __name__ == "__main__":
    unittest.main()
