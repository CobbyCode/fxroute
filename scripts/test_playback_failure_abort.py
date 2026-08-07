#!/usr/bin/env python3
"""Focused snapshot/abort contracts for failed MPV transitions."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
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


class FailedTransitionAbortTests(unittest.IsolatedAsyncioTestCase):
    async def test_staged_target_is_stopped_and_active_context_invalidated(self):
        player = PlayerDouble("/music/new.flac", playing=True)
        current = {"source": "local", "url": "/music/new.flac", "id": "new"}
        retry = dict(current)
        with patch.object(main, "player_instance", player), patch.object(
            main, "current_track_info", current
        ), patch.object(main, "last_track_info", retry), patch.object(
            main, "current_footer_owner", "local"
        ), patch.object(main, "playback_queue", [dict(current)]), patch.object(
            main, "playback_queue_original", [dict(current)]
        ), patch.object(main, "playback_queue_index", 0), patch.object(
            main, "playback_queue_mode", "app_replace"
        ), patch.object(main, "_mark_player_state_authoritative"):
            await main.FxrouteTransitionRuntime().abort_failed_transition(
                local_request("/music/new.flac"),
                {
                    "player": {"current_file": "/music/old.flac"},
                    "current_track": {"source": "local", "url": "/music/old.flac"},
                },
                target_staged=True,
            )

            self.assertIsNone(main.current_track_info)
            self.assertEqual(main.last_track_info, retry)
            self.assertEqual(main.playback_queue, [])
            self.assertEqual(main.playback_queue_mode, "app_replace")

        player.stop_playback.assert_called_once_with()
        self.assertIsNone(player.state["current_file"])
        self.assertFalse(player.state["playing"])
        self.assertEqual(player.state["volume"], 0)

    async def test_unchanged_committed_context_is_kept_for_retry(self):
        player = PlayerDouble("/music/old.flac", playing=True)
        current = {"source": "local", "url": "/music/old.flac", "id": "old"}
        queue = [dict(current), {"source": "local", "url": "/music/next.flac", "id": "next"}]
        with patch.object(main, "player_instance", player), patch.object(
            main, "current_track_info", current
        ), patch.object(main, "playback_queue", queue), patch.object(
            main, "playback_queue_mode", "app_replace"
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
            await main.FxrouteTransitionRuntime().abort_failed_transition(
                request,
                {
                    "player": {"current_file": "/music/old.flac"},
                    "current_track": dict(current),
                },
                target_staged=False,
            )

            self.assertEqual(main.current_track_info, current)
            self.assertEqual(main.playback_queue, queue)

        player.stop_playback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
