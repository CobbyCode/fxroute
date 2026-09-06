# SPDX-License-Identifier: AGPL-3.0-only

"""Regression: Spotify start/resume re-arms the remote-volume pickup.

Critical flow: pickup -> pause (owner stays spotify, MPRIS stays visible)
-> Play/Resume pushes an absolute 100 -> the master must stay unchanged
because the start edge re-armed the pickup, so 100 only anchors.

The re-arm runs synchronously at the entry of every Spotify start path
(UI play, UI toggle resume, remote resume via claim), including the claim
no-op when Spotify already owns playback. The watch is touched only
through the public ``reset_session()``; tests assert the reset lands
before the first await of each entrypoint.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main as main_module


def _committed_result():
    return type("Result", (), {
        "committed": True,
        "transition_id": "tr-spotify-1",
        "source": "spotify",
        "target_rate": 44100,
        "state": {"committed": True},
    })()


def _playing_state():
    return {
        "available": True, "status": "Playing", "title": "T", "artist": "A",
        "album": "B", "trackId": "42", "artUrl": "http://x/c.jpg",
    }


def _paused_state():
    state = _playing_state()
    return dict(state, status="Paused")


class SpotifyVolumeRearmTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._orig_owner = main_module.playback_state.current_playback_owner
        self._orig_intent = main_module.playback_state.playback_intent_generation
        main_module.playback_state.current_playback_owner = None

    async def asyncTearDown(self):
        main_module.playback_state.current_playback_owner = self._orig_owner
        main_module.playback_state.playback_intent_generation = self._orig_intent

    async def test_play_rearms_before_first_await(self):
        reset = mock.Mock()
        seen = {}

        def target_rate(*args, **kwargs):
            seen["reset_called"] = reset.called
            return 44100

        with mock.patch.object(
            main_module.spotifyd_volume_watch, "reset_session", reset
        ), mock.patch.object(
            main_module, "_coordinator_target_rate", side_effect=target_rate
        ), mock.patch.object(
            main_module, "_coordinator_rate_change", return_value=False
        ), mock.patch.object(
            main_module, "_run_coordinated_transition",
            new=mock.AsyncMock(return_value=_committed_result()),
        ), mock.patch.object(
            main_module, "_publish_committed_playback_owner",
            new=mock.AsyncMock(),
        ), mock.patch.object(
            main_module, "get_spotify_ui_state",
            new=mock.AsyncMock(return_value=_playing_state()),
        ), mock.patch.object(
            main_module, "broadcast_spotify_state",
            new=mock.AsyncMock(side_effect=lambda data=None: data),
        ):
            await main_module.api_spotify_play()
        reset.assert_called_once_with()
        self.assertTrue(seen.get("reset_called"))

    async def test_toggle_resume_rearms(self):
        reset = mock.Mock()
        with mock.patch.object(
            main_module.spotifyd_volume_watch, "reset_session", reset
        ), mock.patch.object(
            main_module, "get_spotify_ui_state",
            new=mock.AsyncMock(return_value=_paused_state()),
        ), mock.patch.object(
            main_module, "_coordinator_target_rate", return_value=44100
        ), mock.patch.object(
            main_module, "_coordinator_rate_change", return_value=False
        ), mock.patch.object(
            main_module, "_run_coordinated_transition",
            new=mock.AsyncMock(return_value=_committed_result()),
        ) as run, mock.patch.object(
            main_module, "_publish_committed_playback_owner",
            new=mock.AsyncMock(),
        ), mock.patch.object(
            main_module, "broadcast_spotify_state",
            new=mock.AsyncMock(side_effect=lambda data=None: data),
        ):
            await main_module.api_spotify_toggle()
        reset.assert_called_once_with()
        run.assert_awaited_once()

    async def test_toggle_pause_rearms_at_entry(self):
        # The entry reset runs before the first await even when the toggle
        # turns out to be transport-only pause.
        reset = mock.Mock()
        seen = {}

        async def ui_state(*args, **kwargs):
            seen["reset_called"] = reset.called
            return _playing_state()

        with mock.patch.object(
            main_module.spotifyd_volume_watch, "reset_session", reset
        ), mock.patch.object(
            main_module, "get_spotify_ui_state", side_effect=ui_state
        ), mock.patch.object(
            main_module, "spotify_pause",
            new=mock.AsyncMock(return_value=_paused_state()),
        ), mock.patch.object(
            main_module, "broadcast_spotify_state",
            new=mock.AsyncMock(side_effect=lambda data=None: data),
        ), mock.patch.object(
            main_module, "_run_coordinated_transition",
            new=mock.AsyncMock(side_effect=AssertionError("no handoff on pause")),
        ):
            await main_module.api_spotify_toggle()
        reset.assert_called_once_with()
        self.assertTrue(seen.get("reset_called"))

    async def test_claim_rearms_when_already_owner(self):
        # Remote resume while Spotify already owns playback: the claim is a
        # no-op for ownership but must still re-arm the pickup.
        main_module.playback_state.current_playback_owner = "spotify"
        reset = mock.Mock()
        with mock.patch.object(
            main_module.spotifyd_volume_watch, "reset_session", reset
        ), mock.patch.object(
            main_module, "get_spotify_ui_state",
            new=mock.AsyncMock(return_value=_playing_state()),
        ), mock.patch.object(
            main_module, "_run_coordinated_transition",
            new=mock.AsyncMock(side_effect=AssertionError("no transition expected")),
        ):
            result = await main_module._claim_spotify_playback("playerctl-playing")
        reset.assert_called_once_with()
        self.assertEqual(result["status"], "Playing")

    async def test_claim_rearms_before_transition(self):
        reset = mock.Mock()
        seen = {}

        async def ui_state(*args, **kwargs):
            seen["reset_called"] = reset.called
            return _playing_state()

        with mock.patch.object(
            main_module.spotifyd_volume_watch, "reset_session", reset
        ), mock.patch.object(
            main_module, "get_spotify_ui_state", side_effect=ui_state
        ), mock.patch.object(
            main_module, "_is_spotify_playback_active", return_value=True
        ), mock.patch.object(
            main_module, "_coordinator_target_rate", return_value=44100
        ), mock.patch.object(
            main_module, "_coordinator_rate_change", return_value=False
        ), mock.patch.object(
            main_module, "_run_coordinated_transition",
            new=mock.AsyncMock(return_value=_committed_result()),
        ), mock.patch.object(
            main_module, "_publish_committed_playback_owner",
            new=mock.AsyncMock(),
        ), mock.patch.object(
            main_module, "broadcast_spotify_state",
            new=mock.AsyncMock(return_value=_playing_state()),
        ):
            await main_module._claim_spotify_playback("playerctl-playing")
        reset.assert_called_once_with()
        self.assertTrue(seen.get("reset_called"))


if __name__ == "__main__":
    unittest.main()
