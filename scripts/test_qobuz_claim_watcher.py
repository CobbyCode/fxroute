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

    with mock.patch.object(qobuz_watch, "POLL_INTERVAL_SECONDS", 0.0), mock.patch.object(qobuz_watch, "LINE_POLL_INTERVAL_SECONDS", 0.0):
        while not done() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await watch.stop()


class QobuzClaimWatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_waits_for_cancelled_watch_cleanup(self):
        watch, _ = make_watch([{"available": False, "status": "Stopped"}])
        entered = asyncio.Event()
        release = asyncio.Event()

        async def cleanup_on_cancel():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
                raise

        watch.watch_task = asyncio.create_task(cleanup_on_cancel())
        await entered.wait()
        original = watch.watch_task
        stopped = asyncio.create_task(watch.stop())
        try:
            await asyncio.sleep(0)
            self.assertFalse(stopped.done())
            self.assertIs(watch.watch_task, original)
        finally:
            release.set()
            await asyncio.wait_for(stopped, 1)
        self.assertTrue(original.done())
        self.assertIsNone(watch.watch_task)

    async def test_stop_observes_already_failed_watch(self):
        watch, _ = make_watch([{"available": False, "status": "Stopped"}])

        async def fail():
            raise RuntimeError("watch failed")

        watch.watch_task = asyncio.create_task(fail())
        await asyncio.sleep(0)
        await watch.stop()
        self.assertIsNone(watch.watch_task)

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

    async def test_source_switch_reclaims_when_pause_was_between_polls(self):
        from types import SimpleNamespace
        playback = SimpleNamespace(source_generation=0)
        watch, calls = make_watch([{"available": True, "status": "Playing"}])
        watch._deps.get_playback_state = lambda: playback
        original = watch._deps.broadcast_qobuz_state

        async def broadcast():
            if calls["seen"] == 1:
                playback.source_generation += 1
            return await original()

        watch._deps.broadcast_qobuz_state = broadcast
        await _run_until(watch, calls, passes=3)
        self.assertEqual(len(calls["claims"]), 2, "a retained Playing sample must not hide a new source takeover")

    async def test_playing_line_selection_retries_takeover_without_visible_edge(self):
        from types import SimpleNamespace
        watch, calls = make_watch([{"available": True, "status": "Playing"}])
        playback = SimpleNamespace(source_generation=1, source_mode="external-input")
        watch._deps.get_playback_state = lambda: playback
        await _run_until(watch, calls, passes=3)
        self.assertEqual(len(calls["claims"]), 3, "line routing cannot remain selected over a Playing renderer")

    async def test_claim_precedes_peak_broadcast_on_playing_edge(self):
        from types import SimpleNamespace
        events = []
        watch, calls = make_watch([{"available": True, "status": "Playing"}])
        watch._deps.get_playback_state = lambda: SimpleNamespace(source_generation=0)
        watch._deps.read_qobuz_state = mock.AsyncMock(return_value={"available": True, "status": "Playing"})
        original = watch._deps.broadcast_qobuz_state

        async def broadcast(state=None):
            events.append("broadcast")
            return await original()

        async def claim(detail):
            events.append("claim")
            calls["claims"].append(detail)

        watch._deps.broadcast_qobuz_state = broadcast
        watch._deps.claim_qobuz_playback = claim
        await _run_until(watch, calls, passes=1)
        self.assertEqual(events[:2], ["claim", "broadcast"], "peak setup must not delay source ownership")

    async def test_slow_peak_broadcast_does_not_block_next_transport_observation(self):
        from types import SimpleNamespace
        entered = asyncio.Event()
        release = asyncio.Event()
        watch, calls = make_watch([{"available": True, "status": "Playing"}])
        reads = []
        watch._deps.get_playback_state = lambda: SimpleNamespace(source_generation=0, source_mode="app-playback")

        async def read():
            reads.append(True)
            return {"available": True, "status": "Playing"}

        async def broadcast(state):
            entered.set()
            await release.wait()

        watch._deps.read_qobuz_state = read
        watch._deps.broadcast_qobuz_state = broadcast
        with mock.patch.object(qobuz_watch, "POLL_INTERVAL_SECONDS", 0.0), mock.patch.object(qobuz_watch, "LINE_POLL_INTERVAL_SECONDS", 0.0):
            task = asyncio.create_task(watch.run_watch_loop())
            try:
                await entered.wait()
                for _ in range(20):
                    await asyncio.sleep(0)
                self.assertGreater(len(reads), 2, "meter setup must not starve Connect transport reads")
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await watch.stop()

    async def test_transport_reader_uses_only_daemon_playback_facts(self):
        from streaming.qobuz import backend
        from playback.qobuz_watch import read_claim_state
        with mock.patch.object(backend, "qbzd_installed", return_value=True), mock.patch.object(
            backend, "get_json", new=mock.AsyncMock(return_value={"state": "Playing", "track_id": 42})
        ) as read:
            state = await read_claim_state()
        self.assertTrue(_is_playing(state))
        self.assertEqual(read.await_args.args[1], "/api/playback")

    async def test_join_autoplay_edge_is_not_claimed(self):
        # qbzd re-joins the Connect session and plays the session's track even
        # though FXRoute recorded a stopped intent; that edge is the daemon's
        # own autoplay and must not take the output over.
        watch, calls = make_watch([{"available": True, "status": "Playing"}])
        reconciled = []

        async def reconcile_join():
            reconciled.append(True)
            return {"reconciled": True}

        async def suppress():
            return True

        watch._deps.reconcile_join = reconcile_join
        watch._deps.reconcile_join_autoplay = suppress
        await _run_until(watch, calls, passes=3)
        self.assertEqual(calls["claims"], [])
        self.assertGreaterEqual(len(reconciled), 3, "every pass reconciles while the join window is open")

    async def test_join_reconcile_failure_still_claims(self):
        # A broken join restore must not silently disable Connect takeovers.
        watch, calls = make_watch([{"available": True, "status": "Playing"}])

        async def reconcile_join():
            raise RuntimeError("intent unreadable")

        watch._deps.reconcile_join = reconcile_join
        with self.assertLogs("playback.qobuz_watch", level="WARNING"):
            await _run_until(watch, calls, claims_count=1)
        self.assertEqual(calls["claims"], ["qbzd-playing"])

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
