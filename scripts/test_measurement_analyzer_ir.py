#!/usr/bin/env python3
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from measurement.analyzer import MeasurementAnalyzer
from measurement.signal import generate_log_sweep
from measurement.store import MeasurementStore


class MeasurementAnalyzerIrTests(unittest.TestCase):
    def test_direct_arrival_promotes_stronger_candidate_by_energy(self):
        impulse = np.zeros(6000, dtype=np.float64)
        impulse[4300] = 0.06
        impulse[4495:4506] = [0.055, 0.057, 0.059, 0.060, 0.061, 0.062, 0.061, 0.060, 0.059, 0.057, 0.055]
        impulse[5000] = 1.0
        reference = np.zeros_like(impulse)
        reference[5000] = 1.0

        analyzer = MeasurementAnalyzer(None, None)
        result = analyzer._estimate_impulse_direct_arrival(impulse, reference, 48_000)

        self.assertEqual(result["direct_arrival_index"], 4500)
        self.assertEqual(result["selection_rule"], "skipped_weak_threshold_edge_for_stronger_impulse_region")
        self.assertTrue(result["promotion_applied"])

    def test_arrival_before_electrical_reference_is_ambiguous(self):
        impulse = np.zeros(6000, dtype=np.float64)
        reference = np.zeros_like(impulse)
        impulse[4593] = 1.0
        reference[5000] = 1.0

        result = MeasurementAnalyzer(None, None)._estimate_impulse_direct_arrival(
            impulse, reference, 48_000
        )

        self.assertEqual(result["relative_samples"], -407)
        self.assertFalse(result["timing_valid"])
        self.assertEqual(result["timing_status"], "ambiguous")
        self.assertEqual(result["confidence"], 0.0)

    def test_negative_arrival_does_not_leave_analyzer_as_timing(self):
        rate = 48_000
        sweep = generate_log_sweep(rate, 0.68, 10.0, 22_000.0, peak_scale=0.8)
        lead = np.zeros(int(rate * 0.34), dtype=np.float32)
        tail = np.zeros(int(rate * 0.18), dtype=np.float32)
        reference = np.concatenate([lead, sweep, tail]).astype(np.float32)
        mic = np.zeros_like(reference)
        mic[:-407] = reference[407:]
        with tempfile.TemporaryDirectory() as home:
            store = MeasurementStore(home=Path(home))
            path = store.captures_dir / "negative-arrival.wav"
            store._write_wav(path, np.column_stack([mic, reference]), rate)
            analysis = store._analyzer._analyze_sweep_capture(
                path, expected_sample_rate=rate, channel="left",
                reference_sweep=sweep, inverse_sweep=np.array([1.0]),
                calibration_curve=None, capture_label="Negative arrival",
                reference_channel_index=1, analysis_channel_index=0,
                reference_channel_label="reference",
            )

        impulse_meta = analysis["impulse_response"]
        self.assertFalse(analysis["direct_arrival_timing_available"])
        self.assertEqual(impulse_meta["timing_status"], "ambiguous")
        self.assertLess(impulse_meta["direct_arrival_index"], impulse_meta["reference_peak_index"])
        self.assertEqual(impulse_meta["direct_confidence"], 0.0)
        for key in ("arrival_ms", "arrival_seconds", "arrival_samples"):
            self.assertNotIn(key, impulse_meta)
        analysis["reference_path"].update(peak_dbfs=-12.0, ir_sharpness_db=40.0)
        analysis["clock"].update(start_score=0.99, end_score=0.99)
        verdict = store._evaluate_electrical_reference_status(analysis)
        self.assertFalse(verdict["usable"])
        self.assertFalse(analysis["reference_path"].get("electrical_reference_used", False))


if __name__ == "__main__":
    unittest.main()
