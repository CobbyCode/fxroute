#!/usr/bin/env python3
"""Job-state invariants: exactly one terminal state, no overwrite after
terminal, cancel vs worker-end determinism, cleanup exactly once."""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autosub import _finalize_autosub_job
from measurement import MeasurementStore


class MeasurementJobStateTests(unittest.IsolatedAsyncioTestCase):
    def _store(self, tempdir):
        return MeasurementStore(home=Path(tempdir))

    def _register_job(self, store, job_id, **overrides):
        job = {
            "id": job_id,
            "status": "queued",
            "created_at": store._utc_now(),
            "updated_at": store._utc_now(),
            "message": "Sweep queued.",
            "result": None,
            "error": None,
            "input_channels": {"mic": 1, "electrical_reference": None, "reference_disabled_reason": ""},
            "calibration": {"filename": "", "applied": False},
        }
        job.update(overrides)
        store._jobs[job_id] = job
        return job

    async def test_cancel_racing_successful_worker_end_lands_cancelled(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            job_id = "measurement-job-cancel-race"
            self._register_job(store, job_id)

            def _executor(_job):
                # Cancel arrives while the worker is finishing; the worker
                # itself may already have produced a full result.
                store.cancel_job(job_id)
                return {"message": "Measurement finished."}

            store._execute_capture_job = _executor
            await store._run_measurement_job(job_id)

            job = store.get_job(job_id)
            self.assertEqual(job["status"], "cancelled")
            self.assertIsNone(job["result"])
            self.assertIsNone(job["error"])
            # Terminal state stays stable on repeated reads.
            self.assertEqual(store.get_job(job_id)["status"], "cancelled")

    async def test_cancel_racing_worker_exception_lands_cancelled(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            job_id = "measurement-job-cancel-exc"
            self._register_job(store, job_id)
            store._cancelled_jobs.add(job_id)

            def _executor(_job):
                raise RuntimeError("Measurement cancelled.")

            store._execute_capture_job = _executor
            await store._run_measurement_job(job_id)

            job = store.get_job(job_id)
            self.assertEqual(job["status"], "cancelled")
            self.assertIsNone(job["result"])
            self.assertIsNone(job["error"])
            self.assertEqual(store.get_job(job_id)["status"], "cancelled")

    async def test_two_competing_terminal_updates_only_first_wins(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)

            # completed job: stale promotion must not overwrite it
            done_id = "measurement-job-done"
            self._register_job(store, done_id, status="completed", result={"x": 1}, error=None)
            store._promote_stale_job_to_terminal(done_id, store._jobs[done_id])
            self.assertEqual(store.get_job(done_id)["status"], "completed")
            self.assertEqual(store.get_job(done_id)["result"], {"x": 1})

            # failed job + cancel requested afterwards: runner finalization
            # must not overwrite the failure
            failed_id = "measurement-job-failed"
            self._register_job(store, failed_id, status="failed", result=None,
                               error={"detail": "boom"})
            store._cancelled_jobs.add(failed_id)

            def _executor(_job):
                raise RuntimeError("boom")

            store._execute_capture_job = _executor
            await store._run_measurement_job(failed_id)

            job = store.get_job(failed_id)
            self.assertEqual(job["status"], "failed")
            self.assertEqual(job["error"], {"detail": "boom"})

    async def test_worker_updates_after_terminal_are_ignored(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            job_id = "measurement-job-stable"
            self._register_job(store, job_id)

            def _executor(_job):
                return {"message": "Measurement finished."}

            store._execute_capture_job = _executor
            await store._run_measurement_job(job_id)
            self.assertEqual(store.get_job(job_id)["status"], "completed")

            message_before = store.get_job(job_id)["message"]
            # Simulate a late worker-thread update arriving after terminal.
            store._update_measurement_job_message(job_id, "late message")
            store._update_capture_input_level_status(job_id, -12.0, False, channel_index=0)
            job = store.get_job(job_id)
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["message"], message_before)
            self.assertIsNone(job.get("input_level"))

    async def test_cancel_after_terminal_is_noop(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            job_id = "measurement-job-late-cancel"
            self._register_job(store, job_id, status="completed", result={"x": 1}, error=None)

            job = store.cancel_job(job_id)
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["result"], {"x": 1})
            self.assertEqual(store.get_job(job_id)["status"], "completed")

    async def test_cleanup_runs_exactly_once_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            job_id = "measurement-job-cleanup"
            self._register_job(store, job_id)
            capture = store.captures_dir / f"{job_id}.wav"
            playback = store.playbacks_dir / f"{job_id}.wav"
            capture.write_bytes(b"RIFF-test")
            playback.write_bytes(b"RIFF-test")

            store._cleanup_job_wav_files(job_id)
            self.assertFalse(capture.exists())
            self.assertFalse(playback.exists())
            # Second cleanup (e.g. re-entry) must be a harmless no-op.
            store._cleanup_job_wav_files(job_id)
            store._cleanup_lr_repeat_sweep_wavs(job_id)


class AutoSubJobStateTests(unittest.TestCase):
    def _job(self, **overrides):
        job = {
            "id": "autosub-job-1",
            "status": "running",
            "message": "running",
            "result": None,
            "error": None,
            "cancel_requested": False,
            "auto_gain": {"gain_db": 0.0},
        }
        job.update(overrides)
        return job

    def test_failed_job_with_cancel_request_stays_failed(self):
        job = self._job(status="failed", error={"detail": "boom"}, cancel_requested=True)
        _finalize_autosub_job(job, job["id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"], {"detail": "boom"})

    def test_completed_job_with_cancel_before_commit_becomes_cancelled(self):
        # cancel_requested is only ever set by the cancel endpoint on
        # non-terminal jobs, so completed+cancel_requested means the cancel
        # arrived before the worker committed completion.
        job = self._job(status="completed", result={"mode": "2.2"}, cancel_requested=True)
        _finalize_autosub_job(job, job["id"])
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["message"], "Auto Sub Optimize cancelled.")
        self.assertIsNone(job["result"])
        self.assertIsNone(job["error"])

    def test_completed_job_without_cancel_request_stays_completed(self):
        job = self._job(status="completed", result={"mode": "2.2"}, cancel_requested=False)
        _finalize_autosub_job(job, job["id"])
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["result"]["mode"], "2.2")

    def test_cancelling_job_is_promoted_to_cancelled(self):
        job = self._job(status="cancelling", cancel_requested=True)
        _finalize_autosub_job(job, job["id"])
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["message"], "Auto Sub Optimize cancelled.")

    def test_cancelled_job_stays_cancelled(self):
        job = self._job(status="cancelled", cancel_requested=True)
        _finalize_autosub_job(job, job["id"])
        self.assertEqual(job["status"], "cancelled")


if __name__ == "__main__":
    unittest.main(verbosity=2)
