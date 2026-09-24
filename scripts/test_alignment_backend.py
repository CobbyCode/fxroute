#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Shared alignment backend: neutralization, passband gains, verification."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audio.output_state import default_output_state, set_crossover, set_mode_routing, switch_mode
from audio.output_service import OutputService, OutputServiceDeps
from audio.output_state_store import OutputStateStore
from dsp.manager import DSPManager
from measurement.alignment_backend import (
    estimate_way_level,
    propose_way_gains,
    verify_gain_spread,
    way_passband,
)

import tempfile
from pathlib import Path


def _capture(level_db: float, *, start_hz: float = 20.0, end_hz: float = 20000.0, count: int = 64):
    import math
    points = []
    for index in range(count):
        fraction = index / (count - 1)
        frequency = start_hz * (end_hz / start_hz) ** fraction
        points.append([round(frequency, 3), round(level_db, 3)])
    return {"analysis": {"normalized_by_db": 0.0, "review_points": points}}


class PassbandTests(unittest.TestCase):
    def test_two_way_passbands_avoid_crossover(self):
        low = {"highpass": None, "lowpass": {"family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 2000}}
        high = {"highpass": {"family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 2000}, "lowpass": None}
        low_band = way_passband(low, sample_rate_hz=48000)
        high_band = way_passband(high, sample_rate_hz=48000)
        self.assertLess(low_band[1], 2000.0)
        self.assertGreater(high_band[0], 2000.0)
        self.assertGreater(low_band[1], low_band[0])
        self.assertGreater(high_band[1], high_band[0])

    def test_mid_way_passband_between_cutoffs(self):
        mid = {"highpass": {"family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 300},
               "lowpass": {"family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 2500}}
        band = way_passband(mid, sample_rate_hz=48000)
        self.assertGreater(band[0], 300.0)
        self.assertLess(band[1], 2500.0)

    def test_shallow_slope_needs_wider_guard_than_steep(self):
        shallow = {"highpass": {"family": "butterworth", "slope_db_oct": 6, "frequency_hz": 2000}, "lowpass": None}
        steep = {"highpass": {"family": "linkwitz-riley", "slope_db_oct": 48, "frequency_hz": 2000}, "lowpass": None}
        shallow_band = way_passband(shallow, sample_rate_hz=48000)
        steep_band = way_passband(steep, sample_rate_hz=48000)
        self.assertGreater(shallow_band[0], steep_band[0])


class GainTests(unittest.TestCase):
    def test_median_not_single_point_and_not_total_energy(self):
        capture = _capture(-10.0)
        capture["analysis"]["review_points"][0][1] = 30.0
        estimate = estimate_way_level(capture, (100.0, 8000.0))
        self.assertAlmostEqual(estimate["level_db"], -10.0, delta=0.5)
        narrow = _capture(-10.0, start_hz=100.0, end_hz=200.0, count=32)
        wide = _capture(-10.0, start_hz=100.0, end_hz=8000.0, count=32)
        narrow_estimate = estimate_way_level(narrow, (100.0, 200.0))
        wide_estimate = estimate_way_level(wide, (100.0, 8000.0))
        self.assertAlmostEqual(narrow_estimate["level_db"], wide_estimate["level_db"], delta=0.01)

    def test_narrow_resonances_do_not_move_smoothed_median(self):
        import math
        capture = _capture(-10.0, count=96)
        for point in capture["analysis"]["review_points"]:
            frequency = point[0]
            # Narrow +12 dB resonances every octave, plus broadband tilt.
            if abs(math.log2(frequency / 1000.0) % 1.0) < 0.05:
                point[1] += 12.0
            point[1] += 2.0 * math.log2(frequency / 1000.0)
        estimate = estimate_way_level(capture, (200.0, 8000.0))
        self.assertLess(estimate["mad_db"], 6.0)
        self.assertAlmostEqual(estimate["level_db"], -10.0, delta=2.0)

    def test_insufficient_points_fail_closed(self):
        capture = _capture(-10.0, count=4)
        with self.assertRaises(ValueError):
            estimate_way_level(capture, (100.0, 8000.0))

    def test_propose_equalizes_to_median(self):
        corrections = propose_way_gains({"left_low": -12.0, "left_high": -8.0})
        self.assertEqual(corrections, {"left_low": 2.0, "left_high": -2.0})
        three = propose_way_gains({"a": -12.0, "b": -10.0, "c": -8.0})
        self.assertEqual(three, {"a": 2.0, "b": 0.0, "c": -2.0})

    def test_implausible_gain_fails_closed(self):
        with self.assertRaises(ValueError):
            propose_way_gains({"left_low": -30.0, "left_high": 0.0})

    def test_verify_spread(self):
        ok = verify_gain_spread({"left_low": -10.0, "left_high": -10.5})
        self.assertTrue(ok["confirmed"])
        bad = verify_gain_spread({"left_low": -10.0, "left_high": -12.5})
        self.assertFalse(bad["confirmed"])


class NeutralizeTests(unittest.TestCase):
    def test_alignment_plan_neutralizes_banks_but_keeps_crossover(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        manager = DSPManager(home=Path(directory.name) / "home")
        service = OutputService(OutputServiceDeps(
            store=OutputStateStore(Path(directory.name) / "output-state.json"),
            preset_loader=manager.preset_store.read,
            resolve_ir=lambda kernel: (_ for _ in ()).throw(AssertionError(kernel)),
            measurement_active=lambda: False))
        state = default_output_state()
        state = set_crossover(state, "stereo", True)
        state = set_mode_routing(state, "stereo", "dev",
                                 ["left_low", "left_high", "right_low", "right_high"])
        state = switch_mode(state, "stereo")
        filt = {"family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 2000}
        from audio.output_state import set_output_processing
        for role in ("left_low", "right_low"):
            state = set_output_processing(state, "stereo", role, lowpass=filt)
        for role in ("left_high", "right_high"):
            state = set_output_processing(state, "stereo", role, highpass=filt)
        full = service.compile_plan(state, output_key="dev", channels=4, sample_rate_hz=48000)
        neutral = service.compile_alignment_plan(state, output_key="dev", channels=4, sample_rate_hz=48000)
        self.assertTrue(all(output["bank"]["bypass"] for output in neutral["outputs"]))
        self.assertEqual(neutral["global"]["extras"], {})
        self.assertTrue(all(output["crossover"] for output in neutral["outputs"]))
        self.assertEqual([output["role"] for output in neutral["outputs"]],
                         [output["role"] for output in full["outputs"]])
        self.assertEqual([output["gain_db"] for output in neutral["outputs"]],
                         [output["gain_db"] for output in full["outputs"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
