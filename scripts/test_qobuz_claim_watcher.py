# SPDX-License-Identifier: AGPL-3.0-only
"""Focused tests for the Qobuz qbzd claim watcher.

Verifies exactly one ownership claim per Playing rising edge, no duplicate
claim on repeated Playing states, and that Paused/Stopped states never claim.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import playback.qobuz_watch as qobuz_watch
from playback.qobuz_watch import QobuzPlayerWatch, QobuzWatchDependencies


def _is_playing(state):
    state = state or {}
    return bool(state.get("available") and state.get("status") == "Playing")


def make_watch(states):
    calls = {"claims": [], "seen": 0}

    async def broadcast():
        idx = min(calls["seen"], len(states) - 1)
        calls["seen"] += 1
        return states[idx]

    async def claim(detail):
        calls["claims"].append(detail)

    deps = QobuzWatchDependencies(
        get_playback_state=lambda: None,
        broadcast_qobuz_state=broadcast,
        is_qobuz_playback_active=_is_playing,
        claim_qobuz_playback=claim,
    )
    return QobuzPlayerWatch(deps), calls


async def _run_until(watch, calls, *, claims_count=None, passes=None):
    """Run the watch loop until the predicate is satisfied, then cancel."""
    task = asyncio.create_task(watch.run_watch_loop())
    deadline = asyncio.get_running_loop().time() + 5.0

    def done():
        if claims_count is not None and len(calls["claims"]) < claims_count:
            return False
        if passes is not None and calls["seen"] < passes:
            return False
        return True

    with mock.patch.object(qobuz_watch, "POLL_INTERVAL_SECONDS", 0.0):
        while not done() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


class QobuzClaimWatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_claim_per_playing_rising_edge(self):
        watch, calls = make_watch(
            [
                {"available": True, "status": "Playing"},  # rising edge -> claim
                {"available": True, "status": "Playing"},  # duplicate -> no claim
                {"available": True, "status": "Paused"},   # paused -> no claim
                {"available": True, "status": "Playing"},  # rising edge -> claim
            ],
        )
        await _run_until(watch, calls, claims_count=2)
        self.assertEqual(calls["claims"], ["qbzd-playing", "qbzd-playing"])
        # The four states were fully consumed; the duplicate Playing pass did
        # not produce an extra claim.
        self.assertGreaterEqual(calls["seen"], 4)

    async def test_paused_and_stopped_never_claim(self):
        watch, calls = make_watch(
            [
                {"available": True, "status": "Paused"},
                {"available": True, "status": "Stopped"},
                {"available": False, "status": "Stopped"},
            ],
        )
        await _run_until(watch, calls, passes=3)
        self.assertEqual(calls["claims"], [])


if __name__ == "__main__":
    unittest.main()
