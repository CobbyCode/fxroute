#!/usr/bin/env python3
"""AutoSub lock ownership must survive failing session unregister/finalize."""

import asyncio
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import autosub
import main


class FailingSession:
    def __init__(self):
        self.calls = []

    async def unregister_auto_sub(self, job_id):
        self.calls.append(job_id)
        raise RuntimeError("simulated unregister failure")


class AutoSubLockReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        autosub._AUTO_SUB_JOBS.clear()
        autosub._AUTO_SUB_WORKER_TASKS.clear()
        for task in list(autosub._AUTO_SUB_CLEANUP_TASKS):
            task.cancel()
        autosub._AUTO_SUB_CLEANUP_TASKS.clear()
        main.measurement_sr_session = None
        try:
            autosub._auto_sub_lock.release()
        except RuntimeError:
            pass

    async def test_failed_unregister_releases_lock_and_preserves_job_status(self):
        session = FailingSession()
        main.measurement_sr_session = session
        await autosub._auto_sub_lock.acquire()
        job = {
            "id": "autosub-job",
            "status": "failed",
            "cancel_requested": False,
            "message": "simulated worker failure",
            "result": None,
            "error": {"detail": "boom"},
        }

        # Must not raise: the cleanup path is fully contained.
        await autosub._finish_auto_sub_worker(job, job["id"])

        self.assertEqual(session.calls, [job["id"]])
        self.assertFalse(autosub._auto_sub_lock.locked())
        self.assertFalse(autosub.is_optimization_active())
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["message"], "simulated worker failure")

    async def test_failed_unregister_still_finalizes_cancelling_job(self):
        session = FailingSession()
        main.measurement_sr_session = session
        await autosub._auto_sub_lock.acquire()
        job = {
            "id": "autosub-job-cancel",
            "status": "cancelling",
            "cancel_requested": True,
            "message": "Auto Sub Optimize cancelled.",
            "result": None,
            "error": None,
        }

        await autosub._finish_auto_sub_worker(job, job["id"])

        self.assertEqual(job["status"], "cancelled")
        self.assertFalse(autosub._auto_sub_lock.locked())

    async def test_following_auto_sub_operation_can_start_after_failure(self):
        session = FailingSession()
        main.measurement_sr_session = session
        await autosub._auto_sub_lock.acquire()
        job = {"id": "autosub-job", "status": "failed", "cancel_requested": False}

        await autosub._finish_auto_sub_worker(job, job["id"])

        # A subsequent operation acquires the shared lock immediately.
        await asyncio.wait_for(autosub._auto_sub_lock.acquire(), timeout=1.0)
        autosub._auto_sub_lock.release()

    async def test_release_when_lock_already_free_is_swallowed(self):
        session = FailingSession()
        main.measurement_sr_session = session
        try:
            autosub._auto_sub_lock.release()
        except RuntimeError:
            pass
        job = {"id": "autosub-job", "status": "failed", "cancel_requested": False}

        await autosub._finish_auto_sub_worker(job, job["id"])

        self.assertFalse(autosub._auto_sub_lock.locked())


    async def test_cleanup_task_is_scheduled_owned_and_removes_job(self):
        session = FailingSession()
        main.measurement_sr_session = session
        await autosub._auto_sub_lock.acquire()
        job = {"id": "autosub-job", "status": "failed", "cancel_requested": False}
        autosub._AUTO_SUB_JOBS[job["id"]] = job

        real_sleep = asyncio.sleep

        async def fast_sleep(*_args, **_kwargs):
            await real_sleep(0)
            return None

        with mock.patch("asyncio.sleep", new=fast_sleep):
            await autosub._finish_auto_sub_worker(job, job["id"])
            self.assertFalse(autosub._auto_sub_lock.locked())
            self.assertEqual(len(autosub._AUTO_SUB_CLEANUP_TASKS), 1)
            while autosub._AUTO_SUB_CLEANUP_TASKS:
                await asyncio.sleep(0)

        self.assertNotIn(job["id"], autosub._AUTO_SUB_JOBS)

    async def test_cleanup_task_is_scheduled_even_when_unregister_fails(self):
        session = FailingSession()
        main.measurement_sr_session = session
        await autosub._auto_sub_lock.acquire()
        job = {"id": "autosub-job", "status": "failed", "cancel_requested": False}
        autosub._AUTO_SUB_JOBS[job["id"]] = job

        await autosub._finish_auto_sub_worker(job, job["id"])

        self.assertEqual(len(autosub._AUTO_SUB_CLEANUP_TASKS), 1)

    async def test_shutdown_cancels_and_drains_cleanup_task(self):
        session = FailingSession()
        main.measurement_sr_session = session
        await autosub._auto_sub_lock.acquire()
        job = {"id": "autosub-job", "status": "failed", "cancel_requested": False}
        autosub._AUTO_SUB_JOBS[job["id"]] = job

        await autosub._finish_auto_sub_worker(job, job["id"])
        self.assertEqual(len(autosub._AUTO_SUB_CLEANUP_TASKS), 1)
        cleanup_task = next(iter(autosub._AUTO_SUB_CLEANUP_TASKS))

        await autosub.shutdown()

        self.assertTrue(cleanup_task.cancelled())
        self.assertFalse(autosub._AUTO_SUB_CLEANUP_TASKS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
