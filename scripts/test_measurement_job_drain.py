#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""A terminal status is not proof that a capture's scope has been released."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from measurement.store import MeasurementStore


class MeasurementJobDrainTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="measurement-drain-")
        self.addCleanup(directory.cleanup)
        self.store = MeasurementStore(home=Path(directory.name))

    async def start_child(self):
        entered_cleanup = asyncio.Event()
        release_cleanup = asyncio.Event()
        events = []

        async def enter():
            return False

        async def exit_scope(previous):
            entered_cleanup.set()
            await release_cleanup.wait()
            events.append("scope_released")

        runner = self.store._job_runner
        runner._raw_scope_enter = enter
        runner._raw_scope_exit = exit_scope
        job = {"id": "child", "status": "queued", "measurement_scope": "raw_helper"}
        self.store._jobs["child"] = job
        # Keep real runner task/status/scope ownership; file/result plumbing is
        # unrelated to the completion boundary under test.
        runner._persist_job = lambda job: None
        runner._public_result = lambda result: result
        runner._cleanup_job = lambda job_id: None
        runner._retain_history = lambda: None
        child = runner.start("child", job, lambda job: {"message": "captured"})
        await asyncio.wait_for(entered_cleanup.wait(), 2)
        self.assertEqual(job["status"], "completed")
        self.assertFalse(child.done())
        return child, release_cleanup, events

    async def test_terminal_child_is_drained_through_scope_exit(self):
        self.assertTrue(callable(getattr(self.store, "drain_job", None)),
                        "MeasurementStore needs a per-job completion boundary")
        child, release, events = await self.start_child()
        draining = asyncio.create_task(self.store.drain_job("child"))
        try:
            await asyncio.sleep(0)
            self.assertFalse(draining.done())
            self.assertEqual(events, [])
        finally:
            release.set()
            await asyncio.gather(child, draining)
        self.assertEqual(events, ["scope_released"])

    async def test_repeated_cancellation_cannot_interrupt_child_scope_exit(self):
        self.assertTrue(callable(getattr(self.store, "drain_job", None)),
                        "MeasurementStore needs a per-job completion boundary")
        child, release, events = await self.start_child()
        draining = asyncio.create_task(self.store.drain_job("child"))
        try:
            await asyncio.sleep(0)
            for _ in range(3):
                draining.cancel()
                await asyncio.sleep(0)
                self.assertFalse(draining.done())
                self.assertFalse(child.cancelled())
                self.assertEqual(events, [])
        finally:
            release.set()
            await child
            with self.assertRaises(asyncio.CancelledError):
                await draining
        self.assertEqual(events, ["scope_released"])

    async def test_live_child_is_cancelled_and_drained_without_stopping_other_jobs(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        events = []
        job = {"id": "live", "status": "running"}
        self.store._jobs["live"] = job
        self.store._job_runner._persist_job = lambda job: None

        async def child_work():
            entered.set()
            await release.wait()
            events.append("worker_exited")

        child = asyncio.create_task(child_work())
        self.store._job_tasks["live"] = child
        unrelated = asyncio.create_task(asyncio.Event().wait())
        self.store._job_tasks["unrelated"] = unrelated
        await entered.wait()
        draining = asyncio.create_task(self.store.drain_job("live"))
        try:
            await asyncio.sleep(0)
            self.assertEqual(job["status"], "cancelling")
            self.assertFalse(draining.done())
            self.assertFalse(unrelated.done())
        finally:
            release.set()
            unrelated.cancel()
            await asyncio.gather(child, draining, unrelated, return_exceptions=True)
        self.assertEqual(events, ["worker_exited"])
        self.assertFalse(self.store._job_runner.shutting_down)

    async def test_persist_failure_on_cancel_request_still_drains_the_child(self):
        # drain_job's cancel request can fail (e.g. persisting the record);
        # the actual child task must still be awaited through its cleanup
        # and the original error surfaced afterwards.
        entered = asyncio.Event()
        release = asyncio.Event()
        events = []
        job = {"id": "flaky", "status": "running"}
        self.store._jobs["flaky"] = job
        self.store._job_runner._persist_job = lambda job: None

        async def child_work():
            entered.set()
            await release.wait()
            events.append("worker_exited")

        child = asyncio.create_task(child_work())
        self.store._job_tasks["flaky"] = child

        def broken_cancel(job_id):
            raise OSError("disk full while persisting cancellation")

        drainer = None
        with patch.object(self.store, "cancel_job", broken_cancel):
            drainer = asyncio.create_task(self.store.drain_job("flaky"))
            await asyncio.sleep(0)
            release.set()
            with self.assertRaises(OSError):
                await asyncio.wait_for(drainer, 2)
        self.assertEqual(events, ["worker_exited"])
        await child
        self.assertTrue(callable(getattr(self.store, "drain_job", None)),
                        "MeasurementStore needs a per-job completion boundary")
        with self.assertRaises(KeyError):
            await self.store.drain_job("missing")


if __name__ == "__main__":
    unittest.main()
