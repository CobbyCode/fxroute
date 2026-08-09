#!/usr/bin/env python3
"""Stale measurement jobs without a live worker must not block new runs."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from measurement import MeasurementStore


def _fake_inputs(inputs):
    return inputs


class MeasurementStaleJobTests(unittest.IsolatedAsyncioTestCase):
    def _store(self, tempdir):
        store = MeasurementStore(home=Path(tempdir))
        store._discover_capture_inputs = lambda: [{
            "id": "mic",
            "label": "Mic",
            "node_serial": "serial-1",
            "node_name": "capture_1",
            "channels": 2,
            "sample_rate": 48_000,
            "available": True,
        }]
        store._measurement_inputs_with_sample_rate = _fake_inputs
        store._execute_capture_job = lambda _job: {"message": "finished"}
        return store

    async def test_stale_cancelling_job_does_not_block_new_measurement(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            stale_id = "measurement-job-stale-cancelling"
            done_task = asyncio.create_task(asyncio.sleep(0))
            await done_task
            store._job_tasks[stale_id] = done_task
            store._jobs[stale_id] = {
                "id": stale_id,
                "status": "cancelling",
                "created_at": store._utc_now(),
                "updated_at": store._utc_now(),
                "message": "Measurement cancelled.",
            }

            job = await store.start_measurement(input_id="mic", channel="left")

            self.assertEqual(store.get_job(stale_id)["status"], "cancelled")
            self.assertIn("no live worker", store.get_job(stale_id)["message"])
            task = store._job_tasks[job["id"]]
            await asyncio.wait_for(task, timeout=5)
            self.assertEqual(store.get_job(job["id"])["status"], "completed")

    async def test_stale_running_job_without_task_does_not_block_new_measurement(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            stale_id = "measurement-job-stale-running"
            store._jobs[stale_id] = {
                "id": stale_id,
                "status": "running",
                "created_at": store._utc_now(),
                "updated_at": store._utc_now(),
                "message": "Running sweep…",
            }

            job = await store.start_measurement(input_id="mic", channel="left")

            self.assertEqual(store.get_job(stale_id)["status"], "cancelled")
            task = store._job_tasks[job["id"]]
            await asyncio.wait_for(task, timeout=5)
            self.assertEqual(store.get_job(job["id"])["status"], "completed")

    async def test_live_job_still_blocks_new_measurement(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            live_id = "measurement-job-live"
            blocker = asyncio.create_task(asyncio.Event().wait())
            store._job_tasks[live_id] = blocker
            store._jobs[live_id] = {
                "id": live_id,
                "status": "running",
                "created_at": store._utc_now(),
                "updated_at": store._utc_now(),
                "message": "Running sweep…",
            }

            with self.assertRaisesRegex(RuntimeError, "Another measurement is still active"):
                await store.start_measurement(input_id="mic", channel="left")

            self.assertEqual(store.get_job(live_id)["status"], "running")
            self.assertTrue(store.has_active_measurement_job())
            blocker.cancel()
            await asyncio.gather(blocker, return_exceptions=True)

    async def test_persisted_running_job_is_normalized_after_reload(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = MeasurementStore(home=Path(tempdir))
            job_id = "measurement-job-persisted-running"
            record = {
                "id": job_id,
                "status": "running",
                "created_at": store._utc_now(),
                "updated_at": store._utc_now(),
                "message": "Running sweep…",
                "result": None,
                "error": None,
            }
            (store.job_records_dir / f"{job_id}.json").write_text(
                json.dumps(record), encoding="utf-8"
            )

            loaded = store.get_job(job_id)

            self.assertEqual(loaded["status"], "cancelled")
            self.assertIn("no live worker", loaded["message"])
            self.assertIsNone(store._find_active_or_cancelling_job())
            self.assertFalse(store.has_active_measurement_job())
            persisted = json.loads(
                (store.job_records_dir / f"{job_id}.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["status"], "cancelled")


    async def test_persisted_cancelling_job_is_normalized_after_reload(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = MeasurementStore(home=Path(tempdir))
            job_id = "measurement-job-persisted-cancelling"
            record = {
                "id": job_id,
                "status": "cancelling",
                "created_at": store._utc_now(),
                "updated_at": store._utc_now(),
                "message": "Measurement cancelled.",
                "result": None,
                "error": None,
            }
            (store.job_records_dir / f"{job_id}.json").write_text(
                json.dumps(record), encoding="utf-8"
            )

            loaded = store.get_job(job_id)

            self.assertEqual(loaded["status"], "cancelled")
            self.assertIn("no live worker", loaded["message"])
            self.assertIsNone(store._find_active_or_cancelling_job())
            self.assertFalse(store.has_active_measurement_job())

    async def test_persisted_terminal_jobs_are_preserved_on_reload(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = MeasurementStore(home=Path(tempdir))
            cases = [
                ("completed", {"marker": "completed-result"}, None),
                ("failed", None, {"detail": "boom"}),
                ("cancelled", None, None),
            ]
            for status, result, error in cases:
                job_id = f"measurement-job-{status}"
                record = {
                    "id": job_id,
                    "status": status,
                    "created_at": store._utc_now(),
                    "updated_at": store._utc_now(),
                    "message": f"finished as {status}",
                    "result": result,
                    "error": error,
                }
                (store.job_records_dir / f"{job_id}.json").write_text(
                    json.dumps(record), encoding="utf-8"
                )

                loaded = store.get_job(job_id)

                self.assertEqual(loaded["status"], status)
                self.assertEqual(loaded["result"], result)
                self.assertEqual(loaded["error"], error)
                self.assertIsNone(store._find_active_or_cancelling_job())
                persisted = json.loads(
                    (store.job_records_dir / f"{job_id}.json").read_text(encoding="utf-8")
                )
                self.assertEqual(persisted["status"], status)
                self.assertEqual(persisted["result"], result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
