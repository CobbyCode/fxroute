#!/usr/bin/env python3
"""Pending measurement ownership must be released on every failed start."""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import measurement_session


class _Session:
    generation = 7

    def __init__(self):
        self.active = set()

    async def register_manual_job(self, job_id):
        self.active.add(job_id)
        return self.generation

    async def replace_manual_job(self, old_job_id, new_job_id):
        self.active.remove(old_job_id)
        self.active.add(new_job_id)

    async def unregister_manual_job(self, job_id):
        self.active.discard(job_id)


class _BlockingReplacementSession(_Session):
    def __init__(self):
        super().__init__()
        self.replacement_started = asyncio.Event()
        self.allow_replacement = asyncio.Event()

    async def replace_manual_job(self, old_job_id, new_job_id):
        self.replacement_started.set()
        await self.allow_replacement.wait()
        await super().replace_manual_job(old_job_id, new_job_id)


class MeasurementStartOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = _Session()
        self.original_session = main.measurement_sr_session
        main.measurement_sr_session = self.session

    async def asyncTearDown(self):
        main.measurement_sr_session = self.original_session

    async def test_unexpected_start_error_releases_pending_owner(self):
        async def fail_start():
            raise OSError("disk full")

        with patch.object(measurement_session, "_measurement_entry_preflight", AsyncMock()):
            with self.assertRaisesRegex(OSError, "disk full"):
                await measurement_session._start_registered_manual_measurement(48000, fail_start)
        self.assertEqual(self.session.active, set())

    async def test_request_cancellation_releases_pending_owner(self):
        entered = asyncio.Event()

        async def blocked_start():
            entered.set()
            await asyncio.Event().wait()

        with patch.object(measurement_session, "_measurement_entry_preflight", AsyncMock()):
            task = asyncio.create_task(
                measurement_session._start_registered_manual_measurement(48000, blocked_start)
            )
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        await asyncio.sleep(0)
        self.assertEqual(self.session.active, set())

    async def test_cancellation_during_handoff_keeps_concrete_job_owned(self):
        session = _BlockingReplacementSession()
        main.measurement_sr_session = session

        async def start_job():
            return {"id": "job-1"}

        with patch.object(measurement_session, "_measurement_entry_preflight", AsyncMock()), patch.object(
            measurement_session, "_unregister_measurement_job_after_completion", AsyncMock()
        ) as watcher:
            task = asyncio.create_task(
                measurement_session._start_registered_manual_measurement(48000, start_job)
            )
            await session.replacement_started.wait()
            task.cancel()
            await asyncio.sleep(0)
            self.assertEqual(len(session.active), 1)
            self.assertTrue(next(iter(session.active)).startswith("pending:"))
            session.allow_replacement.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(session.active, {"job-1"})
            await asyncio.sleep(0)
            watcher.assert_awaited_once_with("job-1", session.generation)


if __name__ == "__main__":
    unittest.main(verbosity=2)
