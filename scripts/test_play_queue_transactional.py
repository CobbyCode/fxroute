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
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import playback.queue as playback_queue
import main
from playback.queue import QueueCandidate
from playback_queue_test_support import queue_state, restore_queue_state
from playback.transition import PlaybackTransitionFailure


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
        # Mirrors the MPV-side native playlist length for the direct-selection
        # readiness gate (None = unknown, e.g. never probed).
        self.playlist_count: int | None = None

    def get_property(self, name):
        if name == "playlist-count":
            return self.playlist_count
        return None

    def set_playlist_pos(self, index: int):
        self.state["playlist_pos"] = index
        self.state["current_file"] = f"/music/{chr(ord('a') + index)}.flac"


class _Station:
    def __init__(self, station_id: str) -> None:
        self.id = station_id
        self.name = f"Station {station_id}"
        self.stream_url = f"https://stream.example/{station_id}"


class _MpvPlaylistPlayer:
    """MPV double with a live playlist model for native-queue reorder tests."""

    _running = True

    def __init__(self, playlist_ids: list[str]) -> None:
        self.playlist = list(playlist_ids)
        self.pos = 0
        self.state = {
            "current_file": f"/music/{playlist_ids[0]}.flac" if playlist_ids else None,
            "paused": False,
            "playing": bool(playlist_ids),
            "ended": False,
            "position": 1.0,
            "volume": 100,
        }

    def get_property(self, name):
        if name == "playlist-count":
            return len(self.playlist)
        if name == "playlist":
            return [{"filename": f"/music/{track_id}.flac"} for track_id in self.playlist]
        return None

    def set_playlist_pos(self, index: int):
        self.pos = index
        self.state["playlist_pos"] = index

    def move_playlist_entry(self, old_index: int, new_index: int):
        entry = self.playlist.pop(old_index)
        # True MPV semantics (verified): the entry lands before the entry
        # that originally occupied new_index.
        self.playlist.insert(new_index - 1 if new_index > old_index else new_index, entry)

    def set_loop_playlist(self, enabled):
        pass


class PlayQueueTransactionalTests(unittest.IsolatedAsyncioTestCase):
    GLOBALS = (
        "player_instance", "music_library", "current_track_info",
        "last_track_info", "last_radio_track_info", "current_playback_owner",
    )

    def _install(self, queue_a: list[dict], *, index: int, mode: str = "app_replace",
                 loop: bool = False, shuffle: bool = False) -> dict:
        originals = {name: (getattr(main.runtime, name) if hasattr(main.runtime, name) else getattr(main.playback_state, name) if hasattr(main.playback_state, name) else getattr(main, name)) for name in self.GLOBALS}
        self._saved_queue = queue_state()
        main.runtime.player_instance = _FakePlayer()
        main.runtime.music_library.scanner = _Scanner(["a", "b", "c", "d"])
        main.playback_state.current_track_info = dict(queue_a[index]) if queue_a and index >= 0 else None
        main.playback_state.last_track_info = dict(queue_a[index]) if queue_a and index >= 0 else None
        main.playback_state.last_radio_track_info = None
        main.playback_state.current_playback_owner = "local"
        playback_queue.queue.tracks = [dict(track) for track in queue_a]
        playback_queue.queue.original = [dict(track) for track in queue_a]
        playback_queue.queue.index = index
        playback_queue.queue.mode = mode
        playback_queue.queue.loop = loop
        playback_queue.queue.shuffle = shuffle
        playback_queue.queue.single_track_loop = False
        return originals

    def _restore(self, originals: dict) -> None:
        restore_queue_state(self._saved_queue)
        for name, value in originals.items():
            setattr(main.runtime if hasattr(main.runtime, name) else main.playback_state if hasattr(main.playback_state, name) else main, name, value)

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
        self.assertEqual(playback_queue.queue.tracks, queue_a)
        self.assertEqual(playback_queue.queue.original, queue_a)
        self.assertEqual(playback_queue.queue.index, index)
        self.assertEqual(playback_queue.queue.mode, mode)
        self.assertEqual(playback_queue.queue.loop, loop)
        self.assertEqual(playback_queue.queue.shuffle, shuffle)

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
            with patch.object(playback_queue.random, "shuffle", side_effect=lambda values: values.reverse()):
                with self._patch_context(self._failure()):
                    with self.assertRaises(main.HTTPException) as ctx:
                        await self._play(track_id="d", queue_track_ids=["c", "d", "a"])
        finally:
            self._assert_queue_a_intact(queue_a, index=1, mode="app_replace", loop=True, shuffle=False)
            self.assertEqual(main.playback_state.current_track_info, _track("b"))
            self.assertEqual(main.playback_state.last_track_info, _track("b"))
            self.assertEqual(ctx.exception.status_code, 500)
            self._restore(originals)

    async def test_failed_play_with_shuffle_keeps_queue_a_unchanged(self):
        queue_a = [_track("a"), _track("b"), _track("c"), _track("d")]
        originals = self._install(queue_a, index=2, mode="app_replace", shuffle=True)
        try:
            with patch.object(playback_queue.random, "shuffle", side_effect=lambda values: values.reverse()):
                with self._patch_context(self._failure(stage="commit-readback")):
                    with self.assertRaises(main.HTTPException):
                        await self._play(track_id="b", queue_track_ids=["c", "a", "b"], shuffle=True)
        finally:
            self._assert_queue_a_intact(queue_a, index=2, mode="app_replace", loop=False, shuffle=True)
            self.assertEqual(playback_queue.queue.index, 2)
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
                main.runtime.player_instance.state.update({
                    "current_file": request.target_url,
                    "paused": False,
                    "playing": True,
                    "ended": False,
                    "position": 1.0,
                })
                return SimpleNamespace(target_rate=request.target_rate, committed=True)

            with patch.object(playback_queue.queue, "reduce_native_playlist_to_current", side_effect=reduce), \
                 patch.object(playback_queue.queue, "reset_mpv_loop_state", side_effect=reset), \
                 patch.object(main, "_sample_rate_policy_is_auto", return_value=False), \
                 self._patch_context(self._failure()):
                with self.assertRaises(main.HTTPException):
                    await self._play(track_id="d")

            # The failed play must not trim MPV's transport playlist: the
            # committed native queue and its transport stay consistent.
            self.assertEqual(reduce_calls, [], "transport playlist must stay intact until the commit")
            self.assertEqual(reset_calls, [])
            self._assert_queue_a_intact(queue_a, index=1, mode="native_mpv", loop=False, shuffle=False)

            # The retained queue is navigated directly by MPV.
            with self._patch_context(navigate):
                self.assertTrue(await playback_queue.queue.load_track(2, transition_reason="queue navigation"))
            self.assertEqual(playback_queue.queue.index, 2)
            self.assertEqual(len(navigation_requests), 0)
            self.assertEqual(main.runtime.player_instance.state["playlist_pos"], 2)
        finally:
            self._restore(originals)

    async def test_play_click_in_same_native_queue_uses_direct_mpv_navigation(self):
        queue_a = [_track("a", rate=48000), _track("b", rate=48000), _track("c", rate=48000)]
        originals = self._install(queue_a, index=0, mode="native_mpv")
        try:
            main.runtime.player_instance.state["current_file"] = "/music/a.flac"
            main.runtime.player_instance.state["playing"] = True
            main.runtime.player_instance.state["paused"] = False
            # Healthy MPV side: the mirrored native playlist is loaded.
            main.runtime.player_instance.playlist_count = 3
            with self._patch_context(lambda _request: self.fail("Coordinator must not run")), patch.object(
                main.runtime.music_library.scanner, "get_tracks"
            ) as scan:
                result = await self._play(track_id="c", queue_track_ids=["a", "b", "c"])

            scan.assert_not_called()
            self.assertEqual(result["track"]["id"], "c")
            self.assertEqual(playback_queue.queue.index, 2)
            self.assertEqual(main.runtime.player_instance.state["playlist_pos"], 2)
        finally:
            self._restore(originals)

    async def test_play_click_in_same_native_queue_after_spotify_takeover_runs_coordinator(self):
        # Regression (.104): library queue active -> Spotify takes over and
        # stops MPV (empty playlist) -> back to the library, starting a track
        # from the same queue ids must take the regular coordinator path so
        # the track is really loaded and the owner is taken over. The direct
        # set_playlist_pos fast path against the stale/empty MPV playlist
        # returned 200 without any playback and left the owner on spotify.
        queue_a = [_track("a"), _track("b"), _track("c")]
        originals = self._install(queue_a, index=0, mode="native_mpv")
        requests = []
        try:
            # Spotify takeover: owner moved away, MPV stopped with no file
            # loaded and an empty playlist.
            main.playback_state.current_playback_owner = "spotify"
            main.runtime.player_instance.state["current_file"] = None
            main.runtime.player_instance.state["playing"] = False
            main.runtime.player_instance.state["paused"] = False
            main.runtime.player_instance.playlist_count = 0

            async def succeed(request):
                requests.append(request)
                main.runtime.player_instance.state.update({
                    "current_file": request.target_url,
                    "paused": False,
                    "playing": True,
                    "ended": False,
                    "position": 1.0,
                })
                return SimpleNamespace(
                    target_rate=request.target_rate,
                    committed=True,
                    transition_id="tr-test-takeover",
                )

            with self._patch_context(succeed):
                result = await self._play(track_id="b", queue_track_ids=["a", "b", "c"])

            self.assertEqual(len(requests), 1, "the coordinated transition must run")
            self.assertEqual(requests[0].source, "local")
            self.assertEqual(requests[0].target_url, "/music/b.flac")
            self.assertNotIn(
                "playlist_pos",
                main.runtime.player_instance.state,
                "the stale-playlist fast path must not run",
            )
            self.assertEqual(result["status"], "playing")
            self.assertEqual(result["track"]["id"], "b")
            self.assertEqual(playback_queue.queue.index, 1)
            self.assertEqual(main.playback_state.current_track_info["id"], "b")
            self.assertEqual(main.playback_state.current_playback_owner, "local")
        finally:
            self._restore(originals)

    async def test_successful_play_commits_queue_b_exactly_once(self):
        queue_a = [_track("a"), _track("b"), _track("c")]
        originals = self._install(queue_a, index=0)
        commits = []
        real_commit = playback_queue.queue.commit
        try:
            async def succeed(request):
                main.runtime.player_instance.state.update({
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
                playback_queue.queue, "commit", side_effect=recording_commit
            ):
                result = await self._play(track_id="b", queue_track_ids=["d", "b", "a"])

            self.assertEqual(result["status"], "playing")
            self.assertEqual(len(commits), 1, "the candidate must be committed exactly once")
            self.assertEqual(
                [item["id"] for item in playback_queue.queue.tracks],
                ["d", "b", "a"],
            )
            self.assertEqual(playback_queue.queue.index, 1)
            self.assertEqual(playback_queue.queue.original, [_track("d"), _track("b"), _track("a")])
            self.assertEqual(main.playback_state.current_track_info["id"], "b")
        finally:
            self._restore(originals)

    async def test_local_album_play_keeps_full_queue_and_selected_native_index(self):
        originals = self._install([_track("a"), _track("b"), _track("c")], index=0)
        requests = []
        try:
            main.runtime.music_library.scanner = _Scanner(["a", "b", "c", "d", "e", "f"])

            async def succeed(request):
                requests.append(request)
                main.runtime.player_instance.state.update({
                    "current_file": request.target_url,
                    "paused": False,
                    "playing": True,
                    "ended": False,
                    "position": 1.0,
                })
                return SimpleNamespace(target_rate=request.target_rate, committed=True)

            with self._patch_context(succeed):
                await self._play(
                    track_id="d",
                    queue_track_ids=["a", "b", "c", "d", "e", "f"],
                )

            self.assertEqual([item["id"] for item in playback_queue.queue.tracks],
                             ["a", "b", "c", "d", "e", "f"])
            self.assertEqual(playback_queue.queue.index, 3)
            self.assertEqual(main.playback_state.current_track_info["id"], "d")
            self.assertEqual([item["id"] for item in requests[0].native_queue],
                             ["a", "b", "c", "d", "e", "f"])
            self.assertEqual(requests[0].native_queue_index, 3)
            self.assertEqual(requests[0].target_url, "/music/d.flac")
        finally:
            self._restore(originals)

    async def test_native_candidate_b_commits_once_with_mpv_mode(self):
        queue_a = [_track("a", rate=44100), _track("b", rate=44100)]
        originals = self._install(queue_a, index=0, mode="app_replace")
        requests = []
        commits = []
        real_commit = playback_queue.queue.commit
        try:
            async def succeed(request):
                requests.append(request)
                main.runtime.player_instance.state.update({
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
                playback_queue.queue, "commit", side_effect=recording_commit
            ):
                await self._play(track_id="c", queue_track_ids=["c", "d"])

            request = requests[0]
            self.assertEqual(
                [item["id"] for item in request.native_queue],
                ["c", "d"],
                "the prepared candidate snapshot is carried by the request",
            )
            self.assertEqual(len(commits), 1)
            self.assertEqual(playback_queue.queue.mode, "native_mpv")
            self.assertEqual([item["id"] for item in playback_queue.queue.tracks], ["c", "d"])
            self.assertEqual(playback_queue.queue.index, 0)
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
                main.runtime.player_instance.state.update({
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
            self.assertEqual(playback_queue.queue.tracks, [])
            self.assertEqual(playback_queue.queue.original, [])
            self.assertEqual(playback_queue.queue.index, -1)
            self.assertEqual(playback_queue.queue.mode, "app_replace")
            self.assertFalse(playback_queue.queue.shuffle)
            self.assertFalse(playback_queue.queue.loop)
            self.assertEqual(main.playback_state.current_track_info["id"], "radio_s1")
        finally:
            self._restore(originals)

    async def test_same_queue_play_keeps_active_shuffle_despite_stale_ui_flag(self):
        # Regression: a same-queue album click adopts the existing queue
        # (preserve, no reshuffle) and must then also adopt the committed
        # shuffle state instead of a possibly stale UI flag.  Shuffle on/off
        # belongs to the dedicated toggle endpoint.
        queue_a = [_track("a"), _track("b"), _track("c")]
        originals = self._install(queue_a, index=0, mode="native_mpv")
        try:
            main.playback_state.current_playback_owner = "local"
            playback_queue.queue.shuffle = True

            async def succeed(request):
                main.runtime.player_instance.state.update({
                    "current_file": request.target_url,
                    "paused": False,
                    "playing": True,
                    "ended": False,
                    "position": 1.0,
                })
                return SimpleNamespace(
                    target_rate=request.target_rate,
                    committed=True,
                    transition_id="tr-test",
                )

            with self._patch_context(succeed):
                # Stale UI flag off while the committed queue is shuffled.
                result = await self._play(track_id="b", queue_track_ids=["a", "b", "c"])

            self.assertEqual(result["track"]["id"], "b")
            self.assertEqual([item["id"] for item in playback_queue.queue.tracks], ["a", "b", "c"])
            self.assertEqual(playback_queue.queue.index, 1)
            self.assertTrue(playback_queue.queue.shuffle)
            self.assertEqual(main.playback_state.current_track_info["id"], "b")
            self.assertEqual(main.playback_state.current_playback_owner, "local")
            self.assertNotIn("playlist_pos", main.runtime.player_instance.state)
        finally:
            self._restore(originals)

    async def test_same_queue_play_keeps_shuffle_off_despite_stale_ui_flag(self):
        # Mirror case: a stale UI flag on must not enable shuffle on a
        # same-queue play that keeps the existing order.
        queue_a = [_track("a"), _track("b"), _track("c")]
        originals = self._install(queue_a, index=0, mode="native_mpv")
        try:
            main.playback_state.current_playback_owner = "local"
            playback_queue.queue.shuffle = False

            async def succeed(request):
                main.runtime.player_instance.state.update({
                    "current_file": request.target_url,
                    "paused": False,
                    "playing": True,
                    "ended": False,
                    "position": 1.0,
                })
                return SimpleNamespace(
                    target_rate=request.target_rate,
                    committed=True,
                    transition_id="tr-test",
                )

            with self._patch_context(succeed):
                result = await self._play(track_id="b", queue_track_ids=["a", "b", "c"], shuffle=True)

            self.assertEqual(result["track"]["id"], "b")
            self.assertEqual([item["id"] for item in playback_queue.queue.tracks], ["a", "b", "c"])
            self.assertFalse(playback_queue.queue.shuffle)
            self.assertEqual(main.playback_state.current_playback_owner, "local")
        finally:
            self._restore(originals)

    def test_prepare_without_active_shuffle_keeps_requested_flag(self):
        # Legacy contract (e.g. the selection sync path passes no active
        # state): without active_shuffle the requested flag is honored.
        candidate = playback_queue.queue.prepare_local_queue(
            "a", ["a", "b", "c"], shuffle=True, loop=False, reshuffle=False,
            tracks=_Scanner(["a", "b", "c"]).get_tracks(),
        )
        self.assertTrue(candidate.shuffle)
        self.assertEqual([item["id"] for item in candidate.queue], ["a", "b", "c"])
        candidate = playback_queue.queue.prepare_local_queue(
            "a", ["a", "b", "c"], shuffle=False, loop=False, reshuffle=False,
            tracks=_Scanner(["a", "b", "c"]).get_tracks(),
        )
        self.assertFalse(candidate.shuffle)

    def _patch_context(self, transition, *, radio_stations=None):
        from contextlib import ExitStack

        stack = ExitStack()
        for patcher in self._patches(transition, radio_stations=radio_stations):
            stack.enter_context(patcher)
        return stack

    async def test_library_shuffle_with_spotify_owner_sets_local_queue_only(self):
        # Regression: an explicit library shuffle request must set the local
        # queue state even while Spotify owns playback. Routing it to the
        # owner toggled Spotify shuffle instead (ignoring `enabled`) while
        # the local queue and its footer display stayed untouched.
        queue_a = [_track("a"), _track("b"), _track("c")]
        originals = self._install(queue_a, index=0, mode="native_mpv")
        try:
            player = _MpvPlaylistPlayer(["a", "b", "c"])
            main.runtime.player_instance = player
            main.playback_state.current_playback_owner = "spotify"
            main.playback_state.current_track_info = None
            main.playback_state.last_track_info = None

            def _shuffle_request(enabled):
                request = SimpleNamespace()

                async def _json():
                    return {"enabled": enabled}

                request.json = _json
                return request

            with patch.object(
                main, "spotify_shuffle_toggle",
                new=AsyncMock(side_effect=AssertionError("must not route to Spotify")),
            ), patch.object(
                main, "broadcast_spotify_state",
                new=AsyncMock(side_effect=AssertionError("must not broadcast Spotify state")),
            ), patch.object(main, "get_output_volume_safe", return_value=100):
                result = await main.set_playback_shuffle(_shuffle_request(True))

            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["shuffle"])
            self.assertTrue(result["playback"]["queue"]["shuffle"])
            self.assertTrue(playback_queue.queue.shuffle)
            self.assertEqual(playback_queue.queue.tracks[0]["id"], "a")
            self.assertEqual(
                sorted(item["id"] for item in playback_queue.queue.tracks), ["a", "b", "c"]
            )
            self.assertEqual(
                player.playlist, [item["id"] for item in playback_queue.queue.tracks]
            )

            with patch.object(
                main, "spotify_shuffle_toggle",
                new=AsyncMock(side_effect=AssertionError("must not route to Spotify")),
            ), patch.object(
                main, "broadcast_spotify_state",
                new=AsyncMock(side_effect=AssertionError("must not broadcast Spotify state")),
            ), patch.object(main, "get_output_volume_safe", return_value=100):
                result = await main.set_playback_shuffle(_shuffle_request(False))

            self.assertEqual(result["status"], "ok")
            self.assertFalse(result["shuffle"])
            self.assertFalse(result["playback"]["queue"]["shuffle"])
            self.assertFalse(playback_queue.queue.shuffle)
            self.assertEqual(
                [item["id"] for item in playback_queue.queue.tracks], ["a", "b", "c"]
            )
            self.assertEqual(player.playlist, ["a", "b", "c"])
        finally:
            self._restore(originals)


class QueueSelectionTransactionalTests(unittest.IsolatedAsyncioTestCase):
    """/api/playback/selection: prepare candidate, commit it, keep track dict."""

    GLOBALS = (
        "player_instance", "music_library", "current_track_info",
        "last_track_info", "current_playback_owner",
    )

    def _install(self, queue_a: list[dict], *, index: int, mode: str = "app_replace",
                 shuffle: bool = False) -> dict:
        originals = {name: (getattr(main.runtime, name) if hasattr(main.runtime, name) else getattr(main.playback_state, name) if hasattr(main.playback_state, name) else getattr(main, name)) for name in self.GLOBALS}
        self._saved_queue = queue_state()
        main.runtime.player_instance = _FakePlayer()
        main.runtime.music_library.scanner = _Scanner(["a", "b", "c", "d"])
        main.runtime.player_instance.state["current_file"] = queue_a[index]["url"]
        main.playback_state.current_track_info = dict(queue_a[index])
        main.playback_state.last_track_info = dict(queue_a[index])
        main.playback_state.current_playback_owner = "local"
        playback_queue.queue.tracks = [dict(track) for track in queue_a]
        playback_queue.queue.original = [dict(track) for track in queue_a]
        playback_queue.queue.index = index
        playback_queue.queue.mode = mode
        playback_queue.queue.loop = False
        playback_queue.queue.shuffle = shuffle
        playback_queue.queue.single_track_loop = False
        return originals

    def _restore(self, originals: dict) -> None:
        restore_queue_state(self._saved_queue)
        for name, value in originals.items():
            setattr(main.runtime if hasattr(main.runtime, name) else main.playback_state if hasattr(main.playback_state, name) else main, name, value)

    async def test_selection_commits_prepared_queue_and_track_dict(self):
        queue_a = [_track("a"), _track("b"), _track("c")]
        originals = self._install(queue_a, index=1)
        try:
            payload = playback_queue.queue.sync_active_local_queue_selection(
                ["c", "b", "a"], shuffle=True, loop=True
            )

            self.assertEqual(
                [item["id"] for item in playback_queue.queue.tracks],
                ["c", "b", "a"],
                "the prepared selection becomes the committed queue",
            )
            self.assertEqual(
                playback_queue.queue.original,
                [_track("c"), _track("b"), _track("a")],
            )
            self.assertEqual(playback_queue.queue.index, 1)
            self.assertEqual(playback_queue.queue.mode, "app_replace")
            self.assertTrue(playback_queue.queue.shuffle)
            self.assertTrue(playback_queue.queue.loop)
            self.assertFalse(playback_queue.queue.single_track_loop)

            self.assertEqual(main.playback_state.current_track_info["id"], "b")
            self.assertEqual(main.playback_state.last_track_info["id"], "b")
            self.assertIsInstance(main.playback_state.current_track_info, dict)
            self.assertNotIsInstance(main.playback_state.current_track_info, QueueCandidate)
            self.assertEqual(main.playback_state.current_track_info.get("url"), "/music/b.flac")
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

            with patch.object(playback_queue.queue, "reduce_native_playlist_to_current", side_effect=reduce), \
                 patch.object(playback_queue.queue, "reset_mpv_loop_state", side_effect=reset):
                payload = playback_queue.queue.sync_active_local_queue_selection(
                    ["b", "a"], shuffle=False, loop=False
                )

            self.assertEqual(reduce_calls, [True])
            self.assertGreaterEqual(len(reset_calls), 1)
            self.assertEqual(playback_queue.queue.mode, "app_replace")
            self.assertEqual([item["id"] for item in playback_queue.queue.tracks], ["b", "a"])
            self.assertEqual(playback_queue.queue.index, 1)
            self.assertEqual(main.playback_state.current_track_info["id"], "a")
            self.assertIsInstance(main.playback_state.current_track_info, dict)
        finally:
            self._restore(originals)


if __name__ == "__main__":
    unittest.main()
