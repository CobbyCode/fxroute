#!/usr/bin/env python3
"""Regression tests for the fine-winner confirmation path (2.1).

Reproduces the reported run: the fine scan found a measurably better
alignment (fine -3.71 ms / 67.7% vs incumbent 0.0 ms / 59.2%), but the
winner was discarded before the real confirmation gate because of

  1. the xo_loss_vs_coarse component veto in
     _auto_sub_select_accepted_winner (cross-basis comparison of the fine
     winner's component scores against the coarse winner's), and
  2. the confidence/margin pre-gate in the apply ladder, which ran a
     second time on top of the already accepted fine winner.

Both gates are removed conceptually: the fine winner's own combined
score is the single decision basis, and an accepted fine winner goes
straight into the real confirmation gate (apply decision
"applied_fine_scan_winner").
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import measurement.autosub as autosub


def scored(delay_ms, score, **extra):
    return {
        "delay_ms": delay_ms,
        "score": score,
        "score_pct": score * 100.0,
        "xo_score": score,
        "timing_band_score": score,
        "low_guard_loss_db": 0.0,
        **extra,
    }


class FineWinnerReachesConfirmationTests(unittest.TestCase):
    def test_fine_winner_not_vetoed_by_component_metrics_vs_coarse(self):
        # User scenario: fine winner beats coarse winner and incumbent on
        # the combined score, but its xo component (computed on a different
        # scan basis) is slightly below the coarse winner's. Previously the
        # xo_loss_vs_coarse veto discarded the fine winner.
        coarse = scored(-3.12, 0.592, xo_score=0.90, timing_band_score=0.90)
        fine = scored(-3.71, 0.677, xo_score=0.80, timing_band_score=0.85)
        incumbent = scored(0.0, 0.592, xo_score=0.85, timing_band_score=0.85)

        decision = autosub._auto_sub_select_accepted_winner(
            coarse_winner=coarse,
            fine_winner=fine,
            incumbent_winner=incumbent,
        )

        self.assertIs(decision["accepted_winner"], fine)
        self.assertTrue(decision["fine_accepted"])
        self.assertIsNone(decision["reject_reason"])

    def test_fine_winner_still_rejected_when_not_measurably_better(self):
        # The combined-score basis stays decisive: a fine winner that does
        # not beat the protected winner must still be rejected.
        coarse = scored(-3.12, 0.700)
        fine = scored(-3.71, 0.699)
        incumbent = scored(0.0, 0.500)

        decision = autosub._auto_sub_select_accepted_winner(
            coarse_winner=coarse,
            fine_winner=fine,
            incumbent_winner=incumbent,
        )

        self.assertIs(decision["accepted_winner"], coarse)
        self.assertFalse(decision["fine_accepted"])
        self.assertEqual(decision["reject_reason"], "fine_not_better")

    def test_fine_winner_still_rejected_when_incumbent_protected(self):
        # Incumbent protection stays intact: if the incumbent (roughly) ties
        # or beats the coarse winner, it is the protected winner and a fine
        # winner must beat it, not just the coarse winner.
        coarse = scored(-3.12, 0.500)
        fine = scored(-3.71, 0.510)
        incumbent = scored(0.0, 0.600)

        decision = autosub._auto_sub_select_accepted_winner(
            coarse_winner=coarse,
            fine_winner=fine,
            incumbent_winner=incumbent,
        )

        self.assertIs(decision["accepted_winner"], incumbent)
        self.assertFalse(decision["fine_accepted"])
        self.assertEqual(decision["reject_reason"], "incumbent_better")

    def test_fine_winner_beats_incumbent_even_when_coarse_tied(self):
        # Incumbent protected the coarse winner; the fine winner must beat
        # the incumbent directly - and does.
        coarse = scored(-3.12, 0.600)
        fine = scored(-3.71, 0.677)
        incumbent = scored(0.0, 0.600)

        decision = autosub._auto_sub_select_accepted_winner(
            coarse_winner=coarse,
            fine_winner=fine,
            incumbent_winner=incumbent,
        )

        self.assertIs(decision["accepted_winner"], fine)
        self.assertTrue(decision["fine_accepted"])


if __name__ == "__main__":
    unittest.main()
