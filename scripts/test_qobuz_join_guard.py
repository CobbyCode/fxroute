# SPDX-License-Identifier: AGPL-3.0-only

"""Focused tests for the Qobuz intent store and the Connect join guard.

Live behaviour on .104 (power cycle 2026-10-15): qbzd re-joins the Qobuz
Connect session, the session hands back its own queue/repeat/track and the
fork plays that track although it had reported Paused before the restart.
Neither the repeat mode FXRoute wrote nor the stopped transport exists in qbzd
after the restart, so FXRoute has to restore both from its own record.

The guard is exercised with injected dependencies throughout: no daemon, no
journal and no network.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import playback.qobuz_join_guard as join_guard_module
from playback.qobuz_intent import QobuzIntent, clear, load, observe, save
from playback.qobuz_join_guard import (
    AUTOPLAY_SUPPRESS_WINDOW_SECONDS,
    LOOP_RESTORE_WINDOW_SECONDS,
    QobuzJoinGuard,
    QobuzJoinGuardDependencies,
)


class QobuzIntentStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "qobuz-intent.json"

    def test_round_trip(self):
        save(QobuzIntent(loop="none", playing=False), self.path)
        self.assertEqual(load(self.path), QobuzIntent(loop="none", playing=False))
        self.assertEqual(json.loads(self.path.read_text())["loop"], "none")

    def test_missing_invalid_and_unknown_are_no_intent(self):
        self.assertIsNone(load(self.path))
        self.path.write_text("{not json")
        self.assertIsNone(load(self.path))
        self.path.write_text(json.dumps({"loop": "sometimes", "playing": True}))
        self.assertIsNone(load(self.path))

    def test_observe_writes_only_on_change_and_never_clobbers_with_unknown(self):
        observe(loop="none", playing=False, path=self.path)
        first = self.path.read_text()
        observe(loop="none", playing=False, path=self.path)
        self.assertEqual(self.path.read_text(), first)
        # An unavailable daemon reports no loop mode: the stored intent stands.
        self.assertIsNone(observe(loop="", playing=True, path=self.path))
        self.assertEqual(load(self.path), QobuzIntent(loop="none", playing=False))
        observe(loop="playlist", playing=True, path=self.path)
        self.assertEqual(load(self.path), QobuzIntent(loop="playlist", playing=True))

    def test_save_creates_directory_and_leaves_no_temp_file(self):
        target = self.dir / "nested" / "qobuz-intent.json"
        save(QobuzIntent(loop="track", playing=True), target)
        self.assertTrue(target.exists())
        self.assertEqual([item.name for item in target.parent.iterdir()], ["qobuz-intent.json"])

    def test_clear_is_idempotent(self):
        save(QobuzIntent(loop="none", playing=False), self.path)
        clear(self.path)
        clear(self.path)
        self.assertIsNone(load(self.path))


class _GuardHarness:
    def __init__(self, *, intent=None, observed_loop="playlist", pause_error=None):
        self.intent = intent
        self.observed_loop = observed_loop
        self.pause_error = pause_error
        self.apply_error = None
        self.applied = []
        self.pauses = 0
        self.recorded = []
        self.clock_value = 1000.0
        guard = QobuzJoinGuard(
            QobuzJoinGuardDependencies(
                load_intent=lambda: self.intent,
                record_intent=self._record,
                read_loop=self._read_loop,
                apply_loop=self._apply_loop,
                pause_qobuz=self._pause,
            ),
            window_seconds=LOOP_RESTORE_WINDOW_SECONDS,
            autoplay_window_seconds=AUTOPLAY_SUPPRESS_WINDOW_SECONDS,
            clock=lambda: self.clock_value,
        )
        self.guard = guard

    def _record(self, *, loop, playing):
        self.recorded.append((loop, playing))

    async def _read_loop(self):
        return self.observed_loop

    async def _apply_loop(self, mode):
        if self.apply_error is not None:
            raise self.apply_error
        self.applied.append(mode)
        self.observed_loop = mode

    async def _pause(self):
        self.pauses += 1
        if self.pause_error is not None:
            raise self.pause_error

    def advance(self, seconds):
        self.clock_value += seconds


class QobuzJoinGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_without_intent_nothing_happens(self):
        harness = _GuardHarness(intent=None)
        harness.guard.note_session_join()
        self.assertEqual(await harness.guard.reconcile_join(),
                         {"reconciled": False, "reason": "no-intent"})
        self.assertFalse(await harness.guard.reconcile_join_autoplay())
        self.assertEqual(harness.applied, [])
        self.assertEqual(harness.pauses, 0)

    async def test_window_closed_is_a_no_op(self):
        harness = _GuardHarness(intent=QobuzIntent(loop="none", playing=False))
        self.assertEqual(await harness.guard.reconcile_join(),
                         {"reconciled": False, "reason": "window-closed"})
        self.assertFalse(await harness.guard.reconcile_join_autoplay())
        self.assertEqual(harness.applied, [])

    async def test_window_expires(self):
        harness = _GuardHarness(intent=QobuzIntent(loop="none", playing=False))
        harness.guard.note_session_join()
        harness.advance(LOOP_RESTORE_WINDOW_SECONDS + 1)
        self.assertFalse(harness.guard.window_active())
        self.assertFalse((await harness.guard.reconcile_join())["reconciled"])

    async def test_stored_repeat_is_re_applied_absolutely(self):
        # The session reports its own value (playlist); the user had set Off.
        harness = _GuardHarness(intent=QobuzIntent(loop="none", playing=False))
        harness.guard.note_session_join()
        result = await harness.guard.reconcile_join()
        self.assertEqual(harness.applied, ["none"])
        self.assertEqual(result["loop"], "none")
        self.assertTrue(result["loop_applied"])
        # Already reconciled: a further pass does not write again.
        result = await harness.guard.reconcile_join()
        self.assertEqual(harness.applied, ["none"])
        self.assertFalse(result["loop_applied"])
        self.assertEqual(result["loop"], "none")

    async def test_session_value_arriving_after_the_join_is_corrected(self):
        harness = _GuardHarness(intent=QobuzIntent(loop="none", playing=True))
        harness.guard.note_session_join()
        await harness.guard.reconcile_join()
        # The session pushes its own repeat after FXRoute's restore.
        harness.observed_loop = "track"
        await harness.guard.reconcile_join()
        self.assertEqual(harness.applied, ["none", "none"])

    async def test_failed_repeat_write_is_surfaced_and_retried(self):
        harness = _GuardHarness(intent=QobuzIntent(loop="none", playing=True))
        harness.guard.note_session_join()
        harness.apply_error = RuntimeError("qbzd unavailable")
        with self.assertLogs("playback.qobuz_join_guard", level="WARNING"):
            result = await harness.guard.reconcile_join()
        harness.apply_error = None
        self.assertTrue(result["reconciled"])
        self.assertFalse(result["loop_applied"])
        # A later pass tries again instead of leaving the wrong mode in place.
        await harness.guard.reconcile_join()
        self.assertEqual(harness.applied, ["none"])

    async def test_stopped_intent_pauses_the_join_autoplay(self):
        harness = _GuardHarness(intent=QobuzIntent(loop="none", playing=False))
        harness.guard.note_session_join()
        result = await harness.guard.reconcile_join()
        self.assertTrue(result["paused"])
        self.assertEqual(harness.pauses, 1)
        # Every Playing edge inside the window stays unclaimed: the user has
        # not started playback since the join.
        self.assertTrue(await harness.guard.reconcile_join_autoplay())
        self.assertTrue(await harness.guard.reconcile_join_autoplay())
        self.assertEqual(harness.pauses, 3)

    async def test_playing_intent_keeps_the_autoplay(self):
        harness = _GuardHarness(intent=QobuzIntent(loop="playlist", playing=True))
        harness.guard.note_session_join()
        result = await harness.guard.reconcile_join()
        self.assertFalse(result["paused"])
        self.assertEqual(harness.pauses, 0)
        self.assertFalse(await harness.guard.reconcile_join_autoplay())

    async def test_pause_failure_is_logged_and_not_fatal(self):
        harness = _GuardHarness(intent=QobuzIntent(loop="none", playing=False),
                                pause_error=RuntimeError("qbzd gone"))
        harness.guard.note_session_join()
        with self.assertLogs("playback.qobuz_join_guard", level="WARNING"):
            result = await harness.guard.reconcile_join()
        self.assertTrue(await harness.guard.reconcile_join_autoplay())
        self.assertFalse(result["paused"])

    async def test_observations_are_not_recorded_as_intent_during_the_window(self):
        harness = _GuardHarness(intent=QobuzIntent(loop="none", playing=False))
        harness.guard.note_session_join()
        harness.guard.reconcile_observed(loop="track", playing=True)
        self.assertEqual(harness.recorded, [])
        harness.advance(LOOP_RESTORE_WINDOW_SECONDS + 1)
        harness.guard.reconcile_observed(loop="none", playing=False)
        self.assertEqual(harness.recorded, [("none", False)])

    async def test_autoplay_window_ends_before_the_loop_window(self):
        # Stopping a playback the user starts in the app right after a re-join
        # is the price of the autoplay fix, so it is only paid while the fork's
        # own track start can still happen.
        harness = _GuardHarness(intent=QobuzIntent(loop="none", playing=False))
        harness.guard.note_session_join()
        harness.advance(AUTOPLAY_SUPPRESS_WINDOW_SECONDS + 0.1)
        self.assertTrue(harness.guard.window_active())
        self.assertFalse(harness.guard.autoplay_window_active())
        self.assertFalse(await harness.guard.reconcile_join_autoplay())
        self.assertEqual(harness.pauses, 0)
        # The repeat restore still runs: the session's value can arrive late.
        harness.observed_loop = "track"
        result = await harness.guard.reconcile_join()
        self.assertTrue(result["loop_applied"])
        self.assertEqual(harness.applied, ["none"])

    async def test_user_control_ends_the_window_and_is_recorded(self):
        harness = _GuardHarness(intent=QobuzIntent(loop="none", playing=False))
        harness.guard.note_session_join()
        harness.guard.note_user_intent()
        self.assertFalse(harness.guard.window_active())
        self.assertFalse(harness.guard.autoplay_window_active())
        harness.guard.reconcile_observed(loop="track", playing=True)
        self.assertEqual(harness.recorded, [("track", True)])
        # The press also disarms both restores.
        self.assertEqual(await harness.guard.reconcile_join(),
                         {"reconciled": False, "reason": "window-closed"})
        self.assertFalse(await harness.guard.reconcile_join_autoplay())

    async def test_every_join_rearms_the_window(self):
        harness = _GuardHarness(intent=QobuzIntent(loop="none", playing=False))
        harness.guard.note_session_join()
        harness.guard.note_user_intent()
        harness.guard.note_session_join()
        self.assertTrue(harness.guard.window_active())


class QobuzJoinGuardDefaultWindowTests(unittest.TestCase):
    def test_window_covers_the_session_push_and_track_download(self):
        # Live timing on .104: join +0.0s, track push +0.43s, first audio
        # frame +3.2s (a 64MB HiRes download). Both windows must cover that,
        # and the autoplay one must not outlive the loop one.
        self.assertGreaterEqual(join_guard_module.LOOP_RESTORE_WINDOW_SECONDS, 10.0)
        self.assertGreaterEqual(join_guard_module.AUTOPLAY_SUPPRESS_WINDOW_SECONDS, 10.0)
        self.assertLessEqual(join_guard_module.AUTOPLAY_SUPPRESS_WINDOW_SECONDS,
                             join_guard_module.LOOP_RESTORE_WINDOW_SECONDS)


if __name__ == "__main__":
    unittest.main()
