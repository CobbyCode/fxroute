#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Role-derived exact sub mute: arbitrary masks instead of fixed outputs 3/4."""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from measurement.autosub.jobs import _auto_sub_zero_sub_peaks
from dsp.runtime import DSPRuntime, BassManagementConfig
import measurement.autosub.measurement as candidate_measurement
import measurement.session as measurement_session
from measurement.autosub.roles import sub_mute_indices


def _prediction() -> dict:
    return {
        "linear": {"output_1": 0.5, "output_2": 0.4, "output_3": 0.3, "output_4": 0.2},
        "dbfs": {"output_1": -6.0, "output_2": -8.0, "output_3": -10.0, "output_4": -14.0},
        "maximum_dbfs": -6.0,
        "safe": True,
    }


class ZeroSubPeaksTests(unittest.TestCase):
    def test_legacy_outputs_three_and_four(self):
        result = _auto_sub_zero_sub_peaks(_prediction(), (2, 3))
        self.assertEqual(result["linear"]["output_3"], 0.0)
        self.assertEqual(result["linear"]["output_4"], 0.0)
        self.assertEqual(result["linear"]["output_1"], 0.5)
        self.assertEqual(result["maximum_dbfs"], -6.0)
        self.assertTrue(result["safe"])

    def test_arbitrary_role_indices(self):
        """A supplied extended prediction can be folded; production is still four outputs."""
        prediction = _prediction()
        prediction["linear"]["output_5"] = 0.9
        prediction["dbfs"]["output_5"] = -1.0
        prediction["maximum_dbfs"] = -1.0
        result = _auto_sub_zero_sub_peaks(prediction, (4,))
        self.assertEqual(result["linear"]["output_5"], 0.0)
        self.assertEqual(result["maximum_dbfs"], -6.0)
        self.assertTrue(result["safe"])

    def test_missing_output_fails_closed_without_inventing_a_peak(self):
        for field in ("linear", "dbfs"):
            prediction = _prediction()
            del prediction[field]["output_4"]
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "prediction"):
                _auto_sub_zero_sub_peaks(prediction, (3,))
        with self.assertRaisesRegex(ValueError, "prediction"):
            _auto_sub_zero_sub_peaks(_prediction(), (4,))

    def test_invalid_indices_are_rejected(self):
        for index in (-1, True, 1.5, "2"):
            with self.subTest(index=index), self.assertRaises(ValueError):
                _auto_sub_zero_sub_peaks(_prediction(), (index,))

    def test_mask_indices_require_nonzero_integer(self):
        self.assertEqual(sub_mute_indices(10), (1, 3))
        for mask in (0, -1, True, 1.5, "12", 1 << 32):
            with self.subTest(mask=mask), self.assertRaises(ValueError):
                sub_mute_indices(mask)

    def test_unsafe_stays_unsafe_after_zeroing(self):
        prediction = _prediction()
        prediction["linear"]["output_1"] = 1.2
        prediction["dbfs"]["output_1"] = 2.0
        prediction["maximum_dbfs"] = 2.0
        prediction["safe"] = False
        result = _auto_sub_zero_sub_peaks(prediction, (2,))
        self.assertFalse(result["safe"])
        self.assertEqual(result["maximum_dbfs"], 2.0)

    def test_does_not_mutate_the_prediction(self):
        prediction = _prediction()
        _auto_sub_zero_sub_peaks(prediction, (2, 3))
        self.assertEqual(prediction["linear"]["output_3"], 0.3)


class ExactSubMuteMaskTests(unittest.IsolatedAsyncioTestCase):
    def _runtime(self):
        runtime = DSPRuntime(object())
        runtime._config = type("Config", (), {
            "layout": ({},) * 8,
            "hardware_ports": ("FL", "FR", "RL", "RR"),
            "sample_rate": 48000,
            "output_mode": "crossover",
            "output_key": "mock",
        })()
        runtime._control = AsyncMock(return_value="ok")
        return runtime

    async def test_mask_control_command(self):
        runtime = self._runtime()
        self.assertFalse(await runtime.set_exact_sub_mute(True, mask=0b10000))
        self.assertTrue(runtime.snapshot()["exact_sub_mute"])
        self.assertEqual(runtime._control.await_args.args, ("mute 16 1",))
        await runtime.set_exact_sub_mute(False, mask=0b10000)
        self.assertEqual(runtime._control.await_args.args, ("mute 16 0",))
        self.assertFalse(runtime.snapshot()["exact_sub_mute"])

    async def test_returns_previous_state(self):
        runtime = self._runtime()
        self.assertFalse(await runtime.set_exact_sub_mute(True, mask=0b11000))
        self.assertTrue(await runtime.set_exact_sub_mute(True, mask=0b11000))

    async def test_rejects_zero_and_oversized_masks(self):
        runtime = self._runtime()
        for mask in (0, -1, 1 << 32, 1.5, "16"):
            with self.assertRaises(ValueError):
                await runtime.set_exact_sub_mute(True, mask=mask)

    async def test_rejects_mask_beyond_engine_outputs(self):
        runtime = self._runtime()
        runtime._config.layout = ({},) * 4
        with self.assertRaises(ValueError):
            await runtime.set_exact_sub_mute(True, mask=1 << 4)


    async def test_overlapping_output_mask_survives_exact_mute_restore(self):
        runtime = self._runtime()
        await runtime.apply_output_mask(6)
        await runtime.set_exact_sub_mute(True, mask=12)
        await runtime.set_exact_sub_mute(False, mask=12)
        self.assertEqual(runtime._control.await_args.args, ("mute 8 0",))
        self.assertEqual(runtime.snapshot()["output_mask"], 6)
        self.assertFalse(runtime.snapshot()["exact_sub_mute"])

    async def test_clearing_output_mask_preserves_active_exact_mute(self):
        runtime = self._runtime()
        await runtime.set_exact_sub_mute(True, mask=12)
        await runtime.apply_output_mask(6)
        await runtime.clear_output_mask(6)
        self.assertEqual(runtime._control.await_args.args, ("mute 2 0",))
        self.assertTrue(runtime.snapshot()["exact_sub_mute"])
        self.assertEqual(runtime.snapshot()["output_mask"], 0)

    async def test_active_exact_mask_cannot_be_silently_replaced(self):
        runtime = self._runtime()
        await runtime.set_exact_sub_mute(True, mask=12)
        for enabled in (True, False):
            with self.assertRaisesRegex(RuntimeError, "mask"):
                await runtime.set_exact_sub_mute(enabled, mask=6)
        self.assertEqual(runtime._control.await_count, 1)
        self.assertTrue(runtime.snapshot()["exact_sub_mute"])

    async def test_control_failure_does_not_change_confirmed_state(self):
        runtime = self._runtime()
        runtime._control.side_effect = RuntimeError("no acknowledgement")
        with self.assertRaises(RuntimeError):
            await runtime.set_exact_sub_mute(True, mask=6)
        self.assertFalse(runtime.snapshot()["exact_sub_mute"])
        runtime._control.side_effect = None
        await runtime.set_exact_sub_mute(True, mask=6)
        runtime._control.side_effect = RuntimeError("no acknowledgement")
        with self.assertRaises(RuntimeError):
            await runtime.set_exact_sub_mute(False, mask=6)
        self.assertTrue(runtime.snapshot()["exact_sub_mute"])

    def _gated_runtime(self):
        """Keep the real control lock; pause the fake engine before its ACK."""
        runtime = self._runtime()
        engine = SimpleNamespace(
            mask=0, commands=[], gate_command=None,
            entered=asyncio.Event(), acknowledge=asyncio.Event(),
        )

        async def control(command, *, reply):
            self.assertTrue(reply)
            engine.commands.append(command)
            _, bits, enabled = command.split()
            engine.mask = engine.mask | int(bits) if enabled == "1" else engine.mask & ~int(bits)
            if command == engine.gate_command:
                engine.entered.set()
                await engine.acknowledge.wait()
            return "ok"

        del runtime._control  # Use DSPRuntime._control and its datagram lock.
        runtime._control_unlocked = control
        return runtime, engine

    async def _start_competing_call(self, operation):
        started = asyncio.Event()

        async def run():
            started.set()
            # No yield between signalling and entering the runtime method:
            # the test resumes only once the operation blocks (or finishes).
            return await operation

        task = asyncio.create_task(run())
        await asyncio.wait_for(started.wait(), 1)
        return task

    async def test_concurrent_clear_output_mask_cannot_unmute_active_exact_mute(self):
        """An exact-mute ACK must precede the overlapping clear decision."""
        runtime, engine = self._gated_runtime()
        await runtime.apply_output_mask(6)
        engine.commands.clear()
        engine.gate_command = "mute 12 1"
        enable = asyncio.create_task(runtime.set_exact_sub_mute(True, mask=12))
        try:
            await asyncio.wait_for(engine.entered.wait(), 1)
            clear = await self._start_competing_call(runtime.clear_output_mask(6))
        finally:
            engine.acknowledge.set()
            await enable
        await asyncio.wait_for(clear, 1)
        self.assertEqual(engine.commands, ["mute 12 1", "mute 2 0"])
        self.assertEqual(engine.mask, 12)
        self.assertTrue(runtime.snapshot()["exact_sub_mute"])
        self.assertEqual(runtime._exact_sub_mute_mask, 12)
        self.assertEqual(runtime.snapshot()["output_mask"], 0)

    async def test_concurrent_exact_mute_release_does_not_clear_mask_owned_bits(self):
        """An output-mask ACK must precede the overlapping exact restore decision."""
        runtime, engine = self._gated_runtime()
        await runtime.set_exact_sub_mute(True, mask=12)
        engine.commands.clear()
        engine.gate_command = "mute 6 1"
        apply_task = asyncio.create_task(runtime.apply_output_mask(6))
        try:
            await asyncio.wait_for(engine.entered.wait(), 1)
            release = await self._start_competing_call(runtime.set_exact_sub_mute(False, mask=12))
        finally:
            engine.acknowledge.set()
            await apply_task
        self.assertTrue(await asyncio.wait_for(release, 1))
        self.assertEqual(engine.commands, ["mute 6 1", "mute 8 0"])
        self.assertEqual(engine.mask, 6)
        self.assertFalse(runtime.snapshot()["exact_sub_mute"])
        self.assertEqual(runtime._exact_sub_mute_mask, 0)
        self.assertEqual(runtime.snapshot()["output_mask"], 6)

    async def test_cancelled_mask_waiter_leaves_ownership_usable(self):
        """Cancellation before ownership acquisition sends no command."""
        runtime, engine = self._gated_runtime()
        engine.gate_command = "mute 12 1"
        enable = asyncio.create_task(runtime.set_exact_sub_mute(True, mask=12))
        try:
            await asyncio.wait_for(engine.entered.wait(), 1)
            cancelled = await self._start_competing_call(runtime.clear_output_mask(12))
            cancelled.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await cancelled
        finally:
            engine.acknowledge.set()
            await enable
        await asyncio.wait_for(runtime.set_exact_sub_mute(False, mask=12), 1)
        self.assertEqual(engine.commands, ["mute 12 1", "mute 12 0"])
        self.assertEqual(engine.mask, 0)
        self.assertFalse(runtime.snapshot()["exact_sub_mute"])

    async def test_explicit_mask_checks_legacy_engine_bounds(self):
        runtime = self._runtime()
        runtime._config.layout = ()
        with self.assertRaises(ValueError):
            await runtime.set_exact_sub_mute(True, mask=16)
        runtime._control.assert_not_awaited()


class CandidateRoleMuteTests(unittest.IsolatedAsyncioTestCase):
    """Run the real candidate, peak folding/comparison and runtime mute API.

    Only hardware control, persisted config, pre-arm and capture IO are faked.
    The capture records the native mute bits, so restoring a wrong mask or
    dropping the explicit mask on remeasure is observable, not just a spy call.
    """

    def setUp(self):
        self.job = {"cancel_requested": False}
        self.native_mask = 0
        self.captured_masks = []
        self.outcome = "success"
        self.fail_enable = False
        self.fail_restore = False
        self.runtime = DSPRuntime(object())
        self.runtime._config = SimpleNamespace(
            hardware_ports=("FL", "FR", "RL", "RR"), layout=({},) * 4,
            sample_rate=48000, output_mode="subwoofer-2.1", output_key="mock",
        )
        self.runtime._control = AsyncMock(side_effect=self.control)
        self.runtime.read_output_peaks = AsyncMock(side_effect=self.read_peaks)
        self.store = SimpleNamespace(
            start_measurement=AsyncMock(side_effect=self.capture),
            get_job=lambda _id: {
                "status": "completed", "result": {"measurement": {
                    "channel": "left",
                    "traces": [{"kind": "sweep-response", "points": [[20, -1], [80, 1]]}],
                    "analysis": {"normalized_by_db": -20, "sample_rate": 48000},
                }},
            },
            cancel_job=lambda _id: None,
        )
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for target, name, replacement in (
            (candidate_measurement, "_dsp_runtime", lambda: self.runtime),
            (candidate_measurement, "_measurement_store", lambda: self.store),
            (candidate_measurement, "set_audio_output_mode", lambda *args: {}),
            (candidate_measurement, "get_audio_output_overview", lambda: {}),
            (candidate_measurement, "get_output_volume_unclamped", lambda: 100),
            (candidate_measurement, "_auto_sub_sync_dsp_runtime", AsyncMock()),
            (candidate_measurement, "_predict_auto_sub_stage_peaks", AsyncMock(side_effect=lambda **kw: _prediction())),
            (measurement_session, "_sync_dsp_runtime_for_measurement_sweep", AsyncMock()),
            (asyncio, "sleep", AsyncMock()),
        ):
            self.stack.enter_context(patch.object(target, name, replacement))
        self.stack.enter_context(patch.object(BassManagementConfig, "from_overview", return_value=object()))
        self.stack.enter_context(patch("audio.samplerate._load_audio_output_mode", return_value={"subwoofer": {"sub_alignment_ms": 2.0}}))

    async def control(self, command, **kwargs):
        if command.startswith("mute "):
            _, bits, enabled = command.split()
            if (enabled == "1" and self.fail_enable) or (enabled == "0" and self.fail_restore):
                raise RuntimeError("control acknowledgement missing")
            self.native_mask = self.native_mask | int(bits) if enabled == "1" else self.native_mask & ~int(bits)
        return "ok"

    async def read_peaks(self):
        return {key: 0.0 if self.native_mask & (1 << index) else value
                for index, (key, value) in enumerate(_prediction()["linear"].items())}

    async def capture(self, **kwargs):
        self.captured_masks.append(self.native_mask)
        if self.outcome == "error":
            raise RuntimeError("synthetic capture error")
        if self.outcome == "cancel":
            self.job["cancel_requested"] = True
        if self.outcome == "task_cancel":
            raise asyncio.CancelledError()
        return {"id": "reference"}

    async def measure(self, **overrides):
        kwargs = dict(
            delay_ms=2.0, job=self.job, candidate_index=1, total=2,
            stage="main_reference", fc=80, input_id="mic", channel="left",
            mic_input_channel="1", reference_input_channel="", calibration_ref="",
            calibration_filename=None, calibration_bytes=None,
            auto_sub_sweep_profile={"sweep_seconds": .1, "sweep_start_hz": 20, "sweep_end_hz": 200},
            auto_sub_rate=48000, original_level=-3.0, original_polarity="normal",
            original_highpass=True, exact_sub_mute=True, exact_sub_mute_mask=6,
        )
        kwargs.update(overrides)
        return await candidate_measurement._measure_auto_sub_candidate(**kwargs)

    async def test_explicit_mask_success(self):
        result = await self.measure()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.captured_masks, [6])
        self.assertEqual(self.native_mask, 0)
        self.assertFalse(self.runtime.snapshot()["exact_sub_mute"])
        self.assertEqual(result["stage_output_peaks"]["predicted"]["linear"], {
            "output_1": .5, "output_2": 0.0, "output_3": 0.0, "output_4": .2,
        })

    async def test_explicit_mask_capture_error_restores(self):
        self.outcome = "error"
        self.assertEqual((await self.measure())["status"], "error")
        self.assertEqual(self.captured_masks, [6])
        self.assertEqual(self.native_mask, 0)

    async def test_explicit_mask_cancel_restores(self):
        self.outcome = "cancel"
        self.assertEqual((await self.measure())["status"], "cancelled")
        self.assertEqual(self.captured_masks, [6])
        self.assertEqual(self.native_mask, 0)

    async def test_task_cancellation_restores(self):
        self.outcome = "task_cancel"
        with self.assertRaises(asyncio.CancelledError):
            await self.measure()
        self.assertEqual(self.captured_masks, [6])
        self.assertEqual(self.native_mask, 0)

    async def test_previous_exact_mute_preserved_legacy_and_explicit(self):
        for mask in (None, 6):
            for outcome in ("success", "error", "cancel"):
                with self.subTest(mask=mask, outcome=outcome):
                    self.job.clear()
                    self.outcome = outcome
                    options = {} if mask is None else {"mask": mask}
                    await self.runtime.set_exact_sub_mute(True, **options)
                    result = await self.measure(exact_sub_mute_mask=mask)
                    self.assertEqual(result["status"], {"success": "completed", "error": "error", "cancel": "cancelled"}[outcome])
                    self.assertEqual(self.native_mask, 12 if mask is None else mask)
                    self.assertTrue(self.runtime.snapshot()["exact_sub_mute"])
                    await self.runtime.set_exact_sub_mute(False, **options)

    async def test_preexisting_overlapping_output_mask_preserved(self):
        await self.runtime.apply_output_mask(4)
        self.assertEqual((await self.measure())["status"], "completed")
        self.assertEqual(self.captured_masks, [6])
        self.assertEqual(self.native_mask, 4)
        self.assertEqual(self.runtime.snapshot()["output_mask"], 4)

    async def test_remeasure_keeps_explicit_mask_and_restores_outer_state(self):
        with patch.object(candidate_measurement, "_auto_sub_chain_health_check", side_effect=[{"confirmed": False}, None]):
            result = await self.measure()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.captured_masks, [6, 6])
        self.assertEqual(self.native_mask, 0)

    async def test_invalid_or_unpredicted_mask_never_starts_capture(self):
        for mask in (0, -1, True, 1.5, "6", 1 << 32, 16):
            with self.subTest(mask=mask), self.assertRaises(ValueError):
                await self.measure(exact_sub_mute_mask=mask)
        self.store.start_measurement.assert_not_awaited()
        self.runtime._control.assert_not_awaited()

    async def test_mask_without_enabled_flag_is_rejected(self):
        with self.assertRaises(ValueError):
            await self.measure(exact_sub_mute=False)
        self.store.start_measurement.assert_not_awaited()

    async def test_enable_control_failure_blocks_capture(self):
        self.fail_enable = True
        result = await self.measure()
        self.assertEqual(result["status"], "error")
        self.assertIn("acknowledgement", result["error"])
        self.store.start_measurement.assert_not_awaited()
        self.assertEqual(self.native_mask, 0)
        self.assertFalse(self.runtime.snapshot()["exact_sub_mute"])

    async def test_restore_control_failure_aborts(self):
        self.fail_restore = True
        with self.assertRaisesRegex(RuntimeError, "restoration was not acknowledged"):
            await self.measure()
        self.assertIn("restore failed", self.job["auto_gain"]["reason"])
        self.assertEqual(self.native_mask, 6)
        self.assertTrue(self.runtime.snapshot()["exact_sub_mute"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
