#!/usr/bin/env python3
"""Summation-first separation for AutoSub Delay/Polarity vs Gain.

Binding order: Delay and Polarity optimize acoustic summation / least
cancellation only; Gain afterwards is the single place that may use
Target/Anchor for level adaptation.

Concrete regression: real 2.2-stereo job auto-sub-b314da49f859
(right 0.78 ms vs incumbent 0.0 ms, fc 80, Neutral target):
  winner  0.78: score 0.4959, mean_pri 48.9, dip 10.9, swing_pri 16.6,
                rough 1.804, mean_sec 50.0, min_sec 42.2, swing_sec 12.0,
                xo 0.6304, timing 0.6537
  incumbent 0.0: score 0.4096, mean_pri 48.6, dip 10.7, swing_pri 16.1,
                rough 1.711, mean_sec 49.9, min_sec 43.1, swing_sec 10.9,
                xo 0.6716, timing 0.5339
The hotter candidate (+0.3 dB mean over a 0.3 dB set range) won despite
worse dip/swing/roughness and worse xo_score, because the mean carried
40 % per band. The same level-weighted scorer backs 2.1 and 2.2 mono
polarity decisions, so the fix is a shared rule, not a per-case weight
tweak: alignment scoring is level-invariant (shape only), and no
pre-alignment Target trim may precondition the scans.
"""

import inspect
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from measurement.store import score_sub_alignment_candidates
import measurement.autosub as autosub
import measurement.autosub.runners.optimize as runner_21
import measurement.autosub.runners.optimize_22 as runner_22mono
import measurement.autosub.runners.optimize_22_stereo as runner_22stereo

FC = 80
FREQS = [20.0, 25.0, 31.5, 40.0, 50.0, 63.0, 80.0, 100.0,
         125.0, 160.0, 200.0, 250.0, 315.0, 400.0, 500.0, 640.0]


def flat_points(level_db: float) -> list[list[float]]:
    return [[hz, level_db] for hz in FREQS]


def notch_points(level_db: float, notch_hz: float, depth_db: float) -> list[list[float]]:
    points = []
    for hz in FREQS:
        distance = abs(math.log2(hz / notch_hz)) if hz > 0 else 10.0
        dip = depth_db * math.exp(-(distance ** 2) / 0.05)
        points.append([hz, level_db - dip])
    return points


class SummationFirstScorerTests(unittest.TestCase):
    def test_b314_hotter_shape_worse_must_not_win(self):
        # Models b314 right: hotter level but deeper dip / larger swing /
        # rougher shape must lose to the flatter incumbent.
        incumbent = {"delay_ms": 0.0, "points": notch_points(50.0, 74.0, 10.7)}
        hotter = {"delay_ms": 0.78, "points": notch_points(50.3, 74.0, 10.9)}
        hotter["points"][3][1] += 0.5  # extra swing like 16.6 vs 16.1
        scoring = score_sub_alignment_candidates([incumbent, hotter], crossover_hz=FC)
        self.assertEqual(round(float(scoring["winner"]["delay_ms"]), 2), 0.0)
        by_delay = {round(float(r["delay_ms"]), 2): r for r in scoring["results"]}
        # Shape evidence, not level, decides: winner dip/swing strictly better.
        self.assertLess(by_delay[0.0]["dip_severity_db"], by_delay[0.78]["dip_severity_db"])
        self.assertLess(by_delay[0.0]["swing_primary_db"], by_delay[0.78]["swing_primary_db"])

    def test_pure_level_difference_ties_even_beyond_anchor_cap(self):
        # Flat shapes: 4 dB apart exceeds the +/-1.5 dB chain-anchor cap, so
        # anchored means still differ, yet the rank must tie because mean is
        # diagnostic only.
        scoring = score_sub_alignment_candidates(
            [
                {"delay_ms": 0.0, "points": flat_points(50.0)},
                {"delay_ms": 0.78, "points": flat_points(54.0)},
            ],
            crossover_hz=FC,
        )
        self.assertAlmostEqual(scoring["results"][0]["score"], scoring["results"][1]["score"], places=9)
        self.assertEqual(scoring["confidence"], "uncertain")

    def test_shape_decides_regardless_of_level(self):
        # Flat cold must beat notched hot: cancellation evidence outweighs any
        # absolute level.
        flat_cold = {"delay_ms": 0.0, "points": flat_points(50.0)}
        notched_hot = {"delay_ms": 0.78, "points": notch_points(51.0, 80.0, 12.0)}
        scoring = score_sub_alignment_candidates([flat_cold, notched_hot], crossover_hz=FC)
        self.assertEqual(round(float(scoring["winner"]["delay_ms"]), 2), 0.0)

    def test_scorer_has_no_absolute_mean_weight(self):
        import measurement.store as store_module

        source = inspect.getsource(store_module.score_sub_alignment_candidates)
        self.assertNotIn("n_pri_mean", source)
        self.assertNotIn("n_sec_mean", source)
        # Diagnostics stay: mean is reported but never scored.
        self.assertIn("mean_primary_db", source)


class NoPreAlignmentTargetTrimTests(unittest.TestCase):
    def _assert_runner(self, fn, mode: str) -> None:
        source = inspect.getsource(fn)
        self.assertIn("summation-first", source, f"{mode} must document the order")
        self.assertNotIn(
            "_auto_sub_balance_transfer_deltas(",
            source,
            f"{mode} must not use the legacy transfer path",
        )
        self.assertNotIn(
            "balance_deltas = _auto_sub_gain_deltas(",
            source,
            f"{mode} must not compute a pre-alignment Target trim",
        )

    def test_21_has_no_pre_alignment_trim(self):
        self._assert_runner(autosub._run_auto_sub_optimize, "2.1")

    def test_22_mono_has_no_pre_alignment_trim(self):
        self._assert_runner(autosub._run_auto_sub_22_optimize, "2.2 mono")

    def test_22_stereo_has_no_pre_alignment_trim(self):
        self._assert_runner(autosub._run_auto_sub_22_stereo_optimize, "2.2 stereo")

    def test_gain_stage_still_uses_target_anchor(self):
        # Only the Gain step may use Target/Anchor: each runner must still
        # calculate Gain from the accepted alignment afterwards.
        for fn, mode in (
            (runner_21._run_auto_sub_optimize, "2.1"),
            (runner_22mono._run_auto_sub_22_optimize, "2.2 mono"),
            (runner_22stereo._run_auto_sub_22_stereo_optimize, "2.2 stereo"),
        ):
            with self.subTest(mode=mode):
                source = inspect.getsource(fn)
                self.assertIn("_calculate_auto_sub_gain(", source)
                self.assertIn("_auto_sub_gain_deltas(", source)


if __name__ == "__main__":
    unittest.main()
