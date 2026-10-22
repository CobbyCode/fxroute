# SPDX-License-Identifier: AGPL-3.0-only
"""A failed older write must not replace a newer remote-volume gesture."""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playback.remote_volume import RemoteVolumePickupTranslator, drain_pending_loop


class RemoteVolumeRetryRaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.active = True
        self.master = 50
        self.failure = RuntimeError("write failed")
        self.attempts = []
        self.applied = []

        async def apply(value):
            self.attempts.append(value)
            if len(self.attempts) == 1:
                self.entered.set()
                await self.release.wait()
                if self.failure is not None:
                    raise self.failure
            self.master = value
            self.applied.append(value)

        self.translator = RemoteVolumePickupTranslator(
            is_active=lambda: self.active, apply_volume_value=apply,
            current_master=lambda: self.master,
        )
        self.translator.submit(49)
        self.translator.submit(50)
        self.tasks = []

    async def asyncTearDown(self):
        self.release.set()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def start_flush(self):
        task = asyncio.create_task(self.translator.flush())
        self.tasks.append(task)
        await asyncio.wait_for(self.entered.wait(), 2)
        return task

    async def check_retry(self, newer, cancel=False):
        task = await self.start_flush()
        if newer is not None:
            self.translator.submit(newer)
        if cancel:
            task.cancel()
        else:
            self.release.set()
        with self.assertRaises(asyncio.CancelledError if cancel else RuntimeError):
            await task
        expected = 50 if newer is None else newer
        await self.translator.flush()
        self.assertEqual(self.applied, [expected])
        self.assertEqual(self.master, expected)
        self.assertIsNone(self.translator.pending)

    async def test_failed_write_retries_newest_gesture(self):
        await self.check_retry(51)

    async def test_cancelled_flush_retries_newest_gesture(self):
        await self.check_retry(51, cancel=True)

    async def test_failed_write_preserves_zero_gesture(self):
        await self.check_retry(0)

    async def test_cancelled_flush_preserves_zero_gesture(self):
        await self.check_retry(0, cancel=True)

    async def test_failed_write_without_new_gesture_retries_old_value(self):
        await self.check_retry(None)

    async def test_cancelled_flush_without_new_gesture_retries_old_value(self):
        await self.check_retry(None, cancel=True)

    async def test_successful_write_drains_newer_gesture(self):
        self.failure = None
        task = await self.start_flush()
        self.translator.submit(51)
        self.release.set()
        await task
        await self.translator.flush()
        self.assertEqual(self.applied, [50, 51])
        self.assertEqual(self.master, 51)

    async def test_owner_loss_drops_both_old_and_new_intent(self):
        task = await self.start_flush()
        self.translator.submit(51)
        self.active = False
        self.release.set()
        await task
        self.assertIsNone(self.translator.pending)
        self.assertFalse(self.translator.picked_up)
        self.assertEqual(self.applied, [])

    async def test_reactivation_preserves_new_session_gesture(self):
        task = await self.start_flush()
        self.translator.observe_activation()
        self.translator.submit(49)
        self.translator.submit(52)
        await task
        await self.translator.flush()
        self.assertEqual(self.applied, [52])
        self.assertEqual(self.master, 52)

    async def test_drain_loop_retries_latest_intent_after_write_failure(self):
        warning = mock.Mock()
        retry_delays = []

        async def sleep(delay):
            retry_delays.append(delay)

        task = asyncio.create_task(drain_pending_loop(
            translator=self.translator, debounce_seconds=0, sleep=sleep,
            log_warning=warning, log_prefix="test",
        ))
        self.tasks.append(task)
        await asyncio.wait_for(self.entered.wait(), 2)
        self.translator.submit(51)
        self.translator.submit(52)
        self.release.set()
        await asyncio.wait_for(task, 2)
        self.assertEqual(self.attempts, [50, 52])
        self.assertEqual(self.master, 52)
        self.assertEqual(retry_delays, [0.5])
        self.assertIsNone(self.translator.pending)
        warning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
