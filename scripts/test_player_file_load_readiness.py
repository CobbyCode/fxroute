#!/usr/bin/env python3
"""Regression: mpv source-readiness wait accepts the ``file-loaded`` signal.

``_wait_for_player_current_file`` must not treat the optimistic ``current_file``
set by ``loadfile`` as "loaded": a follow-up seek would race the load.  It
accepts a source once mpv reports a positive ``duration`` (known-length
files/streams) or fires the ``file-loaded`` event (live/unknown-length streams
that never report a duration).
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from playback.player import MPVWrapper


async def _wait(expected_url, state, timeout_ms=60):
    with patch.object(main.runtime, "player_instance", SimpleNamespace(state=state)), \
         patch.object(main, "PIPEWIRE_HANDOFF_POLL_INTERVAL_MS", 10):
        return await main._wait_for_player_current_file(expected_url, timeout_ms=timeout_ms)


class WaitForPlayerCurrentFileTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_when_duration_reported(self):
        state = {"current_file": "/music/a.flac", "duration": 300.0}
        self.assertTrue(await _wait("/music/a.flac", state))

    async def test_ready_when_file_loaded_without_duration(self):
        # Live/unknown-length sources never report a positive duration; the
        # file-loaded event is the fallback readiness signal.
        state = {"current_file": "https://stream.example/radio", "duration": 0.0, "file_loaded": True}
        self.assertTrue(await _wait("https://stream.example/radio", state))

    async def test_not_ready_when_only_current_file_set(self):
        # loadfile sets current_file optimistically; without duration or the
        # file-loaded event the wait must time out instead of declaring ready.
        state = {"current_file": "/music/a.flac", "duration": 0.0, "file_loaded": False}
        self.assertFalse(await _wait("/music/a.flac", state))

    async def test_not_ready_when_file_mismatched(self):
        state = {"current_file": "/music/other.flac", "duration": 300.0}
        self.assertFalse(await _wait("/music/a.flac", state))

    async def test_missing_url_or_player_is_not_ready(self):
        self.assertFalse(await _wait(None, {}))


class PlayerFileLoadedStateTests(unittest.TestCase):
    def _player(self):
        player = MPVWrapper()
        player._send_command = lambda *a, **k: {}
        return player

    def test_file_loaded_event_sets_flag(self):
        player = self._player()
        player._handle_event({"event": "file-loaded"})
        self.assertTrue(player._state["file_loaded"])

    def test_loadfile_resets_flag_until_mpv_confirms(self):
        player = self._player()
        player._state["file_loaded"] = True
        player.loadfile("/music/a.flac")
        self.assertFalse(player._state["file_loaded"])

    def test_stop_playback_resets_flag(self):
        player = self._player()
        player._state["file_loaded"] = True
        player.stop_playback()
        self.assertFalse(player._state["file_loaded"])


if __name__ == "__main__":
    unittest.main()
