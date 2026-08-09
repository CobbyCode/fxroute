#!/usr/bin/env python3
"""AutoSub lifespan task ownership regression tests."""

import asyncio
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import autosub
import main


class Store:
    def __init__(self):
        self.cancelled = []

    def cancel_job(self, job_id):
        self.cancelled.append(job_id)


class AutoSubShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        autosub._AUTO_SUB_JOBS.clear()
        autosub._AUTO_SUB_WORKER_TASKS.clear()
        for task in list(autosub._AUTO_SUB_CLEANUP_TASKS):
            task.cancel()
        autosub._AUTO_SUB_CLEANUP_TASKS.clear()
        main.measurement_store = None

    async def test_shutdown_requests_cancel_and_drains_owned_worker(self):
        store = Store()
        main.measurement_store = store
        job = {
            "id": "autosub-job",
            "status": "running",
            "cancel_requested": False,
            "current_sweep_id": "measurement-job",
        }
        autosub._AUTO_SUB_JOBS[job["id"]] = job

        async def worker():
            while not job["cancel_requested"]:
                await asyncio.sleep(0)

        autosub._start_auto_sub_worker(worker())
        await autosub.shutdown()

        self.assertEqual(job["status"], "cancelling")
        self.assertTrue(job["cancel_requested"])
        self.assertEqual(store.cancelled, ["measurement-job"])
        self.assertFalse(autosub._AUTO_SUB_WORKER_TASKS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
