#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Speaker gain alignment from usable passbands, per side, 2/3/4-way."""

from __future__ import annotations

import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.test_speaker_align import alignment_for, captures_for, state_for
from measurement.speaker_apply import verify_confirmation


def points_at(level_db: float, count: int = 96):
    points = []
    for index in range(count):
        fraction = index / (count - 1)
        frequency = 20.0 * (20000.0 / 20.0) ** fraction
        points.append([round(frequency, 3), round(level_db, 3)])
    return points


def shaped_points(alignment, role: str, level_db: float, rate: int = 48000, count: int = 96):
    """Synthetic way response including its own crossover shape (like real captures)."""
    from dsp.crossover import crossover_response, design_crossover
    import math
    processing = alignment._state["modes"][alignment._state["active_mode"]]["processing"][role]
    specs = []
    for kind in ("highpass", "lowpass"):
        spec = processing.get(kind)
        if spec is None:
            continue
        full = dict(spec)
        full["kind"] = kind
        specs.append(full)
    sections = []
    for spec in specs:
        sections.extend(design_crossover(spec, rate))
    points = []
    for index in range(count):
        fraction = index / (count - 1)
        frequency = 20.0 * (20000.0 / 20.0) ** fraction
        total = 1.0 + 0.0j
        for b0, b1, b2, _, a1, a2 in sections:
            total *= crossover_response([(b0, b1, b2, 1.0, a1, a2)], frequency, rate)
        magnitude_db = 20.0 * math.log10(max(abs(total), 1e-12))
        points.append([round(frequency, 3), round(level_db + magnitude_db, 3)])
    return points


def with_levels(alignment, captures, levels, rate: int = 48000):
    for capture, level in zip(captures, levels):
        capture["analysis"]["normalized_by_db"] = 0.0
        capture["analysis"]["review_points"] = shaped_points(
            alignment, capture["role"], level, rate=rate)
    return captures


class GainProposalTests(unittest.TestCase):
    def test_two_way_gain_equalizes_to_median(self):
        state, channels = state_for()
        alignment, live = alignment_for(state, channels)
        captures = with_levels(alignment, captures_for(alignment), (-12.0, -8.0))
        proposal = alignment.propose(captures, live_target=live)
        self.assertAlmostEqual(proposal["way_levels_db"]["left_low"], -12.0, delta=0.15)
        self.assertAlmostEqual(proposal["way_levels_db"]["left_high"], -8.0, delta=0.15)
        self.assertAlmostEqual(proposal["added_gain_db"]["left_low"], 2.0, delta=0.15)
        self.assertAlmostEqual(proposal["added_gain_db"]["left_high"], -2.0, delta=0.15)
        processing = proposal["candidate_state"]["modes"]["stereo-sub"]["processing"]
        self.assertAlmostEqual(processing["left_low"]["level_db"], 2.0, delta=0.15)
        self.assertAlmostEqual(processing["left_high"]["level_db"], -2.0, delta=0.15)
        # Timing still from arrivals.
        self.assertEqual(proposal["added_delay_ms"], {"left_low": 3.0, "left_high": 0.0})

    def test_three_and_four_way_gains(self):
        for ways, cutoffs, arrivals, levels, expected in (
            (("low", "mid", "high"), (300, 2500), (96, 144, 240), (-12.0, -10.0, -8.0),
             {"right_low": 2.0, "right_mid": 0.0, "right_high": -2.0}),
            (("low", "low_mid", "mid", "high"), (300, 1000, 3000), (96, 144, 192, 240),
             (-13.0, -11.0, -9.0, -7.0),
             {"right_low": 3.0, "right_low_mid": 1.0, "right_mid": -1.0, "right_high": -3.0}),
        ):
            with self.subTest(ways=ways):
                state, channels = state_for(ways, cutoffs)
                alignment, live = alignment_for(state, channels, side="right")
                captures = with_levels(alignment, captures_for(alignment, arrivals, cutoffs=cutoffs), levels)
                proposal = alignment.propose(captures, live_target=live)
                for role, gain in expected.items():
                    self.assertAlmostEqual(proposal["added_gain_db"][role], gain, delta=0.2)

    def test_single_point_and_total_energy_are_not_used(self):
        state, channels = state_for()
        alignment, live = alignment_for(state, channels)
        captures = with_levels(alignment, captures_for(alignment), (-10.0, -10.0))
        # One outlier point must not move the median.
        captures[0]["analysis"]["review_points"][0][1] = 30.0
        proposal = alignment.propose(captures, live_target=live)
        self.assertAlmostEqual(proposal["added_gain_db"]["left_low"], 0.0, delta=0.5)
        # Narrow vs wide way with same level must agree (no total-energy bias).
        self.assertAlmostEqual(proposal["way_levels_db"]["left_low"],
                               proposal["way_levels_db"]["left_high"], delta=0.5)

    def test_missing_points_on_one_way_fails_closed(self):
        state, channels = state_for()
        alignment, live = alignment_for(state, channels)
        captures = with_levels(alignment, captures_for(alignment), (-12.0, -8.0))
        captures[1]["analysis"].pop("review_points", None)
        captures[1]["analysis"].pop("trusted_points", None)
        with self.assertRaises(ValueError):
            alignment.propose(captures, live_target=live)


class GainVerificationTests(unittest.TestCase):
    def confirmation_with_levels(self, baseline, levels):
        confirmation = dict(baseline)
        confirmation["arrival_ms"] = {"left_low": 5.0, "left_high": 5.0}
        confirmation["way_levels_db"] = levels
        return confirmation

    def test_gain_spread_is_reported_as_evidence(self):
        state, channels = state_for()
        alignment, live = alignment_for(state, channels)
        baseline = alignment.propose(
            with_levels(alignment, captures_for(alignment), (-12.0, -8.0)), live_target=live)
        check = verify_confirmation(baseline, self.confirmation_with_levels(
            baseline, {"left_low": -10.0, "left_high": -10.2}))
        self.assertTrue(check["confirmed"])
        self.assertAlmostEqual(check["gain_spread_db"], 0.2, delta=0.01)
        self.assertEqual(check["warnings"], [])

    def test_gain_spread_does_not_veto_the_timing_verdict(self):
        # A shared take measures both ways inside one take, while the planning
        # levels are calibrated per take: their reference moves by more than
        # 10 dB between takes, so an unexpected spread is reported next to the
        # verdict instead of failing a take whose timing did measure aligned.
        state, channels = state_for()
        alignment, live = alignment_for(state, channels)
        baseline = alignment.propose(
            with_levels(alignment, captures_for(alignment), (-12.0, -8.0)), live_target=live)
        check = verify_confirmation(baseline, self.confirmation_with_levels(
            baseline, {"left_low": -10.0, "left_high": -12.5}))
        self.assertTrue(check["confirmed"])
        self.assertAlmostEqual(check["gain_spread_db"], 2.5, delta=0.01)
        self.assertAlmostEqual(check["before_gain_spread_db"], 4.0, delta=0.01)
        self.assertEqual(check["gain_tolerance_db"], 2.0)
        self.assertEqual(len(check["warnings"]), 1)
        self.assertIn("level spread", check["warnings"][0])

    def test_misaligned_timing_still_vetoes(self):
        state, channels = state_for()
        alignment, live = alignment_for(state, channels)
        baseline = alignment.propose(
            with_levels(alignment, captures_for(alignment), (-12.0, -8.0)), live_target=live)
        confirmation = self.confirmation_with_levels(baseline, {"left_low": -10.0, "left_high": -10.0})
        confirmation["arrival_ms"] = {"left_low": 0.0, "left_high": 7.5417}
        check = verify_confirmation(baseline, confirmation)
        self.assertFalse(check["confirmed"])
        self.assertIn("residual", check["reasons"][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
