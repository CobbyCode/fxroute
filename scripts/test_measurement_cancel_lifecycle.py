#!/usr/bin/env python3
"""Cancellation must not outpace the synchronous measurement worker."""

import asyncio
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from measurement import MeasurementStore


class MeasurementCancelLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_stays_cancelling_until_worker_exits(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = MeasurementStore(home=Path(tempdir))
            job_id = "measurement-job-cancel-test"
            store._jobs[job_id] = {
                "id": job_id,
                "status": "queued",
                "created_at": store._utc_now(),
                "updated_at": store._utc_now(),
                "message": "queued",
            }
            entered = threading.Event()
            release = threading.Event()

            def worker(_job):
                entered.set()
                release.wait(timeout=5)
                return {"message": "finished"}

            store._execute_capture_job = worker
            task = asyncio.create_task(store._run_measurement_job(job_id))
            store._job_tasks[job_id] = task
            await asyncio.to_thread(entered.wait, 2)

            cancelled = store.cancel_job(job_id)
            self.assertEqual(cancelled["status"], "cancelling")
            self.assertFalse(task.done())
            self.assertEqual(store._find_active_or_cancelling_job()["id"], job_id)
            self.assertTrue(store.has_active_measurement_job())

            release.set()
            await task
            self.assertEqual(store.get_job(job_id)["status"], "cancelled")
            self.assertFalse(store.has_active_measurement_job())

    async def test_cancelled_job_cannot_spawn_process(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = MeasurementStore(home=Path(tempdir))
            store._cancelled_jobs.add("job-1")
            with patch("measurement.subprocess.Popen") as popen:
                with self.assertRaisesRegex(RuntimeError, "Measurement cancelled"):
                    store._start_job_process("job-1", ["pw-play", "sweep.wav"])
            popen.assert_not_called()

    async def test_direct_task_cancel_waits_for_worker_exit(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = MeasurementStore(home=Path(tempdir))
            job_id = "measurement-job-task-cancel"
            store._jobs[job_id] = {
                "id": job_id,
                "status": "queued",
                "created_at": store._utc_now(),
                "updated_at": store._utc_now(),
                "message": "queued",
            }
            entered = threading.Event()
            release = threading.Event()

            def worker(_job):
                entered.set()
                release.wait(timeout=5)
                raise RuntimeError("Measurement cancelled.")

            store._execute_capture_job = worker
            task = asyncio.create_task(store._run_measurement_job(job_id))
            store._job_tasks[job_id] = task
            await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertEqual(store.get_job(job_id)["status"], "cancelling")
            release.set()
            await task
            self.assertEqual(store.get_job(job_id)["status"], "cancelled")

    async def test_cancel_before_runner_start_never_calls_worker(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = MeasurementStore(home=Path(tempdir))
            job_id = "measurement-job-queued-cancel"
            store._jobs[job_id] = {
                "id": job_id,
                "status": "queued",
                "created_at": store._utc_now(),
                "updated_at": store._utc_now(),
                "message": "queued",
            }
            worker_called = False

            def worker(_job):
                nonlocal worker_called
                worker_called = True
                return {"message": "finished"}

            store._execute_capture_job = worker
            cancelled = store.cancel_job(job_id)
            self.assertEqual(cancelled["status"], "cancelling")
            await store._run_measurement_job(job_id)
            self.assertFalse(worker_called)
            self.assertEqual(store.get_job(job_id)["status"], "cancelled")

    async def test_repeated_task_cancel_cannot_interrupt_worker_drain(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = MeasurementStore(home=Path(tempdir))
            job_id = "measurement-job-repeat-cancel"
            store._jobs[job_id] = {
                "id": job_id,
                "status": "queued",
                "created_at": store._utc_now(),
                "updated_at": store._utc_now(),
                "message": "queued",
            }
            entered = threading.Event()
            release = threading.Event()

            def worker(_job):
                entered.set()
                release.wait(timeout=5)
                raise RuntimeError("Measurement cancelled.")

            store._execute_capture_job = worker
            task = asyncio.create_task(store._run_measurement_job(job_id))
            store._job_tasks[job_id] = task
            await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertEqual(store.get_job(job_id)["status"], "cancelling")
            release.set()
            await task
            self.assertEqual(store.get_job(job_id)["status"], "cancelled")

    async def test_cancel_before_coroutine_entry_finalizes_queued_job(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = MeasurementStore(home=Path(tempdir))
            job_id = "measurement-job-never-entered"
            store._jobs[job_id] = {
                "id": job_id,
                "status": "queued",
                "created_at": store._utc_now(),
                "updated_at": store._utc_now(),
                "message": "queued",
            }
            task = asyncio.create_task(store._run_measurement_job(job_id))
            store._job_tasks[job_id] = task
            task.add_done_callback(
                lambda completed: store._measurement_job_task_done(job_id, completed)
            )
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(0)
            self.assertTrue(task.cancelled())
            self.assertEqual(store.get_job(job_id)["status"], "cancelled")

    async def test_spawn_wins_lock_then_cancel_terminates_registered_process(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = MeasurementStore(home=Path(tempdir))
            job_id = "measurement-job-spawn-race"
            store._jobs[job_id] = {
                "id": job_id,
                "status": "running",
                "created_at": store._utc_now(),
                "updated_at": store._utc_now(),
                "message": "running",
            }
            popen_entered = threading.Event()
            allow_popen = threading.Event()

            class Process:
                terminated = False

                def poll(self):
                    return None

                def terminate(self):
                    self.terminated = True

            process = Process()

            def fake_popen(*_args, **_kwargs):
                popen_entered.set()
                allow_popen.wait(timeout=5)
                return process

            with patch("measurement.subprocess.Popen", side_effect=fake_popen):
                spawn = asyncio.create_task(
                    asyncio.to_thread(store._start_job_process, job_id, ["pw-record"])
                )
                await asyncio.to_thread(popen_entered.wait, 2)
                cancel = asyncio.create_task(asyncio.to_thread(store.cancel_job, job_id))
                await asyncio.sleep(0.02)
                self.assertFalse(cancel.done())
                allow_popen.set()
                self.assertIs(await spawn, process)
                await cancel
            self.assertTrue(process.terminated)

    async def test_lr_repeat_child_uses_parent_owner(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = MeasurementStore(home=Path(tempdir))
            parent_id = "measurement-repeat-job-parent"
            store._jobs[parent_id] = {"id": parent_id, "status": "running"}
            captured = []

            def capture(child_job):
                captured.append(child_job)
                raise RuntimeError("stop after ownership assertion")

            store._execute_capture_job = capture
            with self.assertRaisesRegex(RuntimeError, "ownership assertion"):
                store._execute_lr_repeat_job(
                    {"id": parent_id, "repeat_count": 1, "job_kind": "lr-repeat"}
                )
            self.assertEqual(captured[0]["_owner_job_id"], parent_id)

    async def test_cancel_cannot_overwrite_terminal_transition(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = MeasurementStore(home=Path(tempdir))
            job_id = "measurement-job-terminal-race"
            store._jobs[job_id] = {
                "id": job_id,
                "status": "running",
                "created_at": store._utc_now(),
                "updated_at": store._utc_now(),
                "message": "running",
            }
            with store._job_process_lock:
                cancel = asyncio.create_task(asyncio.to_thread(store.cancel_job, job_id))
                await asyncio.sleep(0.02)
                self.assertFalse(cancel.done())
                store._jobs[job_id]["status"] = "completed"
                store._jobs[job_id]["message"] = "finished"
            result = await cancel
            self.assertEqual(result["status"], "completed")
            self.assertEqual(store.get_job(job_id)["status"], "completed")

    async def test_shutdown_cancels_and_drains_running_jobs(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = MeasurementStore(home=Path(tempdir))
            job_id = "measurement-job-shutdown"
            store._jobs[job_id] = {
                "id": job_id,
                "status": "queued",
                "created_at": store._utc_now(),
                "updated_at": store._utc_now(),
                "message": "queued",
            }
            entered = threading.Event()

            def worker(_job):
                entered.set()
                while job_id not in store._cancelled_jobs:
                    threading.Event().wait(0.01)
                raise RuntimeError("Measurement cancelled.")

            store._execute_capture_job = worker
            task = asyncio.create_task(store._run_measurement_job(job_id))
            store._job_tasks[job_id] = task
            await asyncio.to_thread(entered.wait, 2)

            await store.shutdown()

            self.assertTrue(task.done())
            self.assertEqual(store.get_job(job_id)["status"], "cancelled")
            self.assertFalse(store.has_active_measurement_job())
            self.assertTrue(store._shutdown)

    async def test_shutdown_kills_process_that_ignores_terminate_before_drain(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = MeasurementStore(home=Path(tempdir))
            job_id = "measurement-job-stubborn-process"
            store._jobs[job_id] = {
                "id": job_id,
                "status": "queued",
                "created_at": store._utc_now(),
                "updated_at": store._utc_now(),
                "message": "queued",
            }

            class Process:
                returncode = None
                killed = False

                def poll(self):
                    return self.returncode

                def terminate(self):
                    pass

                def kill(self):
                    self.killed = True
                    self.returncode = -9

                def wait(self, _timeout):
                    if self.returncode is None:
                        raise __import__("subprocess").TimeoutExpired("measurement", 3)
                    return self.returncode

            process = Process()
            store._job_processes[job_id] = [process]

            def worker(_job):
                while process.returncode is None:
                    threading.Event().wait(0.01)
                raise RuntimeError("Measurement cancelled.")

            store._execute_capture_job = worker
            task = asyncio.create_task(store._run_measurement_job(job_id))
            store._job_tasks[job_id] = task
            await asyncio.sleep(0.02)

            await store.shutdown()

            self.assertTrue(process.killed)
            self.assertTrue(task.done())
            self.assertEqual(store.get_job(job_id)["status"], "cancelled")


if __name__ == "__main__":
    unittest.main(verbosity=2)
