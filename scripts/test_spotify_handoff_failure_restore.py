#!/usr/bin/env python3
"""Focused tests for the P1-2 fix: a failed Spotify handoff must not destroy
the previously committed Local/Radio playback context.

The Spotify quiet stage stops MPV and clears current_track_info before the
Spotify start is verified (pause_local_playback_for_spotify_broadcast).  When
the Spotify start or verify fails, abort_failed_transition now restores the
pre-transition Local/Radio context from the Coordinator snapshot so a failed
Spotify start never loses both sources.  The queue, last_track_info and the
radio-reconnect state are preserved.  A successful Spotify commit keeps its
existing semantics (old source context is given up, Spotify takes over).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

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
            "volume": 0,
        }

    @property
    def state(self) -> dict:
        return self._state


def spotify_request() -> TransitionRequest:
    return TransitionRequest(
        operation="spotify-play",
        source="spotify",
        target_rate=48_000,
        should_play=True,
        rate_change=True,
        reload_source=True,
        detail="api-spotify-play",
    )


def local_track() -> dict:
    return {
        "source": "local",
        "url": "/music/old.flac",
        "id": "old",
        "title": "Old Track",
        "sample_rate_hz": 48000,
    }


def radio_track() -> dict:
    return {
        "source": "radio",
        "url": "https://radio.example/live",
        "id": "radio_1",
        "title": "Radio One",
        "sample_rate_hz": 44100,
    }


def snapshot_with(track: dict, *, current_file: str | None, playing: bool = True) -> dict:
    return {
        "player": {"current_file": current_file, "playing": playing, "paused": not playing},
        "current_track": dict(track),
    }


class SpotifyHandoffFailureRestoreTests(unittest.IsolatedAsyncioTestCase):
    """abort_failed_transition restores the pre-transition Local/Radio context."""

    async def test_local_spotify_failure_restores_local_context_and_keeps_queue(self):
        player = PlayerDouble(None, playing=False)  # state after the quiet stage
        track = local_track()
        retry = {"source": "local", "url": "/music/retry.flac", "id": "retry"}
        queue = [dict(track), {"source": "local", "url": "/music/next.flac", "id": "next"}]
        mark_authoritative = Mock()
        saved_queue = queue_state()
        try:
            playback_queue.queue.tracks = [dict(item) for item in queue]
            playback_queue.queue.original = [dict(item) for item in queue]
            playback_queue.queue.index = 0
            playback_queue.queue.mode = "app_replace"
            with patch.object(
                main.FxrouteTransitionRuntime,
                "_restore_committed_source_after_failed_transition",
                AsyncMock(return_value=True),
            ), patch.object(main, "player_instance", player), patch.object(
                main, "current_track_info", None
            ), patch.object(main, "last_track_info", dict(retry)), patch.object(
                main, "current_footer_owner", "spotify"
            ), patch.object(main, "_mark_player_state_authoritative", mark_authoritative):
                await make_transition_runtime().abort_failed_transition(
                    spotify_request(),
                    snapshot_with(track, current_file="/music/old.flac"),
                    target_staged=False,
                )

                self.assertEqual(main.current_track_info, track)
                self.assertEqual(main.current_footer_owner, "local")
                self.assertEqual(main.last_track_info, retry)
                self.assertEqual(playback_queue.queue.tracks, queue)
                self.assertEqual(playback_queue.queue.index, 0)
                self.assertEqual(playback_queue.queue.mode, "app_replace")
        finally:
            restore_queue_state(saved_queue)

        mark_authoritative.assert_called_once()

    async def test_radio_spotify_failure_restores_radio_context_and_reconnect_state(self):
        player = PlayerDouble(None, playing=False)
        track = radio_track()
        with patch.object(
            main.FxrouteTransitionRuntime,
                "_restore_committed_source_after_failed_transition",
            AsyncMock(return_value=True),
        ), patch.object(main, "player_instance", player), patch.object(
            main, "current_track_info", None
        ), patch.object(main, "last_track_info", dict(track)), patch.object(
            main, "current_footer_owner", "spotify"
        ), patch.object(main, "radio_reconnect_attempts", 2), patch.object(
            main, "radio_reconnect_url", "https://radio.example/live"
        ), patch.object(main, "radio_reconnect_active_since", 5.0), patch.object(
            main, "_mark_player_state_authoritative"
        ):
            await make_transition_runtime().abort_failed_transition(
                spotify_request(),
                snapshot_with(track, current_file=None, playing=False),
                target_staged=False,
            )

            self.assertEqual(main.current_track_info, track)
            self.assertEqual(main.current_footer_owner, "local")
            self.assertEqual(main.radio_reconnect_attempts, 2)
            self.assertEqual(main.radio_reconnect_url, "https://radio.example/live")
            self.assertEqual(main.radio_reconnect_active_since, 5.0)

    async def test_spotify_verify_failure_after_start_attempt_restores_context(self):
        player = PlayerDouble(None, playing=False)
        track = local_track()
        with patch.object(
            main.FxrouteTransitionRuntime,
                "_restore_committed_source_after_failed_transition",
            AsyncMock(return_value=True),
        ), patch.object(main, "player_instance", player), patch.object(
            main, "current_track_info", None
        ), patch.object(main, "current_footer_owner", "spotify"), patch.object(
            main, "_mark_player_state_authoritative"
        ):
            await make_transition_runtime().abort_failed_transition(
                spotify_request(),
                snapshot_with(track, current_file="/music/old.flac", playing=False),
                target_staged=False,
            )

            # The abort hook is stage-independent: a commit-verify failure
            # after the Spotify start attempt restores the old context too.
            self.assertEqual(main.current_track_info, track)
            self.assertEqual(main.current_footer_owner, "local")

    async def test_spotify_failure_without_prior_local_context_restores_nothing(self):
        player = PlayerDouble(None, playing=False)
        with patch.object(main, "player_instance", player), patch.object(
            main, "current_track_info", None
        ), patch.object(main, "current_footer_owner", "spotify"), patch.object(
            main, "_mark_player_state_authoritative"
        ):
            await make_transition_runtime().abort_failed_transition(
                spotify_request(),
                {"player": {}, "current_track": {"source": "spotify"}},
                target_staged=False,
            )

            self.assertIsNone(main.current_track_info)
            self.assertEqual(main.current_footer_owner, "spotify")

    async def test_restore_with_missing_player_does_not_crash(self):
        track = local_track()
        with patch.object(
            main.FxrouteTransitionRuntime,
                "_restore_committed_source_after_failed_transition",
            AsyncMock(return_value=True),
        ), patch.object(main, "player_instance", None), patch.object(
            main, "current_track_info", None
        ), patch.object(main, "current_footer_owner", "spotify"), patch.object(
            main, "_mark_player_state_authoritative"
        ):
            await make_transition_runtime().abort_failed_transition(
                spotify_request(),
                snapshot_with(track, current_file="/music/old.flac"),
                target_staged=False,
            )
            self.assertEqual(main.current_track_info, track)
            self.assertEqual(main.current_footer_owner, "local")

    async def test_restored_local_context_is_replayable_through_toggle(self):
        player = PlayerDouble(None, playing=False)
        track = local_track()
        run_mock = AsyncMock(return_value=SimpleNamespace(target_rate=48000))
        restore_ready = AsyncMock(return_value=True)
        commit = Mock()
        with patch.object(
            main.FxrouteTransitionRuntime,
                "_restore_committed_source_after_failed_transition",
            restore_ready,
        ), patch.object(main, "player_instance", player), patch.object(
            main, "current_track_info", None
        ), patch.object(main, "current_footer_owner", "spotify"), patch.object(
            main, "_can_send_play_command", return_value=True
        ), patch.object(main, "_coordinator_target_rate", lambda *a, **k: 48000), patch.object(
            main, "_coordinator_rate_change", lambda *a, **k: False
        ), patch.object(main, "_run_coordinated_transition", run_mock), patch.object(
            main, "_commit_coordinated_track", commit
        ), patch.object(main, "build_playback_payload", side_effect=dict):
            await make_transition_runtime().abort_failed_transition(
                spotify_request(),
                snapshot_with(track, current_file="/music/old.flac"),
                target_staged=False,
            )
            self.assertEqual(main.current_track_info, track)
            self.assertEqual(main.current_footer_owner, "local")

            result = await main.toggle_playback()

        self.assertEqual(result["status"], "playing")
        self.assertEqual(result["replayed"], True)
        request = run_mock.await_args.args[0]
        self.assertEqual(request.operation, "replay")
        self.assertEqual(request.source, "local")
        self.assertEqual(request.target_url, "/music/old.flac")
        commit.assert_called_once()


class SpotifySuccessfulCommitSemanticsTests(unittest.IsolatedAsyncioTestCase):
    """A successful Spotify commit keeps its existing semantics unchanged."""

    async def test_successful_spotify_commit_gives_up_old_context_and_spotify_takes_over(self):
        run_mock = AsyncMock(return_value=SimpleNamespace(committed=True))
        spotify_state = {"status": "Playing", "title": "Spotify Track"}
        broadcast = AsyncMock(return_value={"ok": True})
        with patch.object(main, "_run_coordinated_transition", run_mock), patch.object(
            main, "_coordinator_target_rate", lambda *a, **k: 48000
        ), patch.object(main, "_coordinator_rate_change", lambda *a, **k: True), patch.object(
            main, "current_footer_owner", "local"
        ), patch.object(main, "latest_spotify_state", None), patch.object(
            main, "get_spotify_ui_state", AsyncMock(return_value=spotify_state)
        ), patch.object(main, "broadcast_spotify_state", broadcast):
            result = await main.api_spotify_play()

            self.assertEqual(result, {"ok": True})
            run_mock.assert_awaited_once()
            request = run_mock.await_args.args[0]
            self.assertEqual(request.source, "spotify")
            self.assertEqual(request.operation, "spotify-play")
            self.assertTrue(request.should_play)
            self.assertEqual(main.current_footer_owner, "spotify")
            broadcast.assert_awaited_once_with(spotify_state)


if __name__ == "__main__":
    unittest.main(verbosity=2)
