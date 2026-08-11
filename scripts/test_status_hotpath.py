#!/usr/bin/env python3
"""The status telemetry helper must not run MPV IPC on the event loop."""

from __future__ import annotations

import asyncio
import time
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


class StatusHotpathTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocking_player_read_is_offloaded(self):
        ticks = 0

        async def ticker():
            nonlocal ticks
            for _ in range(3):
                await asyncio.sleep(0.01)
                ticks += 1

        async def run_read():
            return await main._read_status_player_detail(
                lambda: (time.sleep(0.06), {"ok": True})[1], {}
            )

        result, _ = await asyncio.gather(run_read(), ticker())
        self.assertEqual(result, {"ok": True})
        self.assertEqual(ticks, 3)


if __name__ == "__main__":
    unittest.main()
