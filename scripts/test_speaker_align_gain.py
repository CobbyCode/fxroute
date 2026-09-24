#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Speaker gain alignment from usable passbands, per side, 2/3/4-way."""

from __future__ import annotations

import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scripts.test_speaker_align import alignment_for, captures_for, planning_for, state_for
from measurement.speaker_apply import verify_confirmation


def points_at(level_db: float, count: int = 96):
    points = []
    for index in range(count):
        fraction = index / (count - 1)
        frequency = 20.0 * (20000.0 / 20.0) ** fraction
        points.append([round(frequency, 3), round(level_db, 3)])
    return points


def shaped_points(alignment, role: str, level_db: float, rate: int = 48000, count: int = 96):
    """Synthetic way response including its rendered crossover shape (like real captures)."""
    from dsp.crossover import crossover_response, design_crossover
    from measurement.alignment_backend import way_crossover_specs
    import math
    sections = []
    for spec in way_crossover_specs(alignment.way_models()[role]):
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
        proposal = alignment.propose(
            captures, planning=planning_for(alignment), live_target=live)
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
        # Way spacing is wider than every band's own lobe, the same fixture rule
        # the timing suite uses: the shared planning take must be able to
        # separate the ways before the gains are proposed.
        for ways, cutoffs, arrivals, levels, expected in (
            (("low", "mid", "high"), (300, 2500), (96, 336, 576), (-12.0, -10.0, -8.0),
             {"right_low": 2.0, "right_mid": 0.0, "right_high": -2.0}),
            (("low", "low_mid", "mid", "high"), (300, 1000, 3000),
             (96, 336, 576, 816), (-13.0, -11.0, -9.0, -7.0),
             {"right_low": 3.0, "right_low_mid": 1.0, "right_mid": -1.0, "right_high": -3.0}),
        ):
            with self.subTest(ways=ways):
                state, channels = state_for(ways, cutoffs)
                alignment, live = alignment_for(state, channels, side="right")
                captures = with_levels(alignment, captures_for(alignment, arrivals, cutoffs=cutoffs), levels)
                proposal = alignment.propose(
                    captures, planning=planning_for(alignment, arrivals), live_target=live)
                for role, gain in expected.items():
                    self.assertAlmostEqual(proposal["added_gain_db"][role], gain, delta=0.2)

    def test_single_point_and_total_energy_are_not_used(self):
        state, channels = state_for()
        alignment, live = alignment_for(state, channels)
        captures = with_levels(alignment, captures_for(alignment), (-10.0, -10.0))
        # One outlier point must not move the median.
        captures[0]["analysis"]["review_points"][0][1] = 30.0
        proposal = alignment.propose(
            captures, planning=planning_for(alignment), live_target=live)
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
            alignment.propose(captures, planning=planning_for(alignment), live_target=live)


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
            with_levels(alignment, captures_for(alignment), (-12.0, -8.0)),
            planning=planning_for(alignment), live_target=live)
        check = verify_confirmation(baseline, self.confirmation_with_levels(
            baseline, {"left_low": -10.0, "left_high": -10.2}))
        self.assertTrue(check["confirmed"])
        self.assertAlmostEqual(check["gain_spread_db"], 0.2, delta=0.01)
        self.assertEqual(check["warnings"], [])

    def test_gain_spread_does_not_veto_the_timing_verdict(self):
        # The check reads its levels from another take than the plan did, so
        # an unexpected spread is reported next to the verdict instead of
        # failing a take whose timing did measure aligned.
        state, channels = state_for()
        alignment, live = alignment_for(state, channels)
        baseline = alignment.propose(
            with_levels(alignment, captures_for(alignment), (-12.0, -8.0)),
            planning=planning_for(alignment), live_target=live)
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
            with_levels(alignment, captures_for(alignment), (-12.0, -8.0)),
            planning=planning_for(alignment), live_target=live)
        confirmation = self.confirmation_with_levels(baseline, {"left_low": -10.0, "left_high": -10.0})
        confirmation["arrival_ms"] = {"left_low": 0.0, "left_high": 7.5417}
        check = verify_confirmation(baseline, confirmation)
        self.assertFalse(check["confirmed"])
        self.assertIn("residual", check["reasons"][0])


class GainLandsTests(unittest.TestCase):
    """The post-apply level check compares the quantity the gain was planned from.

    Planned gains come from calibrated per-way levels; the verification take is
    read through the same microphone calibration and without its band
    weighting, so a correction that landed reads as landed.
    """

    MICROPHONE = {"frequencies_hz": [20.0, 1000.0, 3000.0, 20000.0],
                  "offsets_db": [0.0, 0.0, 1.0, 3.0]}

    def verification(self, alignment, levels_db, *, calibrated):
        import speaker_take_test_support as takes
        frequencies = np.fft.rfftfreq(takes.TAKE_SAMPLES, 1.0 / takes.RATE)
        roles = alignment.verification_request()["roles"]
        take = takes.shared_take({role: 5.0 for role in roles}, processing=alignment.way_models(),
                                 roles=roles, gains_db=levels_db)
        offsets = np.interp(np.log(np.clip(frequencies, 1e-9, None)),
                            np.log(self.MICROPHONE["frequencies_hz"]), self.MICROPHONE["offsets_db"],
                            left=0.0, right=3.0)
        heard = np.fft.irfft(np.fft.rfft(take) * 10.0 ** (offsets / 20.0), n=takes.TAKE_SAMPLES)
        document = takes.take_document(alignment, alignment.verification_request(), heard)
        document["calibration_curve"] = self.MICROPHONE if calibrated else None
        return alignment.confirmation(document)

    def test_a_landed_gain_correction_reads_as_landed(self):
        state, channels = state_for()
        alignment, live = alignment_for(state, channels)
        proposal = alignment.propose(
            with_levels(alignment, captures_for(alignment), (-12.0, -8.0)),
            planning=planning_for(alignment), live_target=live)
        self.assertEqual(proposal["added_gain_db"], {"left_low": 2.0, "left_high": -2.0})
        # The drivers after the apply: both ways at the median level.
        landed = {"left_low": -12.0 + 2.0, "left_high": -8.0 - 2.0}
        check = verify_confirmation(proposal, self.verification(alignment, landed, calibrated=True))
        self.assertTrue(check["confirmed"])
        self.assertLess(check["gain_spread_db"], 0.5)
        self.assertEqual(check["warnings"], [])
        # Read without the calibration, the microphone's treble rise would be
        # reported as a correction that did not land.
        raw = verify_confirmation(proposal, self.verification(alignment, landed, calibrated=False))
        self.assertGreater(raw["gain_spread_db"], 2.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
