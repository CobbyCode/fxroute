#!/usr/bin/env python3
"""SPL calibration ownership, cancellation, and cleanup regressions."""

import asyncio
import copy
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import main
import measurement.session as measurement_session
import measurement.spl_calibration as spl_calibration
from dsp.manager import DSPManager
from playback.transition import OutputHeldFailure, TransitionRequest


class FakeSession:
    def __init__(self, events=None):
        self.active_ids = set()
        self.unregistered = []
        self.events = events if events is not None else []

    async def register_spl_job(self, job_id, entry_epoch=None):
        self.active_ids.add(job_id)

    async def unregister_spl_job(self, job_id):
        self.active_ids.discard(job_id)
        self.unregistered.append(job_id)
        self.events.append("unregister")

    def keep_playback_paused_on_release(self):
        self.events.append("keep-paused")


class FakeOutputMute:
    """Gate sink mute behind the SPL dependencies; never touches PipeWire."""

    sink_name = "alsa_output.spl-test"

    def __init__(self, events):
        self.muted = False
        self.writes = []
        self.ignore_writes = False
        self.events = events

    def sink(self):
        return self.sink_name

    def read(self, sink):
        assert sink == self.sink_name
        return self.muted

    def set(self, sink, muted):
        assert sink == self.sink_name
        self.writes.append(muted)
        self.events.append(("mute", muted))
        if not self.ignore_writes:
            self.muted = muted


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


class StopExitProcess(NaturalExitProcess):
    def terminate(self):
        super().terminate()
        self.exit_event.set()


class StubbornProcess(FakeProcess):
    def wait(self, timeout=None):
        self.wait_calls += 1
        if self.returncode is None:
            raise subprocess.TimeoutExpired("pw-record", timeout)
        return self.returncode


class UnkillableProcess(StubbornProcess):
    def kill(self):
        self.kill_calls += 1


class FakeRequest:
    def __init__(self, enabled=True):
        self.enabled = enabled

    async def json(self):
        return {"enabled": self.enabled}


class SplCalibrationLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_session = main.measurement_sr_session
        self.original_manager = main.dsp_manager
        self.original_get_volume = main.get_output_volume
        self.original_set_volume = main.set_output_volume
        self.original_mute = (main._spl_output_mute_sink, main._read_sink_mute, main._set_sink_mute)
        self.events = []
        self.output = FakeOutputMute(self.events)
        main._spl_output_mute_sink = self.output.sink
        main._read_sink_mute = self.output.read
        main._set_sink_mute = self.output.set
        spl_calibration._runtime.operation = None
        spl_calibration._runtime.operation_lock = asyncio.Lock()
        spl_calibration._runtime.unrestored = None
        # The pending-restore record must never reach the real state dir.
        self.state_home = tempfile.TemporaryDirectory(prefix="fxroute-spl-state-")
        self.state_env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.state_home.name})
        self.state_env.start()
        self.restore_record = pathlib.Path(self.state_home.name) / "fxroute" / "spl-calibration-restore.json"

    async def asyncTearDown(self):
        if spl_calibration._runtime.operation is not None:
            await spl_calibration._stop_active_operation()
        main.measurement_sr_session = self.original_session
        main.dsp_manager = self.original_manager
        main.get_output_volume = self.original_get_volume
        main.set_output_volume = self.original_set_volume
        main._spl_output_mute_sink, main._read_sink_mute, main._set_sink_mute = self.original_mute
        spl_calibration._runtime.operation = None
        spl_calibration._runtime.operation_lock = None
        spl_calibration._runtime.unrestored = None
        self.state_env.stop()
        self.state_home.cleanup()

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

    async def test_repeated_manual_start_and_duplicate_stop_release_every_owner(self):
        session = FakeSession()
        main.measurement_sr_session = session
        processes = []

        async def register(operation):
            operation.session = session
            operation.registration_attempted = True
            await session.register_spl_job(operation.session_job_id)

        def start(operation):
            process = StopExitProcess()
            processes.append(process)
            operation.noise_process = process
            operation.noise_links = [("noise:output_FL", "fxroute_dsp_sink:playback_FL")]
            return {"status": "playing"}

        missing = subprocess.CompletedProcess([], 1, stdout="", stderr="failed to unlink ports: No such file or directory")
        with mock.patch.object(spl_calibration, "_register_operation", side_effect=register), \
                mock.patch.object(spl_calibration, "_start_spl_calibration_noise", side_effect=start), \
                mock.patch.object(spl_calibration.subprocess, "run", return_value=missing):
            try:
                for _ in range(3):
                    result = await spl_calibration.set_spl_calibration_noise(FakeRequest())
                    self.assertEqual(result["status"], "playing")
                    operation = spl_calibration._runtime.operation
                    results = await asyncio.gather(
                        spl_calibration.set_spl_calibration_noise(FakeRequest(False)),
                        spl_calibration.set_spl_calibration_noise(FakeRequest(False)),
                        return_exceptions=True,
                    )
                    await asyncio.gather(operation.worker_task, return_exceptions=True)
                    self.assertEqual(results, [{"status": "stopped"}, {"status": "stopped"}])
                    self.assertIsNone(spl_calibration._runtime.operation)
                    self.assertEqual(session.active_ids, set())
                    self.assertIsNone(operation.noise_links)
                    self.assertIsNone(operation.restore_state)
                self.assertTrue(all(p.poll() is not None for p in processes))
            finally:
                if spl_calibration._runtime.operation is not None:
                    with mock.patch.object(spl_calibration.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
                        await spl_calibration._cleanup_operation(spl_calibration._runtime.operation)

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

    def test_restore_failure_keeps_state_for_retry(self):
        class Manager:
            temporary_runtime_transition_callback = object()

            def load_global_extras(self):
                return {
                    "autogain": {"enabled": True, "params": {}},
                    "loudness": {"enabled": True, "params": {}},
                }

            def apply_temporary_effects_runtime(self, _previous, _candidate):
                raise RuntimeError("native transition failed")

        manager = Manager()
        main.dsp_manager = manager
        main.get_output_volume = lambda: 100
        volume_writes = []
        main.set_output_volume = volume_writes.append
        operation = spl_calibration._SplCalibrationOperation(
            id="restore",
            kind="manual-noise",
            session_job_id="spl-calibration:restore",
            restore_state={
                "native_effects_extras": {
                    "autogain": {"enabled": True, "params": {}},
                    "loudness": {"enabled": True, "params": {}},
                },
                "neutral_effects_extras": {
                    "autogain": {"enabled": False, "params": {}},
                    "loudness": {"enabled": False, "params": {}},
                },
                "system_volume_percent": 37,
            },
        )

        with self.assertRaisesRegex(RuntimeError, "native transition failed"):
            spl_calibration._restore_spl_calibration_audio(operation)

        self.assertEqual(volume_writes, [37])
        self.assertIsNotNone(operation.restore_state)

    def test_volume_restore_failure_defers_gain_bearing_dsp_restore(self):
        class Manager:
            def __init__(self):
                self.transitions = 0

            def load_global_extras(self):
                return {"loudness": {"enabled": True}}

            def apply_temporary_effects_runtime(self, _previous, _candidate):
                self.transitions += 1

        manager = Manager()
        main.dsp_manager = manager
        volume = [100.0]
        fail_volume = [True]
        main.get_output_volume = lambda: volume[0]

        def set_volume(value):
            if fail_volume[0]:
                raise RuntimeError("volume unavailable")
            volume[0] = value

        main.set_output_volume = set_volume
        operation = spl_calibration._SplCalibrationOperation(
            id="volume-restore", kind="manual-noise", session_job_id="spl-calibration:volume-restore",
            restore_state={
                "system_volume_percent": 37.0,
                "native_effects_extras": {"loudness": {"enabled": True}},
                "neutral_effects_extras": {"loudness": {"enabled": False}},
            },
        )

        with self.assertRaisesRegex(RuntimeError, "volume unavailable"):
            spl_calibration._restore_spl_calibration_audio(operation)
        self.assertEqual(manager.transitions, 0)
        self.assertIsNotNone(operation.restore_state)

        fail_volume[0] = False
        spl_calibration._restore_spl_calibration_audio(operation)
        self.assertEqual(volume, [37.0])
        self.assertEqual(manager.transitions, 1)
        self.assertIsNone(operation.restore_state)

    async def test_dsp_restore_failure_blocks_new_operation_until_retry_succeeds(self):
        class Manager:
            def __init__(self):
                self.fail_restore = True
                self.restores = 0

            def load_global_extras(self):
                return {"autogain": {"enabled": True}, "loudness": {"enabled": True}}

            def apply_temporary_effects_runtime(self, _previous, _candidate):
                self.restores += 1
                if self.fail_restore:
                    raise RuntimeError("DSP restore unavailable")

        manager = Manager()
        main.dsp_manager = manager
        volume = [100.0]
        main.get_output_volume = lambda: volume[0]
        main.set_output_volume = lambda value: volume.__setitem__(0, value)
        operation = await spl_calibration._acquire_operation("manual-noise")
        operation.restore_state = {
            "system_volume_percent": 37.0,
            "native_effects_extras": {"autogain": {"enabled": True}},
            "neutral_effects_extras": {"autogain": {"enabled": False}},
        }
        session = FakeSession()
        operation.session = session
        operation.registration_attempted = True
        session.active_ids.add(operation.session_job_id)

        with self.assertRaisesRegex(RuntimeError, "cleanup did not release"):
            await spl_calibration._cleanup_operation(operation)

        self.assertTrue(operation.completed.is_set())
        self.assertIs(spl_calibration._runtime.operation, operation)
        self.assertIsNotNone(operation.restore_state)
        self.assertEqual(volume, [37.0])
        self.assertEqual(session.active_ids, {operation.session_job_id})
        done = asyncio.create_task(asyncio.sleep(0))
        await done
        operation.worker_task = done
        with self.assertRaises(Exception) as raised:
            await spl_calibration._acquire_operation("automatic")
        self.assertEqual(getattr(raised.exception, "status_code", None), 409)

        manager.fail_restore = False
        await spl_calibration._stop_active_operation()
        self.assertEqual(manager.restores, 2)
        self.assertIsNone(operation.restore_state)
        self.assertEqual(session.active_ids, set())
        self.assertIsNone(spl_calibration._runtime.operation)
        successor = await spl_calibration._acquire_operation("automatic")
        await spl_calibration._cleanup_operation(successor)

    async def _operation_with_failing_restore(self, volume=37.0):
        class Manager:
            restores = 0
            fail_restore = True

            def load_global_extras(self):
                return {"autogain": {"enabled": True}, "loudness": {"enabled": True}}

            def apply_temporary_effects_runtime(self, _previous, _candidate):
                self.restores += 1
                if self.fail_restore:
                    raise RuntimeError("DSP restore unavailable")

        manager = Manager()
        main.dsp_manager = manager
        main.get_output_volume = lambda: volume
        main.set_output_volume = lambda value: None
        operation = await spl_calibration._acquire_operation("manual-noise")
        operation.restore_state = {
            "system_volume_percent": 37.0,
            "native_effects_extras": {"autogain": {"enabled": True}},
            "neutral_effects_extras": {"autogain": {"enabled": False}},
        }
        session = FakeSession(self.events)
        session.active_spl_job_ids = session.active_ids
        operation.session = session
        operation.registration_attempted = True
        session.active_ids.add(operation.session_job_id)
        with self.assertRaisesRegex(RuntimeError, "cleanup did not release"):
            await spl_calibration._cleanup_operation(operation)
        # The operation's own failed cleanup keeps ownership; it does not mute.
        self.assertEqual(self.output.writes, [])
        done = asyncio.create_task(asyncio.sleep(0))
        await done
        operation.worker_task = done
        return operation, session, manager

    async def test_stop_ends_operation_behind_confirmed_mute_when_restore_retry_fails(self):
        operation, session, manager = await self._operation_with_failing_restore()
        with self.assertRaises(main.HTTPException) as refused:
            await spl_calibration._acquire_operation("automatic")
        self.assertEqual(refused.exception.status_code, 409)
        self.assertIn("did not finish its cleanup", refused.exception.detail)

        with self.assertRaises(main.HTTPException) as stopped:
            await spl_calibration._stop_active_operation()

        self.assertEqual(stopped.exception.status_code, 500)
        self.assertIn("without restoring its audio state", stopped.exception.detail)
        self.assertIn("muted for safety", stopped.exception.detail)
        self.assertIn("37.0%", stopped.exception.detail)
        self.assertEqual(manager.restores, 2)
        self.assertIsNone(operation.restore_state)
        self.assertEqual(session.active_ids, set())
        self.assertEqual(session.unregistered, [operation.session_job_id])
        self.assertIsNone(spl_calibration._runtime.operation)
        # The mute is confirmed and playback held paused before the session
        # job is released (its release could otherwise resume playback).
        self.assertEqual(self.events, [("mute", True), "keep-paused", "unregister"])
        self.assertTrue(self.output.muted)
        status = spl_calibration._unrestored_audio_status()
        self.assertEqual(status["output_muted"], True)
        self.assertEqual(status["previous_system_volume_percent"], 37.0)
        self.assertFalse(status["audio_restored"])
        self.assertIn("muted for safety", status["detail"])

        # A new start would capture the unrestored state as its baseline.
        with self.assertRaises(main.HTTPException) as refused:
            await spl_calibration._acquire_operation("automatic")
        self.assertEqual(refused.exception.status_code, 409)
        self.assertIn("retry the restore first", refused.exception.detail)

        # A failing restore retry keeps the mute and the pending state.
        with self.assertRaises(main.HTTPException) as retried:
            await spl_calibration.set_spl_calibration_noise(FakeRequest(enabled=False))
        self.assertEqual(retried.exception.status_code, 500)
        self.assertEqual(manager.restores, 3)
        self.assertTrue(self.output.muted)
        self.assertIsNotNone(spl_calibration._runtime.unrestored)

        # A successful retry restores the audio, then lifts its own mute.
        manager.fail_restore = False
        stopped = await spl_calibration.set_spl_calibration_noise(FakeRequest(enabled=False))
        self.assertEqual(stopped, {"status": "stopped"})
        self.assertEqual(manager.restores, 4)
        self.assertFalse(self.output.muted)
        self.assertEqual(self.output.writes, [True, False])
        self.assertIsNone(spl_calibration._runtime.unrestored)
        self.assertIsNone(spl_calibration._unrestored_audio_status())
        successor = await spl_calibration._acquire_operation("automatic")
        await spl_calibration._cleanup_operation(successor)

    async def test_unconfirmed_mute_keeps_ownership_and_restore_state(self):
        operation, session, manager = await self._operation_with_failing_restore()
        self.output.ignore_writes = True

        with self.assertRaisesRegex(RuntimeError, "cleanup did not release"):
            await spl_calibration._stop_active_operation()

        self.assertIs(spl_calibration._runtime.operation, operation)
        self.assertIsNotNone(operation.restore_state)
        self.assertEqual(session.active_ids, {operation.session_job_id})
        self.assertIsNone(spl_calibration._runtime.unrestored)
        self.assertNotIn("keep-paused", self.events)
        with self.assertRaises(main.HTTPException) as refused:
            await spl_calibration._acquire_operation("automatic")
        self.assertIn("did not finish its cleanup", refused.exception.detail)

        self.output.ignore_writes = False
        with self.assertRaises(main.HTTPException):
            await spl_calibration._stop_active_operation()
        self.assertIsNone(spl_calibration._runtime.operation)
        self.assertTrue(self.output.muted)
        self.assertEqual(session.active_ids, set())

    async def test_unavailable_mute_control_keeps_ownership(self):
        operation, session, _manager = await self._operation_with_failing_restore()

        def refuse(_sink, _muted):
            raise RuntimeError("pactl set-sink-mute failed")

        main._set_sink_mute = refuse
        with self.assertRaisesRegex(RuntimeError, "cleanup did not release"):
            await spl_calibration._stop_active_operation()
        self.assertIs(spl_calibration._runtime.operation, operation)
        self.assertIsNotNone(operation.restore_state)
        self.assertEqual(session.active_ids, {operation.session_job_id})
        self.assertIsNone(spl_calibration._runtime.unrestored)

        main._set_sink_mute = self.output.set
        with self.assertRaises(main.HTTPException):
            await spl_calibration._stop_active_operation()
        self.assertIsNone(spl_calibration._runtime.operation)

    async def test_failed_volume_restore_keeps_calibration_volume_behind_mute(self):
        volume = [100.0]
        operation, session, manager = await self._operation_with_failing_restore()
        main.get_output_volume = lambda: volume[0]

        def refuse_volume(_value):
            raise RuntimeError("volume write failed")

        main.set_output_volume = refuse_volume
        with self.assertRaises(main.HTTPException) as stopped:
            await spl_calibration._stop_active_operation()
        self.assertIn("volume write failed", stopped.exception.detail)
        self.assertTrue(self.output.muted)
        self.assertIsNone(spl_calibration._runtime.operation)

        # The retry restores the calibration volume before unmuting.
        main.set_output_volume = lambda value: volume.__setitem__(0, value)
        manager.fail_restore = False
        await spl_calibration.set_spl_calibration_noise(FakeRequest(enabled=False))
        self.assertEqual(volume, [37.0])
        self.assertFalse(self.output.muted)
        self.assertIsNone(spl_calibration._runtime.unrestored)

    async def test_restore_retry_keeps_a_mute_that_predates_the_calibration(self):
        self.output.muted = True
        operation, session, manager = await self._operation_with_failing_restore()
        with self.assertRaises(main.HTTPException):
            await spl_calibration._stop_active_operation()
        self.assertEqual(self.output.writes, [])
        self.assertIsNone(spl_calibration._runtime.operation)

        manager.fail_restore = False
        stopped = await spl_calibration.set_spl_calibration_noise(FakeRequest(enabled=False))
        self.assertEqual(stopped, {"status": "stopped"})
        self.assertTrue(self.output.muted)
        self.assertEqual(self.output.writes, [])
        self.assertIsNone(spl_calibration._runtime.unrestored)

    async def test_shutdown_retries_a_pending_restore(self):
        _operation, _session, manager = await self._operation_with_failing_restore()
        with self.assertRaises(main.HTTPException):
            await spl_calibration._stop_active_operation()
        manager.fail_restore = False
        await spl_calibration.shutdown()
        self.assertIsNone(spl_calibration._runtime.unrestored)
        self.assertFalse(self.output.muted)

    async def test_pending_restore_record_rearms_the_hold_after_a_restart(self):
        _operation, _session, manager = await self._operation_with_failing_restore()
        self.assertIsNone(spl_calibration.output_hold())
        with self.assertRaises(main.HTTPException):
            await spl_calibration._stop_active_operation()
        self.assertIn("SPL Calibration", spl_calibration.output_hold())
        record = json.loads(self.restore_record.read_text(encoding="utf-8"))
        self.assertEqual(record["sink"], FakeOutputMute.sink_name)
        self.assertFalse(record["output_was_muted"])
        self.assertEqual(record["previous_volume_percent"], 37.0)
        self.assertEqual(record["restore_state"]["system_volume_percent"], 37.0)

        # A failed retry keeps the record with its latest error.
        main.get_output_volume = lambda: 100.0

        def refuse_volume(_value):
            raise RuntimeError("volume write failed")

        main.set_output_volume = refuse_volume
        with self.assertRaises(main.HTTPException):
            await spl_calibration.set_spl_calibration_noise(FakeRequest(enabled=False))
        record = json.loads(self.restore_record.read_text(encoding="utf-8"))
        self.assertEqual(record["error"], "volume write failed")

        # A restart drops the in-memory state; the record re-arms it.
        spl_calibration._runtime.unrestored = None
        self.assertIsNone(spl_calibration.output_hold())
        spl_calibration.load_pending_restore()
        self.assertIsNotNone(spl_calibration.output_hold())
        status = spl_calibration._unrestored_audio_status()
        self.assertEqual(status["previous_system_volume_percent"], 37.0)
        self.assertTrue(status["output_muted"])
        self.assertIn("volume write failed", status["detail"])
        with self.assertRaises(main.HTTPException) as refused:
            await spl_calibration._acquire_operation("automatic")
        self.assertEqual(refused.exception.status_code, 409)

        # The retry restores the volume, lifts the calibration's own mute
        # and removes the record.
        volume = [100.0]
        main.get_output_volume = lambda: volume[0]
        main.set_output_volume = lambda value: volume.__setitem__(0, value)
        manager.fail_restore = False
        stopped = await spl_calibration.set_spl_calibration_noise(FakeRequest(enabled=False))
        self.assertEqual(stopped, {"status": "stopped"})
        self.assertEqual(volume, [37.0])
        self.assertFalse(self.output.muted)
        self.assertFalse(self.restore_record.exists())
        self.assertIsNone(spl_calibration.output_hold())
        spl_calibration.load_pending_restore()
        self.assertIsNone(spl_calibration._runtime.unrestored)

    async def test_unreadable_restore_record_holds_without_touching_the_mute(self):
        self.restore_record.parent.mkdir(parents=True)
        self.restore_record.write_text("{not json", encoding="utf-8")
        spl_calibration.load_pending_restore()
        self.assertIsNotNone(spl_calibration.output_hold())
        status = spl_calibration._unrestored_audio_status()
        self.assertIn("unreadable", status["detail"])
        self.assertIsNone(status["output_muted"])

        stopped = await spl_calibration.set_spl_calibration_noise(FakeRequest(enabled=False))
        self.assertEqual(stopped, {"status": "stopped"})
        self.assertEqual(self.output.writes, [])
        self.assertIsNone(spl_calibration.output_hold())
        self.assertFalse(self.restore_record.exists())

    async def test_stop_keeps_ownership_while_noise_survives_failed_restore(self):
        operation, session, manager = await self._operation_with_failing_restore()
        process = UnkillableProcess()
        operation.noise_process = process

        with self.assertRaisesRegex(RuntimeError, "cleanup did not release"):
            await spl_calibration._stop_active_operation()

        self.assertIs(spl_calibration._runtime.operation, operation)
        self.assertIsNotNone(operation.restore_state)
        self.assertEqual(session.active_ids, {operation.session_job_id})
        process.returncode = 0
        manager.apply_temporary_effects_runtime = lambda _previous, _candidate: None
        await spl_calibration._stop_active_operation()
        self.assertIsNone(spl_calibration._runtime.operation)
        self.assertEqual(session.active_ids, set())

    async def test_manual_noise_watcher_exception_is_logged(self):
        class FailingProcess:
            def wait(self):
                raise RuntimeError("noise wait failed")

            def poll(self):
                return 0

        started = []

        def start(operation):
            operation.noise_process = FailingProcess()
            started.append(operation)
            return {"status": "playing"}

        with mock.patch.object(spl_calibration, "_register_operation", mock.AsyncMock()), \
                mock.patch.object(spl_calibration, "_start_spl_calibration_noise", side_effect=start), \
                self.assertLogs(spl_calibration.logger, level="ERROR") as logged:
            await spl_calibration.set_spl_calibration_noise(FakeRequest())
            operation = started[0]
            await asyncio.wait_for(operation.completed.wait(), timeout=2)
            await asyncio.gather(operation.worker_task, return_exceptions=True)
            await asyncio.sleep(0)

        self.assertTrue(operation.worker_task.done())
        self.assertTrue(any("noise wait failed" in message for message in logged.output))

    def test_cleanup_preserves_newer_external_gain_and_volume(self):
        class Manager:
            temporary_runtime_transition_callback = object()

            def __init__(self):
                self.transitions = []

            def load_global_extras(self):
                return {
                    "autogain": {"enabled": True, "params": {}},
                    "loudness": {"enabled": True, "params": {}},
                }

            def apply_temporary_effects_runtime(self, previous, candidate):
                self.transitions.append((previous, candidate))

        manager = Manager()
        main.dsp_manager = manager
        main.get_output_volume = lambda: 55
        volume_writes = []
        main.set_output_volume = volume_writes.append
        operation = spl_calibration._SplCalibrationOperation(
            id="stale",
            kind="manual-noise",
            session_job_id="spl-calibration:stale",
            restore_state={
                "native_effects_extras": {
                    "autogain": {"enabled": True, "params": {}},
                    "loudness": {"enabled": True, "params": {}},
                },
                "neutral_effects_extras": {
                    "autogain": {"enabled": False, "params": {}},
                    "loudness": {"enabled": False, "params": {}},
                },
                "system_volume_percent": 37,
            },
        )

        spl_calibration._restore_spl_calibration_audio(operation)

        self.assertEqual(volume_writes, [])
        self.assertEqual(len(manager.transitions), 1)
        self.assertFalse(manager.transitions[0][0]["loudness"]["enabled"])
        self.assertTrue(manager.transitions[0][1]["loudness"]["enabled"])
        self.assertIsNone(operation.restore_state)

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


class SplOutputHoldWiringTests(unittest.IsolatedAsyncioTestCase):
    """A pending restore holds main's coordinator and measurement entry."""

    def setUp(self):
        spl_calibration._runtime.unrestored = None

    def tearDown(self):
        spl_calibration._runtime.unrestored = None

    def _hold(self):
        spl_calibration._runtime.unrestored = spl_calibration._UnrestoredSplAudio(
            restore_state=None, sink="alsa_output.spl-test", output_was_muted=False,
            previous_volume_percent=40, error="DSP restore unavailable")

    async def test_pending_restore_refuses_audible_play_as_409(self):
        coordinator = main._make_playback_orchestration_deps().make_transition_coordinator()
        self.assertIsNone(coordinator.output_hold)
        self._hold()
        self.assertEqual(coordinator.output_hold, spl_calibration.output_hold())
        self.assertTrue(coordinator.transition_blocked)
        self.assertEqual(coordinator.status()["output_hold"], spl_calibration.output_hold())

        with self.assertRaises(OutputHeldFailure) as refused:
            await coordinator.execute(TransitionRequest(
                operation="play", source="local", target_rate=44100,
                target_url="/music/track.flac", should_play=True))
        self.assertFalse(coordinator.gate.closed)
        error = main._transition_error_http(refused.exception)
        self.assertEqual(error.status_code, 409)
        self.assertEqual(error.detail, spl_calibration.output_hold())

    async def test_measurement_preflight_reports_the_hold(self):
        self._hold()
        coordinator = SimpleNamespace(
            output_hold=spl_calibration.output_hold(), transition_blocked=True)
        with mock.patch.object(main, "playback_transition_coordinator", coordinator):
            with self.assertRaises(main.HTTPException) as refused:
                await measurement_session._measurement_entry_preflight(48000)
        self.assertEqual(refused.exception.status_code, 409)
        self.assertIn("SPL Calibration", refused.exception.detail)


class SplCalibrationApplyLockTests(unittest.IsolatedAsyncioTestCase):
    """The calibration apply takes the external DSP mutation ownership."""

    OVERVIEW = {"selected_output": {"key": "dev", "target_label": "Dev"}}

    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.manager = DSPManager(home=pathlib.Path(directory.name) / "home")
        self.applied = []
        self.volume_lock_held = None

        def apply_autogain(previous, extras, *, persist_all_presets=True):
            self.volume_lock_held = main._canonical_volume_write_lock().locked()
            self.applied.append(copy.deepcopy(extras))
            return {"extras": extras}

        self.manager.apply_autogain_loudness_runtime = apply_autogain
        self.session = SimpleNamespace(has_active_jobs=False, entry_in_progress=False,
                                       lock=asyncio.Lock())
        self.previous_runtime = (main.measurement_sr_session, main.dsp_manager,
                                 main.measurement_session)
        main.measurement_sr_session = self.session
        main.dsp_manager = self.manager
        main.measurement_session = SimpleNamespace(_read_measurement_setup_settings=lambda: {})
        self.addCleanup(self._restore_runtime)

    def _restore_runtime(self):
        (main.measurement_sr_session, main.dsp_manager,
         main.measurement_session) = self.previous_runtime

    class Request:
        def __init__(self, measured):
            self._measured = measured

        async def json(self):
            return {"measured_spl_db": self._measured}

    async def test_apply_is_refused_while_a_measurement_owns_the_graph(self):
        self.session.has_active_jobs = True
        with mock.patch.object(spl_calibration, "get_audio_output_overview",
                               return_value=self.OVERVIEW):
            with self.assertRaises(Exception) as raised:
                await spl_calibration.apply_spl_calibration(self.Request(79.0))
        self.assertEqual(getattr(raised.exception, "status_code", None), 423)
        self.assertEqual(self.applied, [])

    async def test_apply_holds_the_canonical_volume_lock_and_persists_the_calibration(self):
        with mock.patch.object(spl_calibration, "get_audio_output_overview",
                               return_value=self.OVERVIEW), \
                mock.patch.object(spl_calibration, "_stop_active_operation", mock.AsyncMock()):
            result = await spl_calibration.apply_spl_calibration(self.Request(79.0))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["required_adjustment_db"], 4.0)
        self.assertFalse(result["calibrated"])
        self.assertTrue(self.volume_lock_held)
        self.assertFalse(main._canonical_volume_write_lock().locked())
        calibration = result["calibration"]
        self.assertEqual(calibration["measuredSplDb"], 79.0)
        self.assertEqual(calibration["outputProfileId"], "dev")
        self.assertEqual(self.applied[-1]["loudness"]["params"]["calibration"], calibration)
        self.assertEqual(self.applied[-1]["loudness"]["params"]["calibrationProfiles"]["dev"],
                         calibration)


if __name__ == "__main__":
    unittest.main(verbosity=2)
