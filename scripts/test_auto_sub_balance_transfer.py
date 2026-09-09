#!/usr/bin/env python3
"""AutoSub balance-trim transfer semantics (Balance -> Alignment -> final Gain).

Regression coverage for the documented over-damping (real 2.2-stereo job
auto-sub-fcf88b3b82f3): the balance trim is measured for the incumbent
configuration. When the alignment changes, the post-alignment residual
still contains the part of the balance trim the first application did not
realise (band-median response below 1 dB per dB of sub trim). Adding that
full residual on top of the old trim re-closes the same room excess a
second time and over-damps the sub.

The transfer rule per changed channel is
    final delta = balance trim + (winner residual - incumbent residual)
with both residuals measured at the same balanced level. This reconstructs
the single-stage residual of the end configuration, which the tests verify
against an exact synthetic two-path room (|Main + Sub * e^{j w d}|).

Same-configuration jobs keep the existing fine-trim behaviour.
"""
import json
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import measurement.autosub as autosub
from measurement.autosub.measurement import _auto_sub_target_residual_raw_db

FREQ = [20.0 * (2.0 ** (k / 12.0)) for k in range(60)]  # 20..~640 Hz, 1/12 oct
TARGET = {"points": [[f, 0.0] for f in FREQ], "label": "flat"}
ANCHOR = {"status": "ready", "target_vertical_offset_db": 0.0}
W = [2.0 * math.pi * f for f in FREQ]


def two_path_curve(mag_main: float, mag_sub: float, level_db: float, delay_ms: float) -> list[list[float]]:
    """Calibrated curve of |Main + Sub * 10^(level/20) * e^{j w delay}|."""
    rows = []
    sub = mag_sub * (10.0 ** (level_db / 20.0))
    for f, wi in zip(FREQ, W):
        phase = wi * (delay_ms / 1000.0)
        h = mag_main + sub * complex(math.cos(phase), math.sin(phase))
        rows.append([f, 20.0 * math.log10(abs(h))])
    return rows


def residual(curve: list[list[float]]) -> float:
    return _auto_sub_target_residual_raw_db(curve, TARGET, ANCHOR, 80)[0]


class BalanceTransferMathTests(unittest.TestCase):
    def test_transfer_matches_single_stage_residual_on_alignment_change(self):
        # Incumbent configuration at the original level defines the balance
        # trim B. The accepted alignment +0.98 ms changes the interference
        # sum; the transfer must reconstruct that configuration's true
        # single-stage residual, while the old accumulation over-damps.
        for mag_main, mag_sub, delay_ms in ((0.55, 0.75, 0.98), (0.9, 0.6, 1.17), (0.62, 0.68, 0.98)):
            with self.subTest(room=(mag_main, mag_sub, delay_ms)):
                balance = residual(two_path_curve(mag_main, mag_sub, 0.0, 0.0))
                winner_residual = residual(two_path_curve(mag_main, mag_sub, balance, delay_ms))
                incumbent_residual = residual(two_path_curve(mag_main, mag_sub, balance, 0.0))
                true_single_stage = residual(two_path_curve(mag_main, mag_sub, 0.0, delay_ms))

                transfer = autosub._auto_sub_balance_transfer_deltas(
                    balance_deltas_db={"left": balance, "right": balance},
                    winner_residuals_db={"left": winner_residual, "right": winner_residual},
                    incumbent_residuals_db={"left": incumbent_residual, "right": incumbent_residual},
                    alignment_changed={"left": True, "right": True},
                )
                self.assertTrue(transfer["available"])
                for side in ("left", "right"):
                    channel = transfer["channels"][side]
                    self.assertEqual(channel["mode"], "configuration_transfer")
                    # The returned delta is the increment on top of the still
                    # applied balance trim; the implied total is the accepted
                    # configuration's single-stage residual.
                    self.assertAlmostEqual(
                        channel["implied_total_db"], true_single_stage, delta=0.3,
                        msg="implied total must equal the accepted configuration's single-stage residual",
                    )
                    self.assertAlmostEqual(
                        channel["delta_db"], true_single_stage - balance, delta=0.3,
                        msg="the increment must carry only the accepted configuration's own level effect",
                    )

                legacy_delta = balance + winner_residual
                self.assertLessEqual(
                    legacy_delta - true_single_stage, -0.8,
                    msg="old accumulation (balance trim + full post-alignment residual) must over-damp",
                )

    def test_real_job_values_transfer_instead_of_accumulating(self):
        # Measured decision-band residuals of the real 2.2-stereo job
        # auto-sub-fcf88b3b82f3 (band 40-160 Hz, repo gain math on the
        # stored sweeps): balance trims -0.359/-2.889 dB; accepted winners
        # (left -2.34 ms, right +0.98 ms) residual -0.282/-1.294 dB at the
        # balanced level; incumbent candidates (0.0/0.0 ms) residuals
        # -0.186/-1.136 dB at the same balanced level. The transfer yields
        # -0.455/-3.047 dB where the old accumulation produced -4.183 dB on
        # the right.
        # Measured decision-band residuals of the real 2.2-stereo job
        # auto-sub-fcf88b3b82f3, right channel: balance trim -2.889 dB,
        # accepted winner (+0.98 ms) residual -1.294 dB at the balanced
        # level, incumbent candidate (0.0 ms) residual -1.136 dB at the same
        # level. The transfer must land near -3.05 dB; the old accumulation
        # produced -4.183 dB.
        transfer = autosub._auto_sub_balance_transfer_deltas(
            balance_deltas_db={"left": -0.359, "right": -2.889},
            winner_residuals_db={"left": -0.282, "right": -1.294},
            incumbent_residuals_db={"left": -0.186, "right": -1.136},
            alignment_changed={"left": True, "right": True},
        )
        self.assertTrue(transfer["available"])
        right = transfer["channels"]["right"]
        self.assertEqual(right["mode"], "configuration_transfer")
        # Increment on top of the balance trim: -1.294 - (-1.136) = -0.158.
        self.assertAlmostEqual(right["delta_db"], -0.158, places=3)
        # Implied total: -2.889 + (-0.158) = -3.047 (the old accumulation
        # produced -4.183 dB).
        self.assertAlmostEqual(right["implied_total_db"], -3.047, places=3)
        self.assertNotAlmostEqual(right["implied_total_db"], -4.183, places=2)
        self.assertAlmostEqual(
            right["implied_total_db"] - (-2.889 + -1.294), 1.136, places=3,
            msg="the old accumulation must over-damp exactly by the balance stage's unrealized residual",
        )
        left = transfer["channels"]["left"]
        self.assertEqual(left["mode"], "configuration_transfer")
        self.assertAlmostEqual(left["implied_total_db"], -0.455, places=3)

    def test_same_configuration_keeps_fine_trim(self):
        # Without an alignment change the existing behaviour applies: the
        # residual of the SAME configuration is applied on top of the
        # balance trim.
        transfer = autosub._auto_sub_balance_transfer_deltas(
            balance_deltas_db={"left": -0.359, "right": -2.889},
            winner_residuals_db={"left": -0.282, "right": -0.113},
            incumbent_residuals_db={"left": -0.282, "right": -0.113},
            alignment_changed={"left": False, "right": False},
        )
        self.assertFalse(transfer["available"])
        self.assertEqual(transfer["reason"], "Accepted alignment matches the balance configuration; transfer not applicable")

    def test_mixed_sides_transfer_only_changed_side(self):
        transfer = autosub._auto_sub_balance_transfer_deltas(
            balance_deltas_db={"left": -0.359, "right": -2.889},
            winner_residuals_db={"left": -0.282, "right": -1.294},
            incumbent_residuals_db={"left": -0.282, "right": -1.136},
            alignment_changed={"left": False, "right": True},
        )
        self.assertTrue(transfer["available"])
        self.assertEqual(transfer["channels"]["left"]["mode"], "fine_trim_same_configuration")
        self.assertAlmostEqual(transfer["deltas_db"]["left"], -0.282, places=3)
        self.assertEqual(transfer["channels"]["right"]["mode"], "configuration_transfer")
        self.assertAlmostEqual(transfer["deltas_db"]["right"], -0.158, places=3)

    def test_missing_incumbent_residual_falls_back_to_balance_trim(self):
        transfer = autosub._auto_sub_balance_transfer_deltas(
            balance_deltas_db={"left": -2.889, "right": -2.889},
            winner_residuals_db={"left": -1.294, "right": -1.294},
            incumbent_residuals_db={"left": None, "right": None},
            alignment_changed={"left": True, "right": True},
        )
        self.assertTrue(transfer["available"])
        for side in ("left", "right"):
            self.assertEqual(transfer["channels"][side]["mode"], "fallback_balance_only")
            self.assertAlmostEqual(transfer["deltas_db"][side], 0.0, places=3)
            self.assertAlmostEqual(transfer["channels"][side]["implied_total_db"], -2.889, places=3)

    def test_missing_winner_residual_is_unavailable(self):
        transfer = autosub._auto_sub_balance_transfer_deltas(
            balance_deltas_db={"left": -2.889, "right": -2.889},
            winner_residuals_db={"left": None, "right": None},
            incumbent_residuals_db={"left": -1.136, "right": -1.136},
            alignment_changed={"left": True, "right": True},
        )
        self.assertFalse(transfer["available"])
        self.assertIn("winner residual", transfer["reason"])

    def test_transfer_result_is_clamped_to_bound(self):
        transfer = autosub._auto_sub_balance_transfer_deltas(
            balance_deltas_db={"left": -5.8, "right": -5.8},
            winner_residuals_db={"left": -6.9, "right": -6.9},
            incumbent_residuals_db={"left": -0.1, "right": -0.1},
            alignment_changed={"left": True, "right": True},
            max_abs_db=6.0,
        )
        self.assertTrue(transfer["available"])
        self.assertTrue(transfer["channels"]["left"]["clamped"])
        self.assertAlmostEqual(transfer["deltas_db"]["left"], -6.0, places=3)
        self.assertAlmostEqual(transfer["channels"]["left"]["implied_total_db"], -11.8, places=3)


if __name__ == "__main__":
    unittest.main()
