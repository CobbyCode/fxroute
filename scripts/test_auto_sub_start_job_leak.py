#!/usr/bin/env python3
"""Rejected AutoSub calibration uploads must not orphan ``preparing`` jobs.

The start route used to register the job in ``_AUTO_SUB_JOBS`` before reading
and validating the calibration upload. An HTTP 400 then released the lock but
no worker was ever started, so the job dict (full config snapshot, scan
delays) stayed in the map forever - the 600 s cleanup only runs from the
worker finalizer. Regression coverage:

* a too-large calibration upload leaves no job behind,
* a wrong content type leaves no job behind,
* a valid upload still registers the job and dispatches the runner.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import audio.samplerate as samplerate_module  # noqa: E402
import measurement.autosub as autosub  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from measurement.autosub import deps as autosub_deps  # noqa: E402
from measurement.autosub.runners import start as autosub_start  # noqa: E402


def overview_21(alignment=2.34):
    return {
        "selected_output": {"key": "mock", "channels": 4, "active_rate": 48000, "label": "Mock"},
        "output_mode": {
            "mode": "subwoofer-2.1",
            "crossover_frequency_hz": 80,
            "main_highpass_enabled": True,
            "subwoofer": {
                "crossover_frequency_hz": 80,
                "main_highpass_enabled": True,
                "sub_alignment_ms": alignment,
                "sub_level_db": -3.0,
                "sub_polarity": "normal",
            },
        },
    }


def mode_state():
    return {
        "mode": "subwoofer-2.1",
        "subwoofer": {
            "crossover_frequency_hz": 80,
            "main_highpass_enabled": True,
            "sub_alignment_ms": 2.34,
            "sub_level_db": -3.0,
            "sub_polarity": "normal",
        },
    }


class FakeUpload:
    filename = "calibration.txt"
    content_type = "text/plain"

    def __init__(self, data: bytes = b"", content_length: int | None = None):
        self._data = data
        self.content_length = content_length

    async def read(self, size: int = -1) -> bytes:
        data = self._data
        if size is not None and size > 0 and len(data) > size:
            data = data[:size]
        self._data = data[size:] if size is not None and size > 0 else b""
        return data


class FakeStore:
    def resolve_capture_input_id(self, input_id, input_key=""):
        return input_id

    def has_active_measurement_job(self):
        return False

    def cancel_job(self, job_id):
        pass


class FakeSession:
    def capture_entry_epoch(self):
        return 123


class AutoSubStartJobLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        autosub_deps.configure_dependencies(autosub.AutoSubDependencies(
            get_dsp_runtime=lambda: None,
            get_measurement_store=lambda: FakeStore(),
            get_measurement_session=lambda: FakeSession(),
            get_dsp_manager=lambda: None,
        ))
        try:
            autosub_start._auto_sub_lock.release()
        except RuntimeError:
            pass
        autosub_start._auto_sub_lock = asyncio.Lock()
        autosub_deps._AUTO_SUB_JOBS.clear()
        # Route-local bindings that shadow the module imports.
        self._patch = patch.object(
            autosub_start, "_capture_auto_sub_playback_gain",
            return_value={"enabled": False, "volume_db": 0.0, "linear": 1.0, "source": "hardware-sink"},
        )
        self._patch.start()
        self._samplerate_patch = patch.object(
            samplerate_module, "_load_audio_output_mode", return_value=mode_state()
        )
        self._samplerate_patch.start()
        self._overview_patch = patch.object(autosub_start, "get_audio_output_overview", return_value=overview_21())
        self._overview_patch.start()

    async def asyncTearDown(self) -> None:
        self._overview_patch.stop()
        self._samplerate_patch.stop()
        self._patch.stop()
        autosub_deps._AUTO_SUB_JOBS.clear()
        autosub_deps._autosub_deps = None
        try:
            autosub_start._auto_sub_lock.release()
        except RuntimeError:
            pass

    async def _start(self, calibration_file):
        return await autosub_start.start_auto_sub_optimize(
            input_id="input-1",
            input_key="",
            channel="left",
            mic_input_channel="1",
            reference_input_channel="",
            calibration_ref="",
            target_curve_snapshot="",
            calibration_file=calibration_file,
        )

    async def test_oversized_calibration_leaves_no_job_behind(self) -> None:
        upload = FakeUpload(content_length=3 * 1024 * 1024)
        with self.assertRaises(HTTPException) as raised:
            await self._start(upload)
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("too large", str(raised.exception.detail))
        self.assertEqual(autosub_deps._AUTO_SUB_JOBS, {})

    async def test_wrong_content_type_leaves_no_job_behind(self) -> None:
        upload = FakeUpload(data=b"junk")
        upload.content_type = "application/pdf"
        with self.assertRaises(HTTPException) as raised:
            await self._start(upload)
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("must be a text file", str(raised.exception.detail))
        self.assertEqual(autosub_deps._AUTO_SUB_JOBS, {})

    async def test_valid_calibration_still_registers_and_dispatches(self) -> None:
        started: list = []

        def fake_start_worker(coro):
            started.append(coro)
            # The real _start_auto_sub_worker schedules the coroutine as a
            # task; here the runner never executes, so close the coroutine to
            # release its frames without asyncio's un-awaited warning.
            coro.close()

        with patch.object(autosub_start, "_start_auto_sub_worker", side_effect=fake_start_worker):
            response = await self._start(FakeUpload(data=b"50.0,-2.0\n1000.0,-1.0\n"))
        self.assertEqual(response["status"], "ok")
        self.assertEqual(len(started), 1)
        job_id = response["job"]["id"]
        self.assertIn(job_id, autosub_deps._AUTO_SUB_JOBS)
        self.assertEqual(autosub_deps._AUTO_SUB_JOBS[job_id]["status"], "preparing")
        # The worker is responsible for releasing the shared lock; since the
        # test never runs it, drop the job and free the lock now.
        autosub_deps._AUTO_SUB_JOBS.pop(job_id, None)
        try:
            autosub_start._auto_sub_lock.release()
        except RuntimeError:
            pass


if __name__ == "__main__":
    unittest.main()
