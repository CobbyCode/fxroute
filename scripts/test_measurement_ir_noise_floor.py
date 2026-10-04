#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Pre-arrival IR noise must not become acoustic timing evidence."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from measurement.analyzer import MeasurementAnalyzer
from measurement.store import MeasurementStore


class ImpulseNoiseFloorTests(unittest.TestCase):
    RATE = 48000

    def setUp(self):
        self.analyzer = MeasurementAnalyzer(None, RuntimeError)
        self.reference = np.zeros(24000)
        self.reference[16000] = 1.0

    def noise(self):
        # The physical Sub-1 recording had about 8% RMS noise relative
        # to its IR peak, above the fixed 5% arrival threshold.
        return np.clip(np.random.default_rng(91).normal(0.0, 0.08, 24000), -0.32, 0.32)

    def test_noise_before_reference_does_not_replace_a_delayed_impulse(self):
        impulse = self.noise()
        impulse[16240] = 1.0
        result = self.analyzer._estimate_impulse_direct_arrival(
            impulse, self.reference, self.RATE, band_limited=True)
        self.assertTrue(result["timing_valid"], result["relative_samples"])
        self.assertEqual(result["direct_arrival_index"], 16240)
        self.assertEqual(result["relative_samples"], 240)

    def test_real_early_impulse_is_still_rejected_despite_noise(self):
        impulse = self.noise()
        impulse[15760] = 1.0
        result = self.analyzer._estimate_impulse_direct_arrival(
            impulse, self.reference, self.RATE, band_limited=True)
        self.assertFalse(result["timing_valid"])
        self.assertEqual(result["direct_arrival_index"], 15760)
        self.assertEqual(result["relative_samples"], -240)
        self.assertEqual(result["confidence"], 0.0)

    def test_noise_only_cannot_claim_a_late_acoustic_arrival(self):
        impulse = self.noise()
        # The largest noise excursion is deliberately after the reference.
        impulse[17000] = 0.33
        reference = np.zeros_like(impulse)
        reference[4000] = 1.0
        result = self.analyzer._estimate_impulse_direct_arrival(
            impulse, reference, self.RATE, band_limited=True)
        self.assertFalse(result["timing_valid"], result["relative_samples"])
        self.assertFalse(result["direct_arrival_detected"])
        self.assertEqual(result["confidence"], 0.0)

    def test_quiet_early_direct_sound_is_not_hidden_by_a_late_reflection(self):
        impulse = np.zeros_like(self.reference)
        impulse[16240] = 0.15
        impulse[18000] = 1.0
        result = self.analyzer._estimate_impulse_direct_arrival(
            impulse, self.reference, self.RATE, band_limited=True)
        self.assertTrue(result["timing_valid"])
        self.assertEqual(result["relative_samples"], 240)

    def test_noise_limited_timing_fallback_explains_missing_arrival(self):
        with tempfile.TemporaryDirectory() as home:
            with patch.dict(os.environ, {
                "XDG_CONFIG_HOME": str(Path(home) / "config"),
                "XDG_STATE_HOME": str(Path(home) / "state"),
                "XDG_DATA_HOME": str(Path(home) / "data"),
            }):
                store = MeasurementStore(home=Path(home))
                status = store._evaluate_electrical_reference_status({
                    "direct_arrival_timing_available": False,
                    "impulse_response": {"direct_arrival_detected": False},
                })
        self.assertFalse(status["usable"])
        self.assertIn("noise", status["warning"].lower())

    def test_noisy_band_limited_reference_rise_keeps_relative_arrival(self):
        reference = np.zeros(24000)
        reference[15000:16001] = np.linspace(0.0, 1.0, 1001) ** 3
        reference[16001:17001] = np.linspace(1.0, 0.0, 1000) ** 3
        reference += np.random.default_rng(72).normal(0.0, 0.001, reference.size)
        impulse = np.roll(reference, 240)
        result = self.analyzer._estimate_impulse_direct_arrival(
            impulse, reference, self.RATE, band_limited=True)
        self.assertTrue(result["timing_valid"])
        self.assertEqual(result["relative_samples"], 240)

    def test_unequal_noise_floors_cannot_certify_a_negative_slow_rise(self):
        reference = np.zeros(24000)
        reference[14700:16001] = np.linspace(0.0, 1.0, 1301) ** 3
        reference[16001:17301] = np.linspace(1.0, 0.0, 1300) ** 3
        for noisy_side in ("microphone", "reference"):
            with self.subTest(noisy_side=noisy_side):
                impulse = np.roll(reference, -240)
                noisy_reference = reference.copy()
                if noisy_side == "microphone":
                    impulse += self.noise()
                else:
                    noisy_reference += self.noise()
                result = self.analyzer._estimate_impulse_direct_arrival(
                    impulse, noisy_reference, self.RATE, band_limited=True)
                self.assertFalse(result["timing_valid"], result["relative_samples"])
                self.assertEqual(result["confidence"], 0.0)

    def test_unequal_noise_floors_use_comparable_positive_slow_rise_points(self):
        reference = np.zeros(24000)
        reference[14700:16001] = np.linspace(0.0, 1.0, 1301) ** 3
        reference[16001:17301] = np.linspace(1.0, 0.0, 1300) ** 3
        for noisy_side in ("microphone", "reference"):
            with self.subTest(noisy_side=noisy_side):
                impulse = np.roll(reference, 240)
                noisy_reference = reference.copy()
                if noisy_side == "microphone":
                    impulse += self.noise()
                else:
                    noisy_reference += self.noise()
                result = self.analyzer._estimate_impulse_direct_arrival(
                    impulse, noisy_reference, self.RATE, band_limited=True)
                self.assertTrue(result["timing_valid"], result["relative_samples"])
                self.assertAlmostEqual(result["relative_samples"], 240, delta=120)

    def test_short_noise_only_ir_cannot_use_an_absent_pre_search_window(self):
        impulse = self.noise()[:5000]
        impulse[4500] = 0.33
        reference = np.zeros_like(impulse)
        reference[500] = 1.0
        result = self.analyzer._estimate_impulse_direct_arrival(
            impulse, reference, self.RATE, band_limited=True)
        self.assertFalse(result["timing_valid"], result["relative_samples"])
        self.assertFalse(result["direct_arrival_detected"])

    def test_clean_gain_scaling_does_not_activate_noise_floor_comparison(self):
        reference = np.zeros(24000)
        reference[15940:16001] = np.linspace(0.0, 1.0, 61) ** 3
        reference[16001:16061] = np.linspace(1.0, 0.0, 60) ** 3
        for gain in (1.0, 0.020640947605910398):
            with self.subTest(gain=gain):
                impulse = np.roll(reference, 240) * gain
                result = self.analyzer._estimate_impulse_direct_arrival(
                    impulse, reference * gain, self.RATE, band_limited=True)
                self.assertTrue(result["timing_valid"])
                self.assertEqual(result["relative_samples"], 203)
                self.assertEqual(result["reference_arrival_rule"], "reference_peak")

    def test_absent_reference_is_reported_as_noise_limited_not_negative(self):
        impulse = self.noise()
        impulse[16240] = 1.0
        result = self.analyzer._estimate_impulse_direct_arrival(
            impulse, np.zeros_like(impulse), self.RATE, band_limited=True)
        self.assertFalse(result["timing_valid"])
        self.assertTrue(result["direct_arrival_detected"])
        self.assertFalse(result["reference_arrival_detected"])
        with tempfile.TemporaryDirectory() as home:
            with patch.dict(os.environ, {
                "XDG_CONFIG_HOME": str(Path(home) / "config"),
                "XDG_STATE_HOME": str(Path(home) / "state"),
                "XDG_DATA_HOME": str(Path(home) / "data"),
            }):
                store = MeasurementStore(home=Path(home))
                status = store._evaluate_electrical_reference_status({
                    "direct_arrival_timing_available": False,
                    "impulse_response": {
                        "direct_arrival_detected": result["direct_arrival_detected"],
                        "reference_arrival_detected": result["reference_arrival_detected"],
                    },
                })
        self.assertFalse(status["usable"])
        self.assertIn("noise", status["warning"].lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
