#!/usr/bin/env python3
"""Focused snapshot/abort contracts for failed MPV transitions."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import playback_queue
import main
from playback_queue_test_support import queue_state, restore_queue_state
from playback_transition_test_support import make_transition_runtime
from playback_transition import TransitionRequest


class PlayerDouble:
    _running = True

    def __init__(self, current_file: str | None, *, playing: bool) -> None:
        self._state = {
            "current_file": current_file,
            "playing": playing,
            "paused": not playing,
            "ended": False,
            "volume": 73,
        }
        self.set_volume = Mock(side_effect=self._set_volume)
        self.stop_playback = Mock(side_effect=self._stop)
        self.set_pause = Mock(side_effect=self._set_pause)

    @property
    def state(self) -> dict:
        return self._state

    def _set_volume(self, volume: int) -> None:
        self._state["volume"] = volume

    def _set_pause(self, paused: bool) -> None:
        self._state["paused"] = bool(paused)
        self._state["playing"] = not bool(paused) and bool(self._state.get("current_file"))

    def _stop(self) -> None:
        self._state.update({
            "current_file": None,
            "playing": False,
            "paused": False,
            "ended": False,
        })


def local_request(url: str) -> TransitionRequest:
    return TransitionRequest(
        operation="play",
        source="local",
        target_rate=48_000,
        target_url=url,
        target_track={"source": "local", "url": url},
        should_play=True,
        rate_change=True,
        reload_source=True,
    )


def _track_entry(track_id: str) -> dict:
    return {
        "id": track_id,
        "source": "local",
        "url": f"/music/{track_id}.flac",
        "title": track_id,
        "sample_rate_hz": 48000,
    }


class FailedTransitionAbortTests(unittest.IsolatedAsyncioTestCase):
    async def test_staged_target_is_stopped_but_committed_queue_preserved(self):
        player = PlayerDouble("/music/new.flac", playing=True)
        current = {"source": "local", "url": "/music/new.flac", "id": "new"}
        retry = dict(current)
        queue = [dict(current), {"source": "local", "url": "/music/next.flac", "id": "next"}]
        saved_queue = queue_state()
        try:
            playback_queue.queue.tracks = [dict(track) for track in queue]
            playback_queue.queue.original = [dict(track) for track in queue]
            playback_queue.queue.index = 0
            playback_queue.queue.mode = "app_replace"
            with patch.object(main, "player_instance", player), patch.object(
                main, "current_track_info", current
            ), patch.object(main, "last_track_info", retry), patch.object(
                main, "current_footer_owner", "local"
            ), patch.object(main, "_mark_player_state_authoritative"):
                await make_transition_runtime().abort_failed_transition(
                    local_request("/music/new.flac"),
                    {
                        "player": {"current_file": "/music/old.flac"},
                        "current_track": {"source": "local", "url": "/music/old.flac"},
                    },
                    target_staged=True,
                )

                # A failed transition must never discard the previously working
                # committed queue; only the active track context is invalidated.
                self.assertIsNone(main.current_track_info)
                self.assertEqual(main.last_track_info, retry)
                self.assertEqual(playback_queue.queue.tracks, queue)
                self.assertEqual(playback_queue.queue.mode, "app_replace")
        finally:
            restore_queue_state(saved_queue)

        player.stop_playback.assert_called_once_with()
        self.assertIsNone(player.state["current_file"])
        self.assertFalse(player.state["playing"])
        self.assertEqual(player.state["volume"], 0)

    async def test_staged_native_failure_normalizes_mode_but_keeps_queue(self):
        player = PlayerDouble("/music/new.flac", playing=True)
        current = {"source": "local", "url": "/music/new.flac", "id": "new"}
        queue = [_track_entry("a"), _track_entry("b"), _track_entry("c")]
        saved_queue = queue_state()
        try:
            playback_queue.queue.tracks = [dict(track) for track in queue]
            playback_queue.queue.original = [dict(track) for track in queue]
            playback_queue.queue.index = 1
            playback_queue.queue.mode = "native_mpv"
            with patch.object(main, "player_instance", player), patch.object(
                main, "current_track_info", dict(queue[1])
            ), patch.object(main, "last_track_info", dict(queue[1])), patch.object(
                main, "current_footer_owner", "local"
            ), patch.object(
                playback_queue.queue, "reduce_native_playlist_to_current"
            ), patch.object(
                playback_queue.queue, "reset_mpv_loop_state"
            ), patch.object(main, "_mark_player_state_authoritative"):
                await make_transition_runtime().abort_failed_transition(
                    local_request("/music/new.flac"),
                    {
                        "player": {"current_file": "/music/old.flac"},
                        "current_track": {"source": "local", "url": "/music/old.flac"},
                    },
                    target_staged=True,
                )

                # The staged failure replaced MPV's playlist, so the retained
                # committed queue must not keep a false native_mpv ownership
                # claim; the app-owned mode keeps it navigable via transitions.
                self.assertEqual(playback_queue.queue.tracks, queue)
                self.assertEqual(playback_queue.queue.index, 1)
                self.assertEqual(playback_queue.queue.mode, "app_replace")
                self.assertIsNone(main.current_track_info)
        finally:
            restore_queue_state(saved_queue)

    async def test_staged_native_failure_with_broken_cleanup_still_normalizes_mode(self):
        player = PlayerDouble("/music/new.flac", playing=True)
        current = {"source": "local", "url": "/music/new.flac", "id": "new"}
        queue = [_track_entry("a"), _track_entry("b"), _track_entry("c")]
        navigation_requests = []
        saved_queue = queue_state()
        try:
            playback_queue.queue.tracks = [dict(track) for track in queue]
            playback_queue.queue.original = [dict(track) for track in queue]
            playback_queue.queue.index = 1
            playback_queue.queue.mode = "native_mpv"
            with patch.object(main, "player_instance", player), patch.object(
                main, "current_track_info", dict(queue[1])
            ), patch.object(main, "last_track_info", dict(queue[1])), patch.object(
                main, "current_footer_owner", "local"
            ), patch.object(
                playback_queue.queue, "reduce_native_playlist_to_current",
                side_effect=RuntimeError("IPC communication failed: timed out"),
            ), patch.object(
                playback_queue.queue, "reset_mpv_loop_state"
            ), patch.object(main, "_mark_player_state_authoritative"):
                await make_transition_runtime().abort_failed_transition(
                    local_request("/music/new.flac"),
                    {
                        "player": {"current_file": "/music/old.flac"},
                        "current_track": {"source": "local", "url": "/music/old.flac"},
                    },
                    target_staged=True,
                )

                # Cleanup failure must not leave a false native_mpv ownership
                # claim over an untrusted playlist: the retained queue data and
                # index stay, the mode is normalized to app-owned navigation.
                self.assertEqual(playback_queue.queue.tracks, queue)
                self.assertEqual(playback_queue.queue.index, 1)
                self.assertEqual(playback_queue.queue.mode, "app_replace")
                self.assertIsNone(main.current_track_info)

                # Subsequent queue navigation must use app-owned coordinator
                # navigation, never a false MPV-native jump.
                async def navigate(request):
                    navigation_requests.append(request)
                    return SimpleNamespace(target_rate=48000, committed=True)

                with patch.object(main, "_run_coordinated_transition", navigate), \
                     patch.object(main, "_sample_rate_policy_is_auto", return_value=False), \
                     patch.object(main, "_record_local_track_started", lambda *_a, **_k: None):
                    self.assertTrue(await playback_queue.queue.load_track(2, transition_reason="queue navigation"))
                self.assertEqual(len(navigation_requests), 1)
                request = navigation_requests[0]
                self.assertIsNone(request.native_queue_jump, "no MPV-native jump may be issued")
                self.assertTrue(request.reload_source)
                self.assertEqual(playback_queue.queue.index, 2)
        finally:
            restore_queue_state(saved_queue)

    async def test_play_failure_before_staging_keeps_committed_queue(self):
        player = PlayerDouble("/music/old.flac", playing=True)
        current = {"source": "local", "url": "/music/old.flac", "id": "old"}
        queue = [dict(current), {"source": "local", "url": "/music/next.flac", "id": "next"}]
        saved_queue = queue_state()
        try:
            playback_queue.queue.tracks = [dict(track) for track in queue]
            playback_queue.queue.mode = "app_replace"
            with patch.object(main, "player_instance", player), patch.object(
                main, "current_track_info", current
            ), patch.object(main, "_mark_player_state_authoritative"):
                request = TransitionRequest(
                    operation="play",
                    source="local",
                    target_rate=48_000,
                    target_url="/music/other.flac",
                    target_track={"source": "local", "url": "/music/other.flac"},
                    should_play=True,
                    rate_change=True,
                    reload_source=True,
                )
                await make_transition_runtime().abort_failed_transition(
                    request,
                    {
                        "player": {"current_file": "/music/old.flac"},
                        "current_track": dict(current),
                    },
                    target_staged=False,
                )

                self.assertEqual(main.current_track_info, current)
                self.assertEqual(playback_queue.queue.tracks, queue)
        finally:
            restore_queue_state(saved_queue)

        player.stop_playback.assert_not_called()

    async def test_unchanged_committed_context_is_kept_for_retry(self):
        player = PlayerDouble("/music/old.flac", playing=True)
        current = {"source": "local", "url": "/music/old.flac", "id": "old"}
        queue = [dict(current), {"source": "local", "url": "/music/next.flac", "id": "next"}]
        saved_queue = queue_state()
        try:
            playback_queue.queue.tracks = [dict(track) for track in queue]
            playback_queue.queue.mode = "app_replace"
            with patch.object(main, "player_instance", player), patch.object(
                main, "current_track_info", current
            ), patch.object(main, "_mark_player_state_authoritative"):
                request = TransitionRequest(
                    operation="recovery",
                    source="local",
                    target_rate=48_000,
                    target_url="/music/old.flac",
                    target_track=dict(current),
                    should_play=True,
                    rate_change=False,
                    reload_source=False,
                )
                await make_transition_runtime().abort_failed_transition(
                    request,
                    {
                        "player": {"current_file": "/music/old.flac"},
                        "current_track": dict(current),
                    },
                    target_staged=False,
                )

                self.assertEqual(main.current_track_info, current)
                self.assertEqual(playback_queue.queue.tracks, queue)
        finally:
            restore_queue_state(saved_queue)

        player.stop_playback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
