#!/usr/bin/env python3
"""Focused shared Measurement job setup checks."""

import tempfile
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from measurement import MeasurementStore


class MeasurementJobSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_setup_normalizes_reference_channel_and_common_fields(self):
        with tempfile.TemporaryDirectory() as tempdir:
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

            setup = await store._prepare_measurement_job_setup(
                input_id="mic",
                input_key="",
                mic_input_channel="1",
                reference_input_channel="1",
                calibration_filename=None,
                calibration_bytes=None,
                calibration_ref=None,
                measurement_scope="active-chain",
                job_prefix="measurement-job-",
            )

            self.assertEqual(setup["job"]["id"].split("-")[:2], ["measurement", "job"])
            self.assertEqual(setup["job"]["status"], "queued")
            self.assertEqual(setup["job"]["input"]["id"], "mic")
            self.assertEqual(setup["job"]["input_channels"]["mic"], 1)
            self.assertIsNone(setup["job"]["input_channels"]["electrical_reference"])
            self.assertIn("same channel", setup["job"]["input_channels"]["reference_disabled_reason"])
            self.assertEqual(setup["job"]["measurement_scope"], "active_chain")


if __name__ == "__main__":
    unittest.main()
