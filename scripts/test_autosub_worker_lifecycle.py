#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Finalizer failure tolerance for service jobs (Slice F cleanup chain).

The cleanup order is drain/restore mutes (funnel) → owner restore →
unregister → drop owner → finalize → lock release. Each step can fail;
the chain must still complete in order and always release the lock:

* a failing owner restore fails the job but still unregisters, drops the
  owner and releases the lock,
* a failing session unregister never blocks the owner drop, finalize or
  lock release,
* a committed owner is never restored through the retired stager,
* a failing finalize never blocks the lock release,
* repeated worker cancellation cannot interrupt the chain, and the
  CancelledError surfaces only after the chain completed.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from measurement.autosub import deps as autosub_deps  # noqa: E402
from measurement.autosub import jobs as autosub_jobs  # noqa: E402
from measurement.autosub import candidates as autosub_candidates  # noqa: E402


def make_job(job_id):
    return {"id": job_id, "status": "running", "cancel_requested": False,
            "output_state_context": {"mode": "stereo", "revision": 1,
                                     "output_key": "dev", "channels": 4,
                                     "optimizer_path": "single-sub",
                                     "sub_role_map": {"sub1": "sub1"},
                                     "sub_mute_mask": 4}}


class FinalizerChainTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events = []
        self.lock = asyncio.Lock()
        self.owner = SimpleNamespace(committed=False)
        self.session = SimpleNamespace()
        self.fail_unregister = False

        async def unregister_auto_sub(job_id):
            self.events.append("unregistered")
            if self.fail_unregister:
                raise RuntimeError("session gone")

        self.session.unregister_auto_sub = unregister_auto_sub
        self.patches = [
            patch.object(autosub_jobs, "_auto_sub_lock", self.lock),
            patch.object(autosub_jobs, "_measurement_session", lambda: self.session),
            patch.object(autosub_jobs, "_measurement_store", lambda: None),
            patch.object(autosub_candidates, "_candidate_owner",
                         lambda job_id: self.owner),
            patch.object(autosub_jobs, "drop_candidate_owner",
                         lambda job_id: self.events.append("dropped")),
            patch.object(autosub_jobs, "_finalize_autosub_job",
                         lambda job, job_id: self.events.append("finalized")),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(setattr, autosub_deps, "_autosub_deps", None)
        self.job_id = "finalize-job"

    def restore_factory(self, *, fail=False, committed=False):
        async def restore_or_fail(job, snapshot, message):
            if committed:
                self.events.append("restore_skipped")
                return True
            self.events.append("restore")
            if fail:
                job["status"] = "failed"
                return False
            return True

        return restore_or_fail

    def fail(self, message):
        raise AssertionError(message)

    async def run_cleanup(self, job):
        await self.lock.acquire()
        with patch.object(autosub_jobs, "_restore_original_config_or_fail_job",
                          self.restore_factory()):
            await autosub_jobs._finish_auto_sub_worker(job, self.job_id)

    async def test_failing_restore_still_unregisters_drops_finalizes_and_releases(self):
        job = make_job(self.job_id)
        with patch.object(autosub_jobs, "_restore_original_config_or_fail_job",
                          self.restore_factory(fail=True)):
            await self.lock.acquire()
            await autosub_jobs._finish_auto_sub_worker_cleanup(job, self.job_id)
        self.assertEqual(self.events, ["restore", "unregistered", "dropped", "finalized"])
        self.assertEqual(job["status"], "failed")
        self.assertFalse(self.lock.locked())

    async def test_failing_unregister_does_not_block_owner_drop_finalize_or_lock(self):
        job = make_job(self.job_id)
        self.fail_unregister = True
        with patch.object(autosub_jobs, "_restore_original_config_or_fail_job",
                          self.restore_factory()):
            await self.lock.acquire()
            await autosub_jobs._finish_auto_sub_worker_cleanup(job, self.job_id)
        self.assertEqual(self.events, ["restore", "unregistered", "dropped", "finalized"])
        self.assertFalse(self.lock.locked())

    async def test_committed_owner_is_never_restored(self):
        job = make_job(self.job_id)
        self.owner.committed = True

        def forbidden_restore():
            raise AssertionError("committed owner must not restore")

        self.owner.restore = forbidden_restore
        with patch.object(autosub_jobs, "_restore_original_config_or_fail_job",
                          self.restore_factory(committed=True)):
            await self.lock.acquire()
            await autosub_jobs._finish_auto_sub_worker_cleanup(job, self.job_id)
        self.assertEqual(self.events, ["restore_skipped", "unregistered", "dropped", "finalized"])
        self.assertFalse(self.lock.locked())

    async def test_repeated_cancellation_cannot_interrupt_chain_and_surfaces_after(self):
        job = make_job(self.job_id)
        started = asyncio.Event()
        release = asyncio.Event()

        async def unregister(job_id):
            started.set()
            await release.wait()
            self.events.append("unregistered")

        self.session.unregister_auto_sub = unregister
        task = asyncio.create_task(self.run_cleanup(job))
        try:
            await asyncio.wait_for(started.wait(), 2)
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertEqual(self.events, ["restore"])
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.events,
                         ["restore", "unregistered", "dropped", "finalized"])
        self.assertFalse(self.lock.locked())

    async def test_failing_finalize_never_blocks_lock_release(self):
        job = make_job(self.job_id)

        def finalize(job, job_id):
            self.events.append("finalized")
            raise RuntimeError("snapshot disk full")

        with patch.object(autosub_jobs, "_restore_original_config_or_fail_job",
                          self.restore_factory()), \
                patch.object(autosub_jobs, "_finalize_autosub_job", finalize):
            await self.lock.acquire()
            await autosub_jobs._finish_auto_sub_worker_cleanup(job, self.job_id)
        self.assertEqual(self.events, ["restore", "unregistered", "dropped", "finalized"])
        self.assertFalse(self.lock.locked())


if __name__ == "__main__":
    unittest.main(verbosity=2)
