#!/usr/bin/env python3
"""Synthetic direct-window and complex-response checks."""

import unittest
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hybrid_measurement import (
    analyze_direct_window,
    build_complex_response,
    build_gated_response,
    sum_complex_points,
)
from measurement import MeasurementStore


class HybridMeasurementAnalysisTests(unittest.TestCase):
    def _analysis_payload(self):
        return {
            "method": "test",
            "sample_rate": 48_000,
            "rms_dbfs": -30.0,
            "peak_dbfs": -12.0,
            "window_count": 1,
            "normalized_by_db": 0.0,
            "alignment_samples": 0,
            "alignment_seconds": 0.0,
            "trusted_min_hz": 20.0,
            "trusted_max_hz": 20_000.0,
            "raw_point_count": 2,
            "review_point_count": 2,
            "display_point_count": 2,
            "trusted_band_meta": {},
            "review_band_meta": {},
            "quality_checks": {"items": []},
            "capture_audit": {},
            "clock": {},
            "reference_path": {},
            "impulse_response": {},
            "variable_window": {},
            "trusted_points": [[20, 0], [20_000, 0]],
            "review_points": [[20, 0], [20_000, 0]],
            "direct_response": {"status": "ok", "usable": True, "points": [[500, 0]]},
            "complex_response": {"schema": "fxroute.complex-response.v1", "points": [[40, 1, 0]]},
        }

    def test_direct_analysis_survives_final_measurement_payload(self):
        with tempfile.TemporaryDirectory() as home:
            store = MeasurementStore(home=Path(home))
            measurement = store._build_measurement_from_analysis(
                self._analysis_payload(),
                input_device={"id": "mic", "label": "Mic"},
                channel="left",
                calibration={"applied": False},
                measurement_role="direct",
            )

        self.assertTrue(measurement["analysis"]["direct_response"]["usable"])

    def test_direct_complex_response_survives_final_measurement_payload(self):
        with tempfile.TemporaryDirectory() as home:
            store = MeasurementStore(home=Path(home))
            measurement = store._build_measurement_from_analysis(
                self._analysis_payload(),
                input_device={"id": "mic", "label": "Mic"},
                channel="left",
                calibration={"applied": False},
                measurement_role="direct",
            )

        self.assertEqual(measurement["analysis"]["complex_response"]["points"], [[40, 1, 0]])

    def test_classic_role_does_not_request_hybrid_analysis(self):
        self.assertEqual(MeasurementStore._hybrid_analysis_requirements(""), (False, False))
        self.assertEqual(MeasurementStore._hybrid_analysis_requirements("secondary"), (False, False))
        self.assertEqual(MeasurementStore._hybrid_analysis_requirements("mlp"), (False, True))
        self.assertEqual(MeasurementStore._hybrid_analysis_requirements("direct"), (True, True))

    def test_first_reflection_sets_gate_and_frequency_limit(self):
        sample_rate = 48_000
        direct = 1_000
        reflection = direct + 480
        impulse = np.zeros(4_000)
        impulse[direct] = 1.0
        impulse[reflection] = 0.55

        result = analyze_direct_window(impulse, sample_rate, direct)

        self.assertTrue(result["usable"])
        self.assertEqual(result["status"], "ok")
        self.assertLessEqual(result["first_reflection_index"], reflection)
        self.assertGreaterEqual(result["first_reflection_index"], reflection - 10)
        self.assertGreater(result["usable_window_ms"], 9.0)
        self.assertLess(result["usable_window_ms"], 10.0)
        self.assertGreater(result["lower_reliable_hz"], 150)
        frequencies, magnitude = build_gated_response(impulse, sample_rate, result)
        self.assertEqual(frequencies.shape, magnitude.shape)
        self.assertTrue(np.all(np.isfinite(magnitude)))

    def test_window_limit_is_derived_not_fixed(self):
        sample_rate = 48_000
        direct = 500
        short = np.zeros(4_000)
        long = np.zeros(4_000)
        short[direct] = long[direct] = 1.0
        short[direct + 240] = 0.6
        long[direct + 720] = 0.6

        short_result = analyze_direct_window(short, sample_rate, direct)
        long_result = analyze_direct_window(long, sample_rate, direct)

        self.assertGreater(short_result["lower_reliable_hz"], long_result["lower_reliable_hz"])
        self.assertNotEqual(short_result["lower_reliable_hz"], 500)

    def test_missing_reflection_is_rejected_conservatively(self):
        sample_rate = 48_000
        direct = 500
        impulse = np.zeros(4_000)
        impulse[direct] = 1.0

        result = analyze_direct_window(impulse, sample_rate, direct)

        self.assertFalse(result["usable"])
        self.assertEqual(result["status"], "reflection-not-identifiable")
        self.assertEqual(result["usable_window_ms"], 3.0)
        self.assertEqual(result["lower_reliable_hz"], 500.0)
        self.assertIn("No trustworthy reflection-free interval", result["retry_reason"])

    def test_sub_millisecond_reflection_is_not_hidden_in_fallback_gate(self):
        sample_rate = 48_000
        direct = 500
        reflection = direct + 38
        impulse = np.zeros(4_000)
        impulse[direct] = 1.0
        impulse[reflection] = 0.7

        result = analyze_direct_window(impulse, sample_rate, direct)

        self.assertFalse(result["usable"])
        self.assertEqual(result["status"], "reflection-too-early")
        self.assertLessEqual(result["first_reflection_index"], reflection)
        self.assertGreaterEqual(result["first_reflection_index"], reflection - 10)
        self.assertLess(result["gate_end_index"], reflection)

    def test_connected_early_speaker_response_is_not_an_early_reflection(self):
        sample_rate = 48_000
        direct = 500
        impulse = np.zeros(4_000)
        samples = np.arange(90)
        speaker_envelope = np.where(samples < 34, 0.15 + (samples / 34), np.exp(-(samples - 34) / 22))
        impulse[direct:direct + samples.size] = speaker_envelope * np.cos(samples * 0.62)
        reflection = direct + 220
        impulse[reflection] = 0.8

        result = analyze_direct_window(impulse, sample_rate, direct)

        self.assertTrue(result["usable"])
        self.assertEqual(result["status"], "ok")
        self.assertGreater(result["first_reflection_ms"], 4.0)
        self.assertGreater(result["reflection_detection"]["direct_event_duration_ms"], 1.0)

    def test_separate_early_reflection_after_energy_valley_is_detected(self):
        sample_rate = 48_000
        direct = 500
        impulse = np.zeros(4_000)
        impulse[direct] = 1.0
        reflection = direct + 58
        impulse[reflection] = 0.7

        result = analyze_direct_window(impulse, sample_rate, direct)

        self.assertFalse(result["usable"])
        self.assertEqual(result["status"], "reflection-too-early")
        self.assertLess(result["gate_end_index"], reflection)

    def test_early_reflection_is_not_forced_inside_minimum_gate(self):
        sample_rate = 48_000
        direct = 500
        reflection = direct + 48
        impulse = np.zeros(4_000)
        impulse[direct] = 1.0
        impulse[reflection] = 0.7

        result = analyze_direct_window(impulse, sample_rate, direct)

        self.assertFalse(result["usable"])
        self.assertEqual(result["status"], "reflection-too-early")
        self.assertLess(result["gate_end_index"], reflection)

    def test_complex_calibration_uses_measurement_correction_sign(self):
        sample_rate = 48_000
        impulse = np.zeros(sample_rate)
        impulse[240] = 1.0
        plain = build_complex_response(impulse, sample_rate)
        calibrated = build_complex_response(
            impulse,
            sample_rate,
            calibration_curve=(np.array([20.0, 20_000.0]), np.array([6.0, 6.0])),
        )

        plain_value = abs(complex(*plain["points"][50][1:]))
        calibrated_value = abs(complex(*calibrated["points"][50][1:]))
        self.assertAlmostEqual(calibrated_value / plain_value, 10 ** (-6 / 20), places=6)

    def test_complex_response_preserves_delay_and_sums_vectors(self):
        sample_rate = 48_000
        first = np.zeros(sample_rate)
        second = np.zeros(sample_rate)
        first[240] = 1.0
        second[480] = 0.5
        first_response = build_complex_response(first, sample_rate)
        second_response = build_complex_response(second, sample_rate)
        summed = sum_complex_points(first_response, second_response)

        self.assertEqual(len(summed), 160)
        for index in (0, 40, 100, 159):
            expected = complex(*first_response["points"][index][1:]) + complex(*second_response["points"][index][1:])
            actual = complex(*summed[index][1:])
            self.assertAlmostEqual(actual.real, expected.real, places=8)
            self.assertAlmostEqual(actual.imag, expected.imag, places=8)


if __name__ == "__main__":
    unittest.main()
