#!/usr/bin/env python3
"""Synthetic direct-window and complex-response checks."""

import unittest
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hybrid_measurement import (
    analyze_direct_window,
    build_complex_response,
    build_gated_response,
    sum_complex_points,
)


class HybridMeasurementAnalysisTests(unittest.TestCase):
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
        self.assertEqual(result["first_reflection_index"], reflection)
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
