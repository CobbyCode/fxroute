#!/usr/bin/env python3
"""SPL calibration ownership, cancellation, and cleanup regressions."""

import asyncio
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import main
import spl_calibration


class FakeSession:
    def __init__(self):
        self.active_ids = set()
        self.unregistered = []

    async def register_spl_job(self, job_id, entry_epoch=None):
        self.active_ids.add(job_id)

    async def unregister_spl_job(self, job_id):
        self.active_ids.discard(job_id)
        self.unregistered.append(job_id)


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9

    def wait(self, timeout=None):
        self.wait_calls += 1
        self.returncode = self.returncode if self.returncode is not None else 0
        return self.returncode


class NaturalExitProcess(FakeProcess):
    def __init__(self):
        super().__init__()
        self.exit_event = threading.Event()

    def wait(self, timeout=None):
        self.wait_calls += 1
        if not self.exit_event.wait(timeout):
            raise subprocess.TimeoutExpired("pw-play", timeout)
        self.returncode = 0
        return 0


class StubbornProcess(FakeProcess):
    def wait(self, timeout=None):
        self.wait_calls += 1
        if self.returncode is None:
            raise subprocess.TimeoutExpired("pw-record", timeout)
        return self.returncode


class FakeRequest:
    async def json(self):
        return {"enabled": True}


class SplCalibrationLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_session = main.measurement_sr_session
        self.original_manager = main.easyeffects_manager
        self.original_get_volume = main.get_output_volume
        self.original_set_volume = main.set_output_volume
        spl_calibration._runtime.operation = None
        spl_calibration._runtime.operation_lock = asyncio.Lock()

    async def asyncTearDown(self):
        if spl_calibration._runtime.operation is not None:
            await spl_calibration._stop_active_operation()
        main.measurement_sr_session = self.original_session
        main.easyeffects_manager = self.original_manager
        main.get_output_volume = self.original_get_volume
        main.set_output_volume = self.original_set_volume
        spl_calibration._runtime.operation = None
        spl_calibration._runtime.operation_lock = None

    async def test_double_start_is_rejected_until_owner_cleanup_finishes(self):
        operation = await spl_calibration._acquire_operation("automatic")
        with self.assertRaises(Exception) as raised:
            await spl_calibration._acquire_operation("manual-noise")
        self.assertEqual(getattr(raised.exception, "status_code", None), 409)

        await spl_calibration._cleanup_operation(operation)
        successor = await spl_calibration._acquire_operation("manual-noise")
        self.assertNotEqual(operation.session_job_id, successor.session_job_id)
        await spl_calibration._cleanup_operation(successor)

    async def test_stop_during_automatic_run_drains_process_and_session_owner(self):
        session = FakeSession()
        main.measurement_sr_session = session
        operation = await spl_calibration._acquire_operation("automatic")
        operation.registration_attempted = True
        operation.session = session
        session.active_ids.add(operation.session_job_id)
        recorder = FakeProcess()
        operation.recorder = recorder

        async def worker():
            try:
                while not operation.cancel_requested:
                    await asyncio.sleep(0)
            finally:
                await spl_calibration._cleanup_operation_shielded(operation)

        operation.worker_task = asyncio.create_task(worker())
        await spl_calibration._stop_active_operation()

        self.assertTrue(operation.completed.is_set())
        self.assertGreaterEqual(recorder.terminate_calls, 1)
        self.assertGreaterEqual(recorder.wait_calls, 1)
        self.assertEqual(session.active_ids, set())
        self.assertEqual(session.unregistered, [operation.session_job_id])
        self.assertIsNone(spl_calibration._runtime.operation)

    async def test_natural_noise_exit_restores_and_releases_ownership(self):
        session = FakeSession()
        main.measurement_sr_session = session
        operation = await spl_calibration._acquire_operation("manual-noise")
        operation.registration_attempted = True
        operation.session = session
        session.active_ids.add(operation.session_job_id)
        process = NaturalExitProcess()
        operation.noise_process = process
        watcher = asyncio.create_task(spl_calibration._watch_manual_noise(operation))
        operation.worker_task = watcher

        replacement_session = FakeSession()
        main.measurement_sr_session = replacement_session
        process.exit_event.set()
        await asyncio.wait_for(operation.completed.wait(), timeout=2)
        await watcher

        self.assertEqual(process.returncode, 0)
        self.assertEqual(session.active_ids, set())
        self.assertEqual(replacement_session.unregistered, [])
        self.assertIsNone(spl_calibration._runtime.operation)

    async def test_registration_cancellation_cannot_leave_stale_session_id(self):
        class CancellingSession(FakeSession):
            async def register_spl_job(self, job_id, entry_epoch=None):
                self.active_ids.add(job_id)
                raise asyncio.CancelledError

        session = CancellingSession()
        main.measurement_sr_session = session
        with self.assertRaises(asyncio.CancelledError):
            await spl_calibration.set_spl_calibration_noise(FakeRequest())

        self.assertEqual(session.active_ids, set())
        self.assertEqual(len(session.unregistered), 1)
        self.assertIsNone(spl_calibration._runtime.operation)

    async def test_request_cancellation_drains_owned_thread_before_cleanup(self):
        entered = threading.Event()
        release = threading.Event()
        thread_observations = []

        def delayed_start(operation):
            entered.set()
            release.wait(timeout=2)
            thread_observations.append(operation.cancel_requested)
            return {"status": "playing"}

        with mock.patch.object(
            spl_calibration,
            "_register_operation",
            mock.AsyncMock(),
        ), mock.patch.object(
            spl_calibration,
            "_start_spl_calibration_noise",
            side_effect=delayed_start,
        ):
            request_task = asyncio.create_task(
                spl_calibration.set_spl_calibration_noise(FakeRequest())
            )
            self.assertTrue(
                await asyncio.wait_for(asyncio.to_thread(entered.wait), timeout=2)
            )
            operation = spl_calibration._runtime.operation
            request_task.cancel()
            await asyncio.sleep(0)

            self.assertIs(spl_calibration._runtime.operation, operation)
            self.assertFalse(operation.completed.is_set())
            self.assertEqual(thread_observations, [])

            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await request_task

        self.assertEqual(thread_observations, [True])
        self.assertTrue(operation.completed.is_set())
        self.assertIsNone(spl_calibration._runtime.operation)

    async def test_cleanup_fallback_removes_session_id_after_cancelled_unregister(self):
        class FailingSession:
            def __init__(self):
                self.active_spl_job_ids = set()
                self.lock = asyncio.Lock()
                self.release_checks = 0

            async def unregister_spl_job(self, _job_id):
                raise asyncio.CancelledError

            async def _check_release(self):
                self.release_checks += 1

        session = FailingSession()
        operation = await spl_calibration._acquire_operation("automatic")
        operation.registration_attempted = True
        operation.session = session
        session.active_spl_job_ids.add(operation.session_job_id)

        await spl_calibration._cleanup_operation_shielded(operation)

        self.assertEqual(session.active_spl_job_ids, set())
        self.assertEqual(session.release_checks, 1)
        self.assertTrue(operation.completed.is_set())
        self.assertIsNone(spl_calibration._runtime.operation)

    async def test_stop_timeout_keeps_owner_and_blocks_successor(self):
        operation = await spl_calibration._acquire_operation("automatic")
        blocker = asyncio.create_task(asyncio.Event().wait())
        operation.worker_task = blocker
        with mock.patch.object(spl_calibration, "SPL_STOP_TIMEOUT_SECONDS", 0.01):
            with self.assertRaisesRegex(RuntimeError, "Timed out"):
                await spl_calibration._stop_active_operation()

        self.assertIs(spl_calibration._runtime.operation, operation)
        with self.assertRaises(Exception) as raised:
            await spl_calibration._acquire_operation("manual-noise")
        self.assertEqual(getattr(raised.exception, "status_code", None), 409)
        blocker.cancel()
        await asyncio.gather(blocker, return_exceptions=True)
        operation.worker_task = None
        await spl_calibration._cleanup_operation(operation)

    async def test_unparseable_capture_gain_is_not_blindly_restored(self):
        operation = await spl_calibration._acquire_operation("automatic")
        operation.source_node = "test-source"
        operation.source_volume_percent = 42.0
        result = subprocess.CompletedProcess([], 0, stdout="unparseable", stderr="")
        with mock.patch.object(spl_calibration.subprocess, "run", return_value=result) as run:
            await spl_calibration._cleanup_operation(operation)

        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][:2], ["pactl", "get-source-volume"])

    def test_process_kill_is_followed_by_reap(self):
        process = StubbornProcess()
        spl_calibration._terminate_and_reap(process)
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 1)
        self.assertEqual(process.wait_calls, 2)

    def test_restore_failure_does_not_skip_other_resources(self):
        class Manager:
            def __init__(self):
                self.values = {
                    ("autogain", "bypass"): "true",
                    ("loudness", "outputGain"): "0",
                    ("loudness", "bypass"): "true",
                }
                self.writes = []

            def get_active_plugin_property(self, plugin, _instance, name):
                return self.values[(plugin, name)]

            def set_active_plugin_property(self, plugin, _instance, name, value):
                self.writes.append((plugin, name, value))
                if plugin == "autogain":
                    raise RuntimeError("autogain restore failed")

        manager = Manager()
        main.easyeffects_manager = manager
        main.get_output_volume = lambda: 100
        volume_writes = []
        main.set_output_volume = volume_writes.append
        operation = spl_calibration._SplCalibrationOperation(
            id="restore",
            kind="manual-noise",
            session_job_id="spl-calibration:restore",
            restore_state={
                "autogain_bypass": False,
                "loudness_bypass": False,
                "loudness_output_gain": 12.5,
                "system_volume_percent": 37,
            },
        )

        with mock.patch.object(spl_calibration.time, "sleep", return_value=None):
            spl_calibration._restore_spl_calibration_audio(operation)

        self.assertEqual(volume_writes, [37])
        self.assertIn(("loudness", "outputGain", 12.5), manager.writes)
        self.assertIn(("loudness", "bypass", False), manager.writes)
        self.assertIsNone(operation.restore_state)

    def test_cleanup_preserves_newer_external_gain_and_volume(self):
        class Manager:
            def __init__(self):
                self.writes = []

            def get_active_plugin_property(self, plugin, _instance, name):
                return {
                    ("autogain", "bypass"): "false",
                    ("loudness", "outputGain"): "4.0",
                    ("loudness", "bypass"): "false",
                }[(plugin, name)]

            def set_active_plugin_property(self, plugin, _instance, name, value):
                self.writes.append((plugin, name, value))

        manager = Manager()
        main.easyeffects_manager = manager
        main.get_output_volume = lambda: 55
        volume_writes = []
        main.set_output_volume = volume_writes.append
        operation = spl_calibration._SplCalibrationOperation(
            id="stale",
            kind="manual-noise",
            session_job_id="spl-calibration:stale",
            restore_state={
                "autogain_bypass": True,
                "loudness_bypass": True,
                "loudness_output_gain": 0.0,
                "system_volume_percent": 37,
            },
        )

        spl_calibration._restore_spl_calibration_audio(operation)

        self.assertEqual(volume_writes, [])
        self.assertEqual(manager.writes, [])

    def test_panel_close_always_requests_idempotent_server_stop(self):
        app = (
            pathlib.Path(__file__).resolve().parents[1] / "static" / "app.js"
        ).read_text()
        stop_body = app.split("async function stopSplCalibrationOperation", 1)[1].split(
            "async function closeSplCalibration()", 1
        )[0]
        close_body = app.split("async function closeSplCalibration() {", 1)[1].split(
            "async function toggleSplCalibrationNoise()", 1
        )[0]
        self.assertIn("stopSplCalibrationOperation()", close_body)
        self.assertIn("/api/measurements/spl-calibration/noise", stop_body)
        self.assertNotIn("if (splCalibrationNoiseActive)", close_body)


    async def test_stale_ownership_without_resources_is_recoverable(self):
        operation = await spl_calibration._acquire_operation("automatic")
        done = asyncio.create_task(asyncio.sleep(0))
        await done
        operation.worker_task = done
        session = FakeSession()
        operation.session = session
        operation.registration_attempted = True
        operation.completed.set()

        successor = await spl_calibration._acquire_operation("manual-noise")

        self.assertNotEqual(successor.id, operation.id)
        self.assertIs(spl_calibration._runtime.operation, successor)
        await spl_calibration._cleanup_operation(successor)

    async def test_live_process_keeps_stale_ownership_blocking(self):
        operation = await spl_calibration._acquire_operation("automatic")
        done = asyncio.create_task(asyncio.sleep(0))
        await done
        operation.worker_task = done
        operation.completed.set()
        process = FakeProcess()
        operation.noise_process = process

        with self.assertRaises(Exception) as raised:
            await spl_calibration._acquire_operation("manual-noise")
        self.assertEqual(getattr(raised.exception, "status_code", None), 409)
        self.assertIs(spl_calibration._runtime.operation, operation)
        process.returncode = 0
        await spl_calibration._cleanup_operation(operation)

    async def test_active_session_id_keeps_stale_ownership_blocking(self):
        operation = await spl_calibration._acquire_operation("automatic")
        done = asyncio.create_task(asyncio.sleep(0))
        await done
        operation.worker_task = done
        operation.completed.set()
        session = FakeSession()
        operation.session = session
        operation.registration_attempted = True
        session.active_ids.add(operation.session_job_id)
        session.active_spl_job_ids = session.active_ids

        with self.assertRaises(Exception) as raised:
            await spl_calibration._acquire_operation("manual-noise")
        self.assertEqual(getattr(raised.exception, "status_code", None), 409)
        self.assertIs(spl_calibration._runtime.operation, operation)
        await spl_calibration._cleanup_operation(operation)


    async def test_fresh_operation_mid_setup_is_not_reaped(self):
        operation = await spl_calibration._acquire_operation("automatic")
        with self.assertRaises(Exception) as raised:
            await spl_calibration._acquire_operation("manual-noise")
        self.assertEqual(getattr(raised.exception, "status_code", None), 409)
        self.assertIs(spl_calibration._runtime.operation, operation)
        await spl_calibration._cleanup_operation(operation)

    async def test_unfinished_lifecycle_is_not_reaped_even_without_resources(self):
        operation = await spl_calibration._acquire_operation("automatic")
        done = asyncio.create_task(asyncio.sleep(0))
        await done
        operation.worker_task = done
        with self.assertRaises(Exception) as raised:
            await spl_calibration._acquire_operation("manual-noise")
        self.assertEqual(getattr(raised.exception, "status_code", None), 409)
        self.assertIs(spl_calibration._runtime.operation, operation)
        await spl_calibration._cleanup_operation(operation)

    async def test_registration_in_progress_is_not_reaped(self):
        operation = await spl_calibration._acquire_operation("automatic")
        done = asyncio.create_task(asyncio.sleep(0))
        await done
        operation.worker_task = done
        session = FakeSession()
        operation.session = session
        operation.registration_attempted = True
        session.active_ids.add(operation.session_job_id)
        session.active_spl_job_ids = session.active_ids
        with self.assertRaises(Exception) as raised:
            await spl_calibration._acquire_operation("manual-noise")
        self.assertEqual(getattr(raised.exception, "status_code", None), 409)
        self.assertIs(spl_calibration._runtime.operation, operation)
        await spl_calibration._cleanup_operation(operation)


if __name__ == "__main__":
    unittest.main(verbosity=2)
