#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Measurement failures retain tracebacks without changing job cleanup."""

import asyncio
import copy
import sys
import traceback
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from measurement.job_runner import MeasurementJobRunner


class MeasurementJobRunnerLoggingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.job = {"id": "measurement-failure", "status": "queued"}
        self.persisted = []
        self.cleaned = []
        self.retained = []
        self.runner = MeasurementJobRunner(
            get_job=lambda _job_id: self.job,
            persist_job=lambda job: self.persisted.append(copy.deepcopy(job)),
            public_result=copy.deepcopy,
            cleanup_job=self.cleaned.append,
            retain_history=lambda: self.retained.append(True),
            utc_now=lambda: "2026-10-22T00:00:00Z",
            is_terminal=lambda status: status in {"completed", "failed", "cancelled"},
        )

    def assert_failure(self, captured, error, frame_name):
        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        self.assertIn(self.job["id"], record.getMessage())
        self.assertIsNotNone(record.exc_info)
        self.assertIs(record.exc_info[1], error)
        frames = traceback.extract_tb(record.exc_info[2])
        self.assertIn(frame_name, [frame.name for frame in frames])
        self.assertIn("Traceback (most recent call last)", captured.output[0])
        self.assertEqual(self.job["status"], "failed")
        self.assertEqual(self.job["message"], str(error))
        self.assertEqual(self.job["error"], {"detail": str(error)})
        self.assertIsNone(self.job["result"])
        self.assertEqual(self.persisted[-1], self.job)
        self.assertEqual(self.cleaned, [self.job["id"]])
        self.assertEqual(self.retained, [True])

    async def test_executor_failure_logs_original_traceback_and_job_id(self):
        error = ValueError("capture failed")

        def failing_capture(_job):
            raise error

        with self.assertLogs("measurement.job_runner", level="ERROR") as captured:
            await self.runner.run(self.job["id"], self.job, failing_capture)
        self.assert_failure(captured, error, "failing_capture")

    async def test_scope_entry_failure_logs_original_traceback_and_job_id(self):
        error = RuntimeError("scope unavailable")

        async def failing_scope():
            raise error

        def unexpected_capture(_job):
            self.fail("capture must not run after scope entry fails")

        self.runner._active_scope_enter = failing_scope
        with self.assertLogs("measurement.job_runner", level="ERROR") as captured:
            await self.runner.run(self.job["id"], self.job, unexpected_capture)
        self.assert_failure(captured, error, "failing_scope")

    async def test_cooperative_cancellation_is_not_logged_as_failure(self):
        self.runner.cancelled_jobs.add(self.job["id"])
        with self.assertNoLogs("measurement.job_runner", level="ERROR"):
            await self.runner.run(self.job["id"], self.job, lambda _job: self.fail("cancelled"))
        self.assertEqual(self.job["status"], "cancelled")
        self.assertEqual(self.cleaned, [self.job["id"]])

    async def test_task_cancellation_is_not_logged_as_failure(self):
        async def cancelled_scope():
            raise asyncio.CancelledError()

        self.runner._active_scope_enter = cancelled_scope
        with self.assertNoLogs("measurement.job_runner", level="ERROR"):
            await self.runner.run(self.job["id"], self.job, lambda _job: self.fail("cancelled"))
        self.assertEqual(self.job["status"], "cancelled")
        self.assertEqual(self.cleaned, [self.job["id"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
