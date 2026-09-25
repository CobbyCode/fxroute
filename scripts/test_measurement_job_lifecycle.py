#!/usr/bin/env python3
"""Measurement job-lifecycle residuals: persist-safe guard and atomic start slot."""

import asyncio
import copy
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from measurement.store import MeasurementStore


def _fake_inputs(inputs):
    return inputs


def _make_store(tempdir):
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


class MeasurementPersistGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_persist_failure_does_not_break_has_active(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = _make_store(tempdir)
            stale_id = "measurement-job-stale-persist"
            store._jobs[stale_id] = {
                "id": stale_id,
                "status": "running",
                "created_at": store._utc_now(),
                "updated_at": store._utc_now(),
                "message": "Running sweep…",
            }
            real_persist = store._persistence._persist_job

            def fail_for_stale(job):
                if job.get("id") == stale_id:
                    raise OSError("disk full")
                return real_persist(job)

            with patch.object(store._persistence, "_persist_job", side_effect=fail_for_stale):
                # Predicate for the 423/ownership guard must not raise.
                try:
                    active = store.has_active_measurement_job()
                except OSError:
                    self.fail("has_active_measurement_job raised OSError")
                self.assertFalse(active)
                # In-memory promotion is kept even though the disk write failed.
                self.assertEqual(store._jobs[stale_id]["status"], "cancelled")
                self.assertIn("no live worker", store._jobs[stale_id]["message"])
                # A new measurement can still take the slot.
                job = await store.start_measurement(input_id="mic", channel="left")
                task = store._job_tasks[job["id"]]
                await asyncio.wait_for(task, timeout=5)
                self.assertEqual(store.get_job(job["id"])["status"], "completed")

    async def test_stale_persist_failure_does_not_break_output_commit_guard(self):
        from audio.output_service import OutputService, OutputServiceDeps
        from audio.output_state_store import OutputStateStore

        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = _make_store(tempdir)
            stale_id = "measurement-job-stale-commit-guard"
            store._jobs[stale_id] = {
                "id": stale_id,
                "status": "running",
                "created_at": store._utc_now(),
                "updated_at": store._utc_now(),
                "message": "Running sweep…",
            }
            with patch.object(
                store._persistence, "_persist_job", side_effect=OSError("disk full")
            ):
                # The guard predicate itself must stay callable.
                try:
                    active = store.has_active_measurement_job()
                except OSError:
                    self.fail("commit guard predicate raised OSError")
                self.assertFalse(active)

                output_path = Path(tempdir) / "output-state.json"
                service = OutputService(OutputServiceDeps(
                    store=OutputStateStore(output_path),
                    preset_loader=lambda name: {"chain": []},
                    resolve_ir=lambda kernel: {},
                    measurement_active=store.has_active_measurement_job,
                ))
                state = service.load()
                candidate = copy.deepcopy(state)
                # Stale-only activity must not lock the commit (no 423) and
                # must not surface the disk error as a 500 either.
                try:
                    committed = service.commit_unowned(
                        candidate, expected_revision=state["revision"])
                except OSError:
                    self.fail("output commit raised the stale persist OSError")
                self.assertEqual(committed["revision"], state["revision"] + 1)

    async def test_resurrected_file_job_persist_failure_stays_in_memory(self):
        import json

        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = MeasurementStore(home=Path(tempdir))
            job_id = "measurement-job-resurrected-persist"
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
                json.dumps(record), encoding="utf-8")
            with patch.object(
                store._persistence, "_persist_job", side_effect=OSError("disk full")
            ):
                try:
                    loaded = store.get_job(job_id)
                except OSError:
                    self.fail("get_job raised stale persist OSError")
                self.assertEqual(loaded["status"], "cancelled")
                self.assertIn("no live worker", loaded["message"])
                self.assertFalse(store.has_active_measurement_job())


class MeasurementConcurrentStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_starts_single_winner_early_rejection(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = _make_store(tempdir)
            real_cached = store._cached_capture_inputs
            calls = {"count": 0}

            def slow_cached():
                calls["count"] += 1
                time.sleep(0.4)
                return real_cached()

            store._cached_capture_inputs = slow_cached

            results = await asyncio.gather(
                store.start_measurement(input_id="mic", channel="left"),
                store.start_measurement(input_id="mic", channel="left"),
                return_exceptions=True,
            )
            successes = [r for r in results if isinstance(r, dict)]
            failures = [r for r in results if isinstance(r, BaseException)]
            self.assertEqual(len(successes), 1)
            self.assertEqual(len(failures), 1)
            self.assertIsInstance(failures[0], RuntimeError)
            self.assertIn("Another measurement is still active", str(failures[0]))
            # The loser is rejected before the first await (input discovery).
            self.assertEqual(calls["count"], 1)
            # Exactly one job was registered.
            self.assertEqual(len(store._jobs), 1)
            self.assertFalse(store._is_start_slot_reserved())
            task = store._job_tasks[successes[0]["id"]]
            await asyncio.wait_for(task, timeout=5)
            self.assertEqual(store.get_job(successes[0]["id"])["status"], "completed")
            self.assertFalse(store.has_active_measurement_job())

    async def test_preparation_error_releases_reservation(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = _make_store(tempdir)
            with self.assertRaisesRegex(ValueError, "channel must be"):
                await store.start_measurement(input_id="mic", channel="bogus")
            self.assertFalse(store._is_start_slot_reserved())
            self.assertFalse(store.has_active_measurement_job())
            self.assertEqual(len(store._jobs), 0)
            # Next start works normally.
            job = await store.start_measurement(input_id="mic", channel="left")
            task = store._job_tasks[job["id"]]
            await asyncio.wait_for(task, timeout=5)
            self.assertEqual(store.get_job(job["id"])["status"], "completed")

    async def test_post_prepare_validation_error_releases_reservation(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = _make_store(tempdir)
            with self.assertRaisesRegex(ValueError, "measurement_role must be"):
                await store.start_measurement(
                    input_id="mic", channel="left", measurement_role="bogus-role")
            self.assertFalse(store._is_start_slot_reserved())
            self.assertFalse(store.has_active_measurement_job())
            job = await store.start_measurement(input_id="mic", channel="left")
            task = store._job_tasks[job["id"]]
            await asyncio.wait_for(task, timeout=5)
            self.assertEqual(store.get_job(job["id"])["status"], "completed")

    async def test_cancellation_during_preparation_releases_reservation(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = _make_store(tempdir)
            real_cached = store._cached_capture_inputs

            def slow_cached():
                time.sleep(0.5)
                return real_cached()

            store._cached_capture_inputs = slow_cached
            task = asyncio.create_task(
                store.start_measurement(input_id="mic", channel="left"))
            await asyncio.sleep(0.1)
            self.assertTrue(store._is_start_slot_reserved())
            self.assertTrue(store.has_active_measurement_job())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(store._is_start_slot_reserved())
            self.assertFalse(store.has_active_measurement_job())
            self.assertEqual(
                [j for j in store._jobs.values()
                 if str(j.get("status") or "") not in {"completed", "failed", "cancelled"}],
                [],
            )
            job = await store.start_measurement(input_id="mic", channel="left")
            worker = store._job_tasks[job["id"]]
            await asyncio.wait_for(worker, timeout=5)
            self.assertEqual(store.get_job(job["id"])["status"], "completed")

    async def test_register_persist_failure_leaves_no_orphan(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = _make_store(tempdir)
            with patch.object(
                store._persistence, "_persist_job", side_effect=OSError("disk full")
            ):
                with self.assertRaises(OSError):
                    await store.start_measurement(input_id="mic", channel="left")
            self.assertFalse(store._is_start_slot_reserved())
            self.assertFalse(store.has_active_measurement_job())
            self.assertEqual(len(store._jobs), 0)
            job = await store.start_measurement(input_id="mic", channel="left")
            task = store._job_tasks[job["id"]]
            await asyncio.wait_for(task, timeout=5)
            self.assertEqual(store.get_job(job["id"])["status"], "completed")

    async def test_normal_start_finish_cancel_unchanged(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = _make_store(tempdir)
            job = await store.start_measurement(input_id="mic", channel="left")
            self.assertTrue(store.has_active_measurement_job())
            task = store._job_tasks[job["id"]]
            await asyncio.wait_for(task, timeout=5)
            self.assertEqual(store.get_job(job["id"])["status"], "completed")
            self.assertFalse(store.has_active_measurement_job())

            repeat = None
            store._execute_lr_repeat_job = lambda _job: {"message": "repeat done"}
            repeat = await store.start_lr_repeat_measurement(input_id="mic")
            # The repeat job was registered with the mocked executor; drain it.
            repeat_task = store._job_tasks[repeat["id"]]
            await asyncio.wait_for(repeat_task, timeout=5)
            self.assertEqual(store.get_job(repeat["id"])["status"], "completed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
