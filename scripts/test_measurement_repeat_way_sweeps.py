#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""L/R repeat runs its internal way sweeps through the frozen measurement area.

Each of the repeat's left/right sweeps is one internal way sweep of the same
frozen target: it applies that side's output mask for its duration, clears it
again afterwards (also when the sweep fails), and never touches masks without
a frozen target.  Without a target provider the repeat job keeps the legacy
job shape byte for byte.
"""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from measurement.store import MeasurementStore

ROLES = ["left_low", "left_high", "right_low", "right_high", "sub1"]


def frozen_target(bank_id, measured_roles):
    """A frozen target document shaped like ``freeze_measurement_target``."""
    return {
        "schema": "fxroute.measurement-target",
        "version": 1,
        "mode": "crossover",
        "device_key": "test-device",
        "bank_id": bank_id,
        "preset": "Neutral",
        "revision": 3,
        "processing_fingerprint": "fingerprint-test",
        "sample_rate_hz": 48000,
        "channels": len(ROLES),
        "roles": list(ROLES),
        "measured_roles": list(measured_roles),
        "reference_tap": "fxroute_dsp_sink.monitor",
    }


class RepeatWaySweepTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="fxroute-repeat-way-sweeps-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self._environ = {name: os.environ.get(name) for name in ("XDG_CONFIG_HOME", "XDG_STATE_HOME")}
        self.addCleanup(self._restore_environ)
        os.environ["XDG_CONFIG_HOME"] = str(self.root / "config")
        os.environ["XDG_STATE_HOME"] = str(self.root / "state")
        self.store = MeasurementStore(home=self.root)
        self.masked: list[tuple[str, int]] = []
        self.store.output_mask_apply_sync = lambda mask: self.masked.append(("apply", mask))
        self.store.output_mask_clear_sync = lambda mask: self.masked.append(("clear", mask))
        self.targets: dict[str, dict] = {}

        def provider(bank_id, sample_rate_hz):
            if bank_id not in self.targets:
                raise ValueError(f"Bank {bank_id!r} is not an active role on the selected outputs")
            return self.targets[bank_id]

        self.store.measurement_target_provider = provider

    def _restore_environ(self):
        for name, value in self._environ.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _stub_start(self):
        self.store._discover_capture_inputs = lambda: [{
            "id": "test-input",
            "label": "Test input",
            "available": True,
            "channels": 2,
        }]

        async def no_capture(_job_id):
            return None

        self.store._run_measurement_job = no_capture

    def test_repeat_job_freezes_area_and_its_per_sweep_masks(self):
        self.targets["left_low"] = frozen_target("left_low", ["left_low"])
        self._stub_start()

        async def start():
            return await self.store.start_lr_repeat_measurement(
                input_id="test-input", base_name="Sofa center", measurement_bank="left_low")

        job = asyncio.run(start())
        self.assertEqual(job["measurement_bank"], "left_low")
        self.assertEqual(job["measurement_target"]["bank_id"], "left_low")
        # One side of this area: both internal sweeps keep the area audible.
        area_mask = sum(1 << index for index, role in enumerate(ROLES) if role != "left_low")
        self.assertEqual(area_mask, 0b11110)
        self.assertEqual(job["sweep_output_masks"], {"left": area_mask, "right": area_mask})
        self.assertEqual(job["output_mask"], area_mask)

    def test_global_repeat_splits_masks_per_internal_sweep(self):
        self.targets["global"] = frozen_target("global", ROLES)
        self._stub_start()

        async def start():
            return await self.store.start_lr_repeat_measurement(
                input_id="test-input", measurement_bank="global")

        job = asyncio.run(start())
        self.assertEqual(job["output_mask"], 0)
        self.assertEqual(job["sweep_output_masks"], {
            # Left sweep keeps the left ways plus the mono sub.
            "left": 0b01100,
            "right": 0b00011,
        })

    def test_unknown_area_fails_before_any_sweep(self):
        self._stub_start()

        async def start():
            return await self.store.start_lr_repeat_measurement(
                input_id="test-input", measurement_bank="left_mid")

        with self.assertRaisesRegex(ValueError, "not an active role"):
            asyncio.run(start())
        self.assertEqual(self.masked, [])

    def test_internal_sweep_applies_and_clears_its_mask(self):
        job = {"sweep_output_masks": {"left": 0b01100, "right": 0b00011}}
        self.assertEqual(
            self.store._repeat_runner._run_internal_way_sweep(job, "left", lambda: "captured"),
            "captured")
        self.assertEqual(self.masked, [("apply", 0b01100), ("clear", 0b01100)])

    def test_failed_sweep_still_clears_its_mask(self):
        job = {"sweep_output_masks": {"left": 0b01100, "right": 0b00011}}

        def failing():
            raise RuntimeError("capture failed")

        with self.assertRaisesRegex(RuntimeError, "capture failed"):
            self.store._repeat_runner._run_internal_way_sweep(job, "right", failing)
        self.assertEqual(self.masked, [("apply", 0b00011), ("clear", 0b00011)])

    def test_repeat_loop_masks_every_sweep_and_stops_on_failure(self):
        job = {"id": "repeat-job", "repeat_count": 1,
               "sweep_output_masks": {"left": 0b01100, "right": 0b00011}}

        def capture(capture_job):
            raise RuntimeError("left sweep failed")

        self.store._execute_capture_job = capture
        with self.assertRaisesRegex(RuntimeError, "left sweep failed"):
            self.store._execute_lr_repeat_job(job)
        # The right sweep never ran, and the left mask was released again.
        self.assertEqual(self.masked, [("apply", 0b01100), ("clear", 0b01100)])

    def test_without_a_frozen_target_nothing_is_masked(self):
        # A legacy store has neither a target provider nor the synchronous
        # mask callables the runtime injects, so its sweeps run unmasked.
        legacy = MeasurementStore(home=self.root)
        legacy._discover_capture_inputs = lambda: [{
            "id": "test-input",
            "label": "Test input",
            "available": True,
            "channels": 2,
        }]

        async def no_capture(_job_id):
            return None

        legacy._run_measurement_job = no_capture

        async def start():
            return await legacy.start_lr_repeat_measurement(input_id="test-input")

        job = asyncio.run(start())
        for key in ("measurement_bank", "measurement_target", "output_mask", "sweep_output_masks"):
            self.assertNotIn(key, job)
        self.assertEqual(
            legacy._repeat_runner._run_internal_way_sweep(
                {"sweep_output_masks": {"left": 0b1}}, "left", lambda: "captured"),
            "captured")
        self.assertEqual(self.masked, [])

    def test_zero_and_missing_masks_are_no_ops(self):
        job = {"sweep_output_masks": {"left": 0}}
        self.assertEqual(
            self.store._repeat_runner._run_internal_way_sweep(job, "left", lambda: "captured"),
            "captured")
        self.assertEqual(
            self.store._repeat_runner._run_internal_way_sweep({"id": "no-masks"}, "left",
                                                              lambda: "captured"),
            "captured")
        self.assertEqual(
            self.store._repeat_runner._run_internal_way_sweep(
                {"sweep_output_masks": ["left"]}, "left", lambda: "captured"),
            "captured")
        self.assertEqual(self.masked, [])


if __name__ == "__main__":
    unittest.main()
