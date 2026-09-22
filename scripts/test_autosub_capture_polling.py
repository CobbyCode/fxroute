#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Capture start/polling behavior: timeout, vanished child, cancel, success.

Pins the exact contract the start+polling extraction must preserve:

* a sweep that never reaches a terminal state is polled exactly 300 times
  at 0.2 s, then cancelled with a 0.5 s settle, and continues through the
  existing drain path (failed/timeout, drain awaited once),
* a child that vanishes between drain and the final fetch reports
  cancelled/"Sweep job disappeared" while keeping its sweep id,
* a cancel observed during polling cancels the child and still drains and
  restores exact mute through the outer cleanup,
* a successful capture completes with decoded points and clears
  ``current_sweep_id``.
"""

from __future__ import annotations

import asyncio
import copy
import sys
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import measurement.autosub.measurement as funnel  # noqa: E402
from measurement.autosub import deps as autosub_deps  # noqa: E402


def _prediction() -> dict:
    return {
        "linear": {"output_1": 0.5, "output_2": 0.4, "output_3": 0.3, "output_4": 0.2},
        "dbfs": {"output_1": -6.0, "output_2": -8.0, "output_3": -10.0, "output_4": -14.0},
        "maximum_dbfs": -6.0,
        "safe": True,
    }


class FakeRuntime:
    """Recording exact-mute runtime with peaks matching the muted prediction."""

    def __init__(self, measured: dict):
        self.measured = dict(measured)
        self.muted = False
        self.mute_calls: list[tuple[bool, int | None]] = []

    def snapshot(self):
        return {"active": True, "output_gain_db": 0.0, "exact_sub_mute": self.muted}

    async def reset_output_peaks(self):
        return None

    async def read_output_peaks(self):
        return dict(self.measured)

    async def set_exact_sub_mute(self, enabled, *, mask=None):
        previous = self.muted
        self.mute_calls.append((bool(enabled), mask))
        self.muted = bool(enabled)
        return previous


class FakeStore:
    """Child store with a scripted get_job sequence per test."""

    def __init__(self):
        self.start_calls: list[dict] = []
        self.get_job = Mock(side_effect=lambda _job_id: {"status": "running"})
        self.drain_job = AsyncMock()
        self.cancel_job = Mock(return_value=None)

    async def start_measurement(self, **kwargs):
        self.start_calls.append(copy.deepcopy(kwargs))
        return {"id": "sweep-1"}


class CapturePollingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Mask 6 zeroes output_2/output_3 in the prediction; the meter must
        # agree so the post-poll peak comparison stays silent.
        self.measured = {"output_1": 0.5, "output_2": 0.0, "output_3": 0.0, "output_4": 0.2}
        self.runtime = FakeRuntime(self.measured)
        self.store = FakeStore()
        self.job = {
            "id": "poll-job", "cancel_requested": False,
            "output_state_context": {
                "mode": "stereo", "revision": 0, "output_key": "dev",
                "channels": 4, "optimizer_path": "single-sub",
                "sub_role_map": {"sub1": "sub1"}, "sub_mute_mask": 4,
            },
            "current_sweep_id": "",
        }
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)

        def _canned_prediction(**_kwargs):
            return copy.deepcopy(_prediction())

        self.stack.enter_context(patch.object(
            funnel, "_dsp_runtime", lambda: self.runtime))
        self.stack.enter_context(patch.object(
            funnel, "_measurement_store", lambda: self.store))
        self.stack.enter_context(patch.object(
            funnel, "_stage_auto_sub_service_candidate",
            AsyncMock(return_value={
                "expected_native_layout": [],
                "fingerprint": "fp-test",
                "expected_native_output_mode": "subwoofer-2.1",
            })))
        self.stack.enter_context(patch.object(
            funnel, "get_output_volume_unclamped", lambda: 100))
        self.stack.enter_context(patch.object(
            funnel, "_predict_auto_sub_stage_peaks",
            AsyncMock(side_effect=_canned_prediction)))
        self.sleep = AsyncMock()
        self.stack.enter_context(patch.object(asyncio, "sleep", self.sleep))
        owner = SimpleNamespace(ensure_ready=AsyncMock())
        autosub_deps.register_candidate_owner(self.job["id"], owner)
        self.addCleanup(autosub_deps.drop_candidate_owner, self.job["id"])

    async def measure(self):
        return await funnel._measure_auto_sub_candidate(
            delay_ms=2.0, job=self.job, candidate_index=1, total=2,
            stage="coarse", fc=80, input_id="mic", channel="left",
            mic_input_channel="1", reference_input_channel="", calibration_ref="",
            calibration_filename=None, calibration_bytes=None,
            auto_sub_sweep_profile={"sweep_seconds": 0.1, "sweep_start_hz": 20, "sweep_end_hz": 200},
            auto_sub_rate=48000, original_level=-3.0, original_polarity="normal",
            original_highpass=True, exact_sub_mute=True, exact_sub_mute_mask=6,
        )

    def sleep_args(self):
        return [call.args[0] for call in self.sleep.await_args_list]

    async def test_poll_timeout_cancels_settles_and_fails_through_drain(self):
        calls = {"count": 0}

        def _get_job(_job_id):
            calls["count"] += 1
            if calls["count"] <= 300:
                return {"status": "running"}
            return {"status": "cancelled"}

        self.store.get_job = Mock(side_effect=_get_job)
        result = await self.measure()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "timeout")
        self.assertEqual(result["sweep_id"], "sweep-1")
        # Exactly 300 polls at 0.2 s plus the final fetch: 1 config settle
        # (0.5 s), 300 poll sleeps (0.2 s), 1 timeout settle (0.5 s).
        self.assertEqual(self.store.get_job.call_count, 301)
        self.assertEqual(
            self.sleep_args(), [0.5] + [0.2] * 300 + [0.5])
        self.store.cancel_job.assert_called_once_with("sweep-1")
        self.store.drain_job.assert_awaited_once_with("sweep-1")
        self.assertEqual(self.job.get("current_sweep_id"), "")
        self.assertIn("predicted", result["stage_output_peaks"])
        self.assertEqual(self.runtime.mute_calls, [(True, 6), (False, 6)])
        self.assertFalse(self.runtime.muted)

    async def test_vanished_child_reports_cancelled_with_sweep_id(self):
        calls = {"count": 0}

        def _get_job(_job_id):
            calls["count"] += 1
            if calls["count"] == 1:
                return {"status": "completed"}
            raise KeyError("sweep-1")

        self.store.get_job = Mock(side_effect=_get_job)
        result = await self.measure()
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["error"], "Sweep job disappeared")
        self.assertEqual(result["sweep_id"], "sweep-1")
        self.assertEqual(self.job.get("current_sweep_id"), "")
        # The regular drain still ran before the final fetch observed the loss.
        self.store.drain_job.assert_awaited_once_with("sweep-1")
        self.store.cancel_job.assert_not_called()
        self.assertEqual(self.runtime.mute_calls, [(True, 6), (False, 6)])
        self.assertFalse(self.runtime.muted)

    async def test_cancel_during_poll_cancels_drains_and_restores(self):
        def _get_job(_job_id):
            self.job["cancel_requested"] = True
            return {"status": "running"}

        self.store.get_job = Mock(side_effect=_get_job)
        result = await self.measure()
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["error"], "Auto Sub Optimize cancelled")
        self.assertEqual(result["sweep_id"], "")
        self.store.cancel_job.assert_called_once_with("sweep-1")
        self.store.drain_job.assert_awaited_once_with("sweep-1")
        self.assertEqual(self.job.get("current_sweep_id"), "")
        self.assertEqual(self.runtime.mute_calls, [(True, 6), (False, 6)])
        self.assertFalse(self.runtime.muted)

    async def test_successful_capture_completes_with_decoded_points(self):
        completed = {
            "status": "completed",
            "result": {"measurement": {
                "channel": "left",
                "traces": [{"kind": "sweep-response", "points": [[20, -1], [80, 1]]}],
                "analysis": {
                    "normalized_by_db": -20, "sample_rate": 48000,
                    "alignment_samples": 1000,
                },
            }},
        }
        self.store.get_job = Mock(return_value=completed)
        result = await self.measure()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["points"], [[20, -1], [80, 1]])
        self.assertEqual(result["calibrated_points"], [[20, -21.0], [80, -19.0]])
        self.assertEqual(result["normalized_by_db"], -20)
        self.assertEqual(result["measurement_channel"], "left")
        self.assertEqual(result["sweep_id"], "sweep-1")
        self.assertTrue(result["exact_sub_mute"])
        self.assertEqual(self.job.get("current_sweep_id"), "")
        self.store.cancel_job.assert_not_called()
        self.store.drain_job.assert_awaited_once_with("sweep-1")
        self.assertEqual(self.runtime.mute_calls, [(True, 6), (False, 6)])
        self.assertFalse(self.runtime.muted)

    async def test_start_failure_reports_error_and_restores_mute(self):
        async def _boom(**_kwargs):
            raise RuntimeError("synthetic capture error")

        self.store.start_measurement = _boom
        result = await self.measure()
        self.assertEqual(result["status"], "error")
        self.assertIn("synthetic capture error", result["error"])
        self.assertEqual(result["sweep_id"], "")
        self.assertEqual(self.job.get("current_sweep_id"), "")
        self.store.drain_job.assert_not_awaited()
        self.assertEqual(self.runtime.mute_calls, [(True, 6), (False, 6)])
        self.assertFalse(self.runtime.muted)

    async def test_task_cancel_during_poll_drains_restores_and_reraises(self):
        started = asyncio.Event()
        release = asyncio.Event()
        calls = {"count": 0}

        async def _blocking_sleep(seconds):
            calls["count"] += 1
            if calls["count"] == 1:
                return None
            started.set()
            await release.wait()

        with patch.object(asyncio, "sleep", _blocking_sleep):
            task = asyncio.create_task(self.measure())
            try:
                await asyncio.wait_for(started.wait(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        self.store.drain_job.assert_awaited_once_with("sweep-1")
        self.assertEqual(self.runtime.mute_calls, [(True, 6), (False, 6)])
        self.assertFalse(self.runtime.muted)


if __name__ == "__main__":
    unittest.main(verbosity=2)
