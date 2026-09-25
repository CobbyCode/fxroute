#!/usr/bin/env python3
"""Residuals: shutdown vs preparation, early persist, deferred-restart ownership."""

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from measurement.store import MeasurementStore


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
    store._measurement_inputs_with_sample_rate = lambda inputs: inputs
    store._execute_capture_job = lambda _job: {"message": "finished"}
    store._execute_lr_repeat_job = lambda _job: {"message": "repeat done"}
    return store


class ShutdownDuringPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def _run_shutdown_race(self, starter):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = _make_store(tempdir)
            real_cached = store._cached_capture_inputs

            def slow_cached():
                time.sleep(0.5)
                return real_cached()

            store._cached_capture_inputs = slow_cached
            prep = asyncio.create_task(starter(store))
            try:
                deadline = time.monotonic() + 5
                while not store._is_start_slot_reserved() and time.monotonic() < deadline:
                    await asyncio.sleep(0.02)
                self.assertTrue(store._is_start_slot_reserved())
                await store.shutdown()
                with self.assertRaisesRegex(RuntimeError, "shutting down"):
                    await prep
                prep = None
                self.assertEqual(len(store._jobs), 0)
                self.assertFalse(store._is_start_slot_reserved())
                self.assertFalse(store.has_active_measurement_job())
                # Post-shutdown starts stay rejected.
                with self.assertRaisesRegex(RuntimeError, "shutting down"):
                    await starter(store)
            finally:
                if prep is not None and not prep.done():
                    prep.cancel()
                    try:
                        await prep
                    except BaseException:
                        pass

    async def test_shutdown_during_single_preparation_starts_no_job(self):
        await self._run_shutdown_race(
            lambda store: store.start_measurement(input_id="mic", channel="left"))

    async def test_shutdown_during_lr_repeat_preparation_starts_no_job(self):
        await self._run_shutdown_race(
            lambda store: store.start_lr_repeat_measurement(input_id="mic"))


class EarlyPersistFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_early_persist_failure_terminalizes_without_task_exception(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = _make_store(tempdir)
            runner = store._job_runner
            job_id = "measurement-job-early-persist"
            job = {"id": job_id, "status": "queued"}
            calls = {"persist": 0, "cleanup": 0, "retain": 0}
            real_persist = store._persistence._persist_job

            def flaky_persist(current):
                calls["persist"] += 1
                if calls["persist"] == 1:
                    raise OSError("disk full on running")
                return real_persist(current)

            runner._persist_job = flaky_persist
            runner._cleanup_job = lambda _jid: calls.__setitem__("cleanup", calls["cleanup"] + 1)
            runner._retain_history = lambda: calls.__setitem__("retain", calls["retain"] + 1)
            task = runner.start(job_id, job, lambda _j: {"message": "finished"})
            # Must not raise: early persist failure terminalizes inside run().
            await asyncio.wait_for(task, timeout=5)
            self.assertTrue(task.done())
            self.assertFalse(task.cancelled())
            self.assertIsNone(task.exception())
            self.assertEqual(job["status"], "failed")
            self.assertIn("disk full", job["message"])
            self.assertGreaterEqual(calls["persist"], 2)
            self.assertEqual(calls["cleanup"], 1)
            self.assertEqual(calls["retain"], 1)


class DeferredRestartOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_immediate_cancel_releases_restart_pending(self):
        import main
        main.update_lifecycle.finish_deferred_restart()
        self.assertFalse(main.update_lifecycle._restart_pending)
        try:
            main.update_lifecycle.reserve_deferred_restart()
            self.assertTrue(main.update_lifecycle._restart_pending)

            async def slow_restart():
                try:
                    await asyncio.sleep(10)
                finally:
                    main.update_lifecycle.finish_deferred_restart()

            task = main._create_lifecycle_background_task(slow_restart(), name="service-restart")
            task.add_done_callback(lambda _done: main.update_lifecycle.finish_deferred_restart())
            # Cancel before the first coroutine step runs.
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(0)
            self.assertFalse(main.update_lifecycle._restart_pending)
            # Maintenance guard is free again (no 409).
            try:
                async with main.update_lifecycle.maintenance_operation():
                    pass
            except Exception as exc:
                self.fail(f"maintenance still blocked after pre-start cancel: {exc!r}")
        finally:
            main.update_lifecycle.finish_deferred_restart()
            for pending in list(main.runtime.lifecycle_background_tasks):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(*main.runtime.lifecycle_background_tasks, return_exceptions=True)

    async def test_update_schedules_restart_with_pre_start_safe_ownership(self):
        import main
        from unittest.mock import patch
        main.update_lifecycle.finish_deferred_restart()
        for pending in list(main.runtime.lifecycle_background_tasks):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(*main.runtime.lifecycle_background_tasks, return_exceptions=True)
        self.assertFalse(main.update_lifecycle._restart_pending)

        captured = {}
        created = {"count": 0}

        async def fake_restart(service_name):
            captured["service"] = service_name
            try:
                await asyncio.sleep(10)
            finally:
                main.update_lifecycle.finish_deferred_restart()

        real_create = main._create_lifecycle_background_task

        def cancelling_create(coro, *, name):
            created["count"] += 1
            captured["task_name"] = name
            # Close the never-started coroutine to avoid warnings; its
            # finally would not run after a pre-start cancel anyway.
            try:
                coro.close()
            except Exception:
                pass

            async def already_cancelled():
                await asyncio.sleep(0)
                raise asyncio.CancelledError()

            task = real_create(already_cancelled(), name=name)
            # Pre-start cancellation: the restart coroutine finally never runs.
            task.cancel()
            return task

        async def fake_update(_timeout, *_args, on_result=None):
            result = {
                "returncode": 0,
                "stdout": "Pulling updates with fast-forward only.\n",
                "stderr": "",
            }
            if on_result:
                on_result(result)
            return result

        from starlette.requests import Request

        def headerless_request():
            return Request({
                "type": "http", "method": "POST", "path": "/api/system/update",
                "scheme": "http", "server": ("testserver", 80),
                "headers": [(b"host", b"testserver")], "query_string": b"",
            })

        try:
            with patch.object(main, "_run_update_operation", new=fake_update), \
                 patch.object(main, "_restart_fxroute_service_after_response", new=fake_restart), \
                 patch.object(main, "_create_lifecycle_background_task", new=cancelling_create), \
                 patch.object(main, "_read_version_file", return_value="9.9.9"), \
                 patch.object(main, "_configured_service_name", return_value="fxroute"):
                response = await main.system_update(headerless_request())
                self.assertTrue(response["restart_scheduled"])
                self.assertEqual(created["count"], 1)
                self.assertEqual(captured.get("task_name"), "service-restart")
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                tasks = list(main.runtime.lifecycle_background_tasks)
                for task in tasks:
                    try:
                        await asyncio.wait_for(task, timeout=2)
                    except asyncio.CancelledError:
                        pass
                await asyncio.sleep(0)
                self.assertFalse(
                    main.update_lifecycle._restart_pending,
                    "pre-start cancelled restart must not block maintenance forever",
                )
                try:
                    async with main.update_lifecycle.maintenance_operation():
                        pass
                except Exception as exc:
                    self.fail(f"maintenance still blocked with 409: {exc!r}")
        finally:
            main.update_lifecycle.finish_deferred_restart()
            for pending in list(main.runtime.lifecycle_background_tasks):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(*main.runtime.lifecycle_background_tasks, return_exceptions=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
