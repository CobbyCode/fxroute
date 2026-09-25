# SPDX-License-Identifier: AGPL-3.0-only
"""Older Spotify reads must not replace newer provider state."""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main  # noqa: E402


class SpotifyStatusOrderingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._read_sequence = main.playback_state.spotify_state_read_sequence
        self._commit_sequence = main.playback_state.spotify_state_commit_sequence

    def tearDown(self):
        main.playback_state.spotify_state_read_sequence = self._read_sequence
        main.playback_state.spotify_state_commit_sequence = self._commit_sequence

    async def test_late_playing_read_cannot_replace_newer_paused_read(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def status():
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
                return {"status": "Playing"}
            return {"status": "Paused"}

        with mock.patch.object(main, "spotify_get_status", side_effect=status), \
             mock.patch.object(main, "get_output_volume_safe", return_value=50), \
             mock.patch.object(main.playback_state, "latest_spotify_state", None):
            old = asyncio.create_task(main.get_spotify_ui_state())
            await asyncio.wait_for(entered.wait(), 1)
            try:
                newer = await main.get_spotify_ui_state()
            finally:
                release.set()
            late = await asyncio.wait_for(old, 1)
            self.assertEqual(newer["status"], "Paused")
            self.assertEqual(late["status"], "Paused")
            self.assertEqual(main.playback_state.latest_spotify_state["status"], "Paused")

    async def test_late_playing_read_cannot_replace_newer_stop_action(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def status():
            entered.set()
            await release.wait()
            return {"status": "Playing"}

        async def pause():
            return {"status": "Stopped"}

        with mock.patch.object(main, "spotify_get_status", side_effect=status), \
             mock.patch.object(main, "spotify_pause", side_effect=pause), \
             mock.patch.object(main, "get_output_volume_safe", return_value=50), \
             mock.patch.object(main.playback_state, "latest_spotify_state", None):
            old = asyncio.create_task(main.get_spotify_ui_state())
            await asyncio.wait_for(entered.wait(), 1)
            try:
                main._commit_spotify_state_direct(await main.spotify_pause())
            finally:
                release.set()
            late = await asyncio.wait_for(old, 1)
            self.assertEqual(late["status"], "Stopped")
            self.assertEqual(main.playback_state.latest_spotify_state["status"], "Stopped")


if __name__ == "__main__":
    unittest.main()
