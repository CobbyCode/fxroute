#!/usr/bin/env python3
"""Regressions: Measurement entry vs. close (entry-invalidation epoch).

Covers the P2 fix: request_close() invalidates in-flight measurement start
requests that captured the session's entry epoch before the close.  A stale
entry must abort before any 48 kHz / playback / ownership side effect.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import autosub
import main
import measurement_session
import spl_calibration
from fastapi import HTTPException
from measurement_session import (
    MeasurementEntryInvalidated,
    MeasurementSampleRateSession,
)


class _TestAudioHarness:
    """Replace main/measurement_session globals; restore them afterwards.

    Fake audio only: no real PipeWire, force-rate, playback or subwoofer state.
    """

    def __init__(self) -> None:
        self._saved: dict[tuple[str, str], object] = {}
        self.force_rate = 44100
        self.set_force_calls: list[int] = []
        self.session: MeasurementSampleRateSession | None = None

    def _remember(self, module, name: str) -> None:
        self._saved[(module.__name__, name)] = getattr(module, name)

    def __enter__(self):
        m = main
        ms = measurement_session
        for name in (
            "get_samplerate_status",
            "_set_pipewire_force_rate",
            "_get_current_pipewire_force_rate",
            "_coordinator_current_playback_context",
            "_run_coordinated_transition",
            "_sync_subwoofer_runtime_at_rate",
            "_ensure_playback_samplerate_force",
            "_wait_for_samplerate_alignment",
            "playback_transition_coordinator",
            "current_track_info",
            "player_instance",
            "_begin_playback_transition_attempt",
            "_end_playback_transition_attempt",
        ):
            self._remember(m, name)
        self._remember(ms, "_capture_playback_state_before_measurement")
        self._remember(ms.samplerate, "load_sample_rate_policy")
        m.get_samplerate_status = self._get_samplerate_status
        m._set_pipewire_force_rate = self._set_force
        m._get_current_pipewire_force_rate = lambda: self.force_rate
        m._coordinator_current_playback_context = self._playback_context
        m._run_coordinated_transition = self._run_transition
        m._sync_subwoofer_runtime_at_rate = self._noop
        m._ensure_playback_samplerate_force = self._noop
        m._wait_for_samplerate_alignment = self._noop
        m.playback_transition_coordinator = None
        m.current_track_info = None
        m.player_instance = None
        m._begin_playback_transition_attempt = lambda: 0
        m._end_playback_transition_attempt = lambda: None
        ms._capture_playback_state_before_measurement = lambda *a, **k: None
        ms.samplerate.load_sample_rate_policy = lambda: {"mode": "auto", "rate": 0}
        self.session = MeasurementSampleRateSession()
        m.measurement_sr_session = self.session
        return self

    def __exit__(self, *exc) -> None:
        for (module_name, name), value in self._saved.items():
            module = sys.modules[module_name]
            setattr(module, name, value)

    def _get_samplerate_status(self) -> dict:
        return {"force_rate": self.force_rate, "active_rate": self.force_rate}

    def _set_force(self, rate: int) -> None:
        self.set_force_calls.append(rate)
        self.force_rate = rate

    async def _playback_context(self) -> dict:
        return {"source": "local", "target_url": None, "target_track": {}, "should_play": False}

    async def _run_transition(self, request):
        self.force_rate = request.target_rate
        return SimpleNamespace(committed=True, target_rate=request.target_rate)

    async def _noop(self, *args, **kwargs):
        return True


class MeasurementEntryEpochTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.harness = _TestAudioHarness()
        self.harness.__enter__()
        self.session = self.harness.session

    async def asyncTearDown(self):
        self.harness.__exit__(None, None, None)

    def assert_session_idle(self):
        session = self.session
        self.assertFalse(session.active)
        self.assertFalse(session.entry_in_progress)
        self.assertEqual(session.active_manual_job_ids, set())
        self.assertEqual(session.active_spl_job_ids, set())
        self.assertIsNone(session.active_auto_sub_job_id)
        self.assertEqual(self.harness.force_rate, 44100)

    async def test_close_before_entry_commit_rejects_stale_entry(self):
        """Race A: close fully processed while entry waits before registration."""
        epoch_a = self.session.capture_entry_epoch()
        gate = asyncio.Event()
        release = asyncio.Event()

        async def entry():
            gate.set()
            await release.wait()
            await self.session.register_manual_job("sweep-a", entry_epoch=epoch_a)

        task = asyncio.create_task(entry())
        await gate.wait()

        await self.session.request_close()
        self.assertEqual(self.session._entry_epoch, 1)
        self.assertEqual(self.session.generation, 0, "close must not touch release generation")
        self.assertTrue(self.session.close_requested)
        self.assertTrue(self.session.deferred_release_pending)

        release.set()
        with self.assertRaises(MeasurementEntryInvalidated):
            await task

        self.assert_session_idle()
        self.assertTrue(self.session.close_requested, "stale entry must not clear close_requested")
        self.assertEqual(self.harness.set_force_calls, [], "no 48 kHz side effect")

    async def test_manual_start_endpoint_409_when_close_interleaves(self):
        """API-level race: close during the pre-registration upload await."""
        fake_services = SimpleNamespace(
            get_store=lambda: SimpleNamespace(),
            get_session=lambda: self.session,
            auto_sub_active=lambda: False,
        )
        upload_entered = asyncio.Event()
        upload_release = asyncio.Event()

        async def blocked_read_upload(_file, _limit):
            upload_entered.set()
            await upload_release.wait()
            return b"cal"

        with patch.object(measurement_session, "_services", fake_services), patch.object(
            measurement_session, "read_upload", blocked_read_upload
        ):
            task = asyncio.create_task(
                measurement_session.start_measurement(
                    input_id="mic-1",
                    input_key="",
                    channel="left",
                    mic_input_channel="1",
                    reference_input_channel="",
                    calibration_ref="",
                    calibration_file=SimpleNamespace(filename="cal.txt"),
                    measurement_role="",
                )
            )
            await asyncio.wait_for(upload_entered.wait(), timeout=2)
            await self.session.request_close()
            upload_release.set()
            with self.assertRaises(HTTPException) as raised:
                await task

        self.assertEqual(raised.exception.status_code, 409)
        self.assert_session_idle()

    async def test_lr_repeat_endpoint_409_when_close_interleaves(self):
        fake_services = SimpleNamespace(
            get_store=lambda: SimpleNamespace(),
            get_session=lambda: self.session,
            auto_sub_active=lambda: False,
        )
        upload_entered = asyncio.Event()
        upload_release = asyncio.Event()

        async def blocked_read_upload(_file, _limit):
            upload_entered.set()
            await upload_release.wait()
            return b"cal"

        with patch.object(measurement_session, "_services", fake_services), patch.object(
            measurement_session, "read_upload", blocked_read_upload
        ):
            task = asyncio.create_task(
                measurement_session.start_lr_repeat_measurement(
                    input_id="mic-1",
                    input_key="",
                    base_name="test",
                    mic_input_channel="1",
                    reference_input_channel="",
                    calibration_ref="",
                    calibration_file=SimpleNamespace(filename="cal.txt"),
                )
            )
            await asyncio.wait_for(upload_entered.wait(), timeout=2)
            await self.session.request_close()
            upload_release.set()
            with self.assertRaises(HTTPException) as raised:
                await task

        self.assertEqual(raised.exception.status_code, 409)
        self.assert_session_idle()

    async def test_committed_entry_close_deferred_release(self):
        """Race B: entry commits first, close defers until job ends."""
        epoch = self.session.capture_entry_epoch()
        await self.session.register_manual_job("job-1", entry_epoch=epoch)
        self.assertTrue(self.session.active)
        self.assertEqual(self.harness.force_rate, 48000)

        await self.session.request_close()
        self.assertTrue(self.session.active)
        self.assertTrue(self.session.close_requested)
        self.assertTrue(self.session.deferred_release_pending)

        await self.session.unregister_manual_job("job-1")
        self.assertFalse(self.session.active)
        self.assertFalse(self.session.close_requested)

    async def test_new_start_after_close_works(self):
        """Race C: a fresh explicit start after a completed close is valid."""
        await self.session.request_close()
        self.assertEqual(self.session._entry_epoch, 1)

        new_epoch = self.session.capture_entry_epoch()
        await self.session.register_manual_job("job-2", entry_epoch=new_epoch)

        self.assertTrue(self.session.active)
        self.assertEqual(self.harness.force_rate, 48000)
        await self.session.unregister_manual_job("job-2")

    async def test_direct_start_fallback_without_token(self):
        """Direct callers without a captured token keep working."""
        await self.session.start(48000)
        self.assertTrue(self.session.active)
        self.assertEqual(self.harness.force_rate, 48000)

    async def test_request_open_does_not_bump_entry_epoch(self):
        await self.session.request_open()
        self.assertEqual(self.session._entry_epoch, 0)

    async def test_auto_sub_stale_token_rejected_no_owner(self):
        epoch_a = self.session.capture_entry_epoch()
        await self.session.request_close()
        with self.assertRaises(MeasurementEntryInvalidated):
            await self.session.register_auto_sub("auto-1", entry_epoch=epoch_a)
        self.assert_session_idle()
        self.assertIsNone(self.session.active_auto_sub_job_id)

    async def test_spl_stale_token_rejected_no_owner(self):
        epoch_a = self.session.capture_entry_epoch()
        await self.session.request_close()
        with self.assertRaises(MeasurementEntryInvalidated):
            await self.session.register_spl_job("spl-1", entry_epoch=epoch_a)
        self.assert_session_idle()
        self.assertEqual(self.session.active_spl_job_ids, set())

    async def test_auto_sub_worker_stale_token_aborts_controlled(self):
        """Auto-Sub worker registers after close → cancelled, no job owner."""
        epoch_a = self.session.capture_entry_epoch()
        await self.session.request_close()

        job_id = "auto-sub-epoch-test"
        job = {
            "id": job_id,
            "status": "preparing",
            "cancel_requested": False,
            "current_sweep_id": "",
            "result": None,
            "error": None,
            "message": "",
        }
        autosub._AUTO_SUB_JOBS[job_id] = job
        autosub._auto_sub_lock = asyncio.Lock()
        await autosub._auto_sub_lock.acquire()
        try:
            with patch.object(main, "measurement_sr_session", self.session), patch.object(
                autosub, "_finish_auto_sub_worker", new=AsyncMock()
            ) as finish:
                await autosub._run_auto_sub_22_optimize(
                    job_id=job_id,
                    input_id="mic-1",
                    mic_input_channel="1",
                    reference_input_channel="",
                    calibration_ref="",
                    calibration_filename=None,
                    calibration_bytes=None,
                    sub1_scan_delays=[0.0],
                    sub2_scan_delays=[0.0],
                    fc=80,
                    original_config_snapshot={},
                    fine_step_ms=0.1,
                    entry_epoch=epoch_a,
                )
            finish.assert_awaited_once_with(job, job_id)
        finally:
            if autosub._auto_sub_lock.locked():
                autosub._auto_sub_lock.release()
            autosub._AUTO_SUB_JOBS.pop(job_id, None)

        self.assertEqual(job["status"], "cancelled")
        self.assertIn("measurement window was closed", job["message"])
        self.assert_session_idle()

    async def test_spl_operation_captures_and_validates_epoch(self):
        spl_calibration._runtime.operation = None
        spl_calibration._runtime.operation_lock = asyncio.Lock()
        try:
            operation = await spl_calibration._acquire_operation("automatic")
            self.assertEqual(operation.entry_epoch, self.session.capture_entry_epoch())

            await self.session.request_close()
            operation.session = self.session
            with self.assertRaises(MeasurementEntryInvalidated):
                await spl_calibration._register_operation(operation)

            self.assert_session_idle()
        finally:
            await spl_calibration._cleanup_operation(operation)
            spl_calibration._runtime.operation = None


if __name__ == "__main__":
    unittest.main(verbosity=2)
