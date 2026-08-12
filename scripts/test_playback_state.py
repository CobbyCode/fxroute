#!/usr/bin/env python3
"""Behavior tests for the REFACTOR-006 extraction:

- playback_state.is_local_playback_active
- playback_state.is_spotify_playback_active
- playback_state.playback_state_matches_track

plus wrapper parity against main._is_local_playback_active,
main._is_spotify_playback_active and main._playback_state_matches_track.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main
from playback_state import (
    is_local_playback_active,
    is_spotify_playback_active,
    playback_state_matches_track,
)


class IsLocalPlaybackActiveTests(unittest.TestCase):
    def test_none_and_empty_state_inactive(self):
        self.assertFalse(is_local_playback_active(None))
        self.assertFalse(is_local_playback_active({}))

    def test_active_only_with_current_file_not_paused_not_ended(self):
        self.assertTrue(is_local_playback_active({"current_file": "/a.mp3"}))
        self.assertTrue(
            is_local_playback_active({"current_file": "/a.mp3", "paused": False, "ended": False})
        )

    def test_paused_or_ended_inactive(self):
        self.assertFalse(is_local_playback_active({"current_file": "/a.mp3", "paused": True}))
        self.assertFalse(is_local_playback_active({"current_file": "/a.mp3", "ended": True}))

    def test_falsy_current_file_inactive(self):
        self.assertFalse(is_local_playback_active({"current_file": ""}))
        self.assertFalse(is_local_playback_active({"current_file": None}))


class IsSpotifyPlaybackActiveTests(unittest.TestCase):
    def test_none_and_empty_state_inactive(self):
        self.assertFalse(is_spotify_playback_active(None))
        self.assertFalse(is_spotify_playback_active({}))

    def test_active_only_with_available_and_status_playing(self):
        self.assertTrue(is_spotify_playback_active({"available": True, "status": "Playing"}))

    def test_other_statuses_inactive(self):
        self.assertFalse(is_spotify_playback_active({"available": True, "status": "Paused"}))
        self.assertFalse(is_spotify_playback_active({"available": True, "status": "Stopped"}))

    def test_falsy_available_inactive(self):
        self.assertFalse(is_spotify_playback_active({"available": False, "status": "Playing"}))
        self.assertFalse(is_spotify_playback_active({"available": None, "status": "Playing"}))
        self.assertFalse(is_spotify_playback_active({"status": "Playing"}))


class PlaybackStateMatchesTrackTests(unittest.TestCase):
    def test_none_state_or_track_match(self):
        self.assertTrue(playback_state_matches_track(None, None))
        self.assertTrue(playback_state_matches_track({}, {}))
        self.assertTrue(playback_state_matches_track(None, {"source": "local", "url": "/a.mp3"}))

    def test_matching_url_match(self):
        self.assertTrue(
            playback_state_matches_track(
                {"current_file": "/a.mp3"}, {"source": "local", "url": "/a.mp3"}
            )
        )
        self.assertTrue(
            playback_state_matches_track(
                {"current_file": "http://radio/x"}, {"source": "radio", "url": "http://radio/x"}
            )
        )

    def test_mismatch_only_for_local_and_radio(self):
        self.assertFalse(
            playback_state_matches_track(
                {"current_file": "/a.mp3"}, {"source": "local", "url": "/b.mp3"}
            )
        )
        self.assertFalse(
            playback_state_matches_track(
                {"current_file": "http://radio/a"}, {"source": "radio", "url": "http://radio/b"}
            )
        )
        # Andere Quellen: Mismatch irrelevant -> Match True
        self.assertTrue(
            playback_state_matches_track(
                {"current_file": "/a.mp3"}, {"source": "spotify", "url": "/b.mp3"}
            )
        )
        self.assertTrue(
            playback_state_matches_track(
                {"current_file": "/a.mp3"}, {"source": None, "url": "/b.mp3"}
            )
        )

    def test_missing_url_or_current_file_still_match(self):
        # Special cases: missing URL or current_file -> no mismatch
        self.assertTrue(playback_state_matches_track({"current_file": "/a.mp3"}, {"source": "local"}))
        self.assertTrue(
            playback_state_matches_track({"current_file": "/a.mp3"}, {"source": "local", "url": None})
        )
        self.assertTrue(playback_state_matches_track({}, {"source": "local", "url": "/b.mp3"}))
        self.assertTrue(
            playback_state_matches_track({"current_file": None}, {"source": "radio", "url": "/b.mp3"})
        )
        # empty string is falsy -> also no mismatch
        self.assertTrue(
            playback_state_matches_track({"current_file": ""}, {"source": "local", "url": "/b.mp3"})
        )
        self.assertTrue(
            playback_state_matches_track({"current_file": "/a.mp3"}, {"source": "local", "url": ""})
        )

    def test_inputs_not_mutated(self):
        state = {"current_file": "/a.mp3", "paused": False}
        track = {"source": "local", "url": "/b.mp3"}
        state_before = dict(state)
        track_before = dict(track)
        playback_state_matches_track(state, track)
        is_local_playback_active(state)
        is_spotify_playback_active(state)
        self.assertEqual(state, state_before)
        self.assertEqual(track, track_before)


class WrapperParityTests(unittest.TestCase):
    def test_local_wrapper_matches_module_function(self):
        states = [
            None,
            {},
            {"current_file": "/a.mp3"},
            {"current_file": "/a.mp3", "paused": True},
            {"current_file": "/a.mp3", "ended": True},
            {"current_file": "/a.mp3", "paused": False, "ended": False},
            {"current_file": ""},
        ]
        for state in states:
            self.assertEqual(
                main._is_local_playback_active(state),
                is_local_playback_active(state),
                f"Parität für {state!r}",
            )

    def test_spotify_wrapper_matches_module_function(self):
        states = [
            None,
            {},
            {"available": True, "status": "Playing"},
            {"available": True, "status": "Paused"},
            {"available": False, "status": "Playing"},
            {"status": "Playing"},
        ]
        for state in states:
            self.assertEqual(
                main._is_spotify_playback_active(state),
                is_spotify_playback_active(state),
                f"Parität für {state!r}",
            )

    def test_match_wrapper_matches_module_function(self):
        cases = [
            (None, None),
            ({}, {}),
            ({"current_file": "/a.mp3"}, {"source": "local", "url": "/a.mp3"}),
            ({"current_file": "/a.mp3"}, {"source": "local", "url": "/b.mp3"}),
            ({"current_file": "http://r/a"}, {"source": "radio", "url": "http://r/b"}),
            ({"current_file": "/a.mp3"}, {"source": "spotify", "url": "/b.mp3"}),
            ({"current_file": "/a.mp3"}, {"source": "local"}),
            ({"current_file": "/a.mp3"}, {"source": "local", "url": None}),
            ({}, {"source": "local", "url": "/b.mp3"}),
            ({"current_file": ""}, {"source": "local", "url": "/b.mp3"}),
        ]
        for state, track in cases:
            self.assertEqual(
                main._playback_state_matches_track(state, track),
                playback_state_matches_track(state, track),
                f"Parität für {(state, track)!r}",
            )


if __name__ == "__main__":
    unittest.main()
