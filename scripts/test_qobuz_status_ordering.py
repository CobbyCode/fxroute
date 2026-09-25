# SPDX-License-Identifier: AGPL-3.0-only
"""Older qbzd reads must not replace newer provider state."""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main  # noqa: E402


class QobuzStatusOrderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_newer_read_does_not_discard_successful_older_read(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def status():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("qbzd temporarily unavailable")
            entered.set()
            await release.wait()
            return {"status": "Playing", "volume": 100}

        with mock.patch.object(main.streaming, "get_provider", return_value=mock.Mock(status=status)), \
             mock.patch.object(main, "get_output_volume_safe", return_value=50), \
             mock.patch.object(main.playback_state, "latest_qobuz_state", None):
            old = asyncio.create_task(main.get_qobuz_ui_state())
            await asyncio.wait_for(entered.wait(), 1)
            with self.assertRaises(RuntimeError):
                await main.get_qobuz_ui_state()
            release.set()
            self.assertEqual((await asyncio.wait_for(old, 1))["status"], "Playing")
            self.assertEqual(main.playback_state.latest_qobuz_state["status"], "Playing")

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
                return {"status": "Playing", "volume": 100}
            return {"status": "Paused", "volume": 100}

        provider = mock.Mock(status=status)
        with mock.patch.object(main.streaming, "get_provider", return_value=provider), \
             mock.patch.object(main, "get_output_volume_safe", return_value=50), \
             mock.patch.object(main.playback_state, "latest_qobuz_state", None):
            old = asyncio.create_task(main.get_qobuz_ui_state())
            await asyncio.wait_for(entered.wait(), 1)
            try:
                newer = await main.get_qobuz_ui_state()
            finally:
                release.set()
            late = await asyncio.wait_for(old, 1)
            self.assertEqual(newer["status"], "Paused")
            self.assertEqual(late["status"], "Paused")
            self.assertEqual(main.playback_state.latest_qobuz_state["status"], "Paused")

    async def test_late_playing_read_cannot_replace_newer_stop_action(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def status():
            entered.set()
            await release.wait()
            return {"status": "Playing", "volume": 100}

        async def pause():
            return {"status": "Stopped"}

        with mock.patch.object(main.streaming, "get_provider", return_value=mock.Mock(status=status, pause=pause)), \
             mock.patch.object(main, "get_output_volume_safe", return_value=50), \
             mock.patch.object(main.playback_state, "latest_qobuz_state", None):
            old = asyncio.create_task(main.get_qobuz_ui_state())
            await asyncio.wait_for(entered.wait(), 1)
            try:
                await main.qobuz_pause()
            finally:
                release.set()
            late = await asyncio.wait_for(old, 1)
            self.assertEqual(late["status"], "Stopped")
            self.assertEqual(main.playback_state.latest_qobuz_state["status"], "Stopped")


if __name__ == "__main__":
    unittest.main()
