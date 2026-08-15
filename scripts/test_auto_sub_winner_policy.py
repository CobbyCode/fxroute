#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

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


class AutoSubWinnerPolicyTests(unittest.TestCase):
    def test_stereo_side_keeps_incumbent_for_sub_percent_gain(self):
        incumbent = scored(0.0, 0.800)
        candidate = scored(1.0, 0.805)

        decision = autosub._auto_sub_select_accepted_winner(
            coarse_winner=candidate,
            fine_winner=None,
            incumbent_winner=incumbent,
        )

        self.assertIs(decision["accepted_winner"], incumbent)

    def test_stereo_side_accepts_gain_above_minimum(self):
        incumbent = scored(0.0, 0.800)
        candidate = scored(1.0, 0.811)

        decision = autosub._auto_sub_select_accepted_winner(
            coarse_winner=candidate,
            fine_winner=None,
            incumbent_winner=incumbent,
        )

        self.assertIs(decision["accepted_winner"], candidate)

    def test_mono_matrix_keeps_incumbent_for_sub_percent_gain(self):
        candidates = [
            self._matrix_candidate(0.0, 0.0),
            self._matrix_candidate(1.0, 1.0),
        ]

        with patch.object(autosub, "score_sub_alignment_candidates", side_effect=self._matrix_scores(0.800, 0.805)):
            result = autosub._score_auto_sub_matrix_candidates(
                candidates,
                crossover_hz=80,
                original_sub1_alignment_ms=0.0,
                original_sub2_alignment_ms=0.0,
            )

        self.assertTrue(result["incumbent_accepted"])
        self.assertEqual(result["accepted_winner"]["sub1_alignment_ms"], 0.0)
        self.assertEqual(result["reject_reason"], "incumbent_gain_below_minimum")

    def test_mono_matrix_accepts_gain_above_minimum(self):
        candidates = [
            self._matrix_candidate(0.0, 0.0),
            self._matrix_candidate(1.0, 1.0),
        ]

        with patch.object(autosub, "score_sub_alignment_candidates", side_effect=self._matrix_scores(0.800, 0.811)):
            result = autosub._score_auto_sub_matrix_candidates(
                candidates,
                crossover_hz=80,
                original_sub1_alignment_ms=0.0,
                original_sub2_alignment_ms=0.0,
            )

        self.assertFalse(result["incumbent_accepted"])
        self.assertEqual(result["accepted_winner"]["sub1_alignment_ms"], 1.0)

    @staticmethod
    def _matrix_candidate(sub1, sub2):
        points = [[40.0, 0.0], [80.0, 0.0], [120.0, 0.0]]
        return {
            "sub1_alignment_ms": sub1,
            "sub2_alignment_ms": sub2,
            "points_left": points,
            "points_right": points,
        }

    @staticmethod
    def _matrix_scores(incumbent_score, candidate_score):
        def score(rows, **_kwargs):
            results = []
            for row in rows:
                index = int(row["delay_ms"])
                value = incumbent_score if index == 0 else candidate_score
                results.append(scored(index, value))
            results.sort(key=lambda item: item["score"], reverse=True)
            return {"results": results, "winner": results[0]}

        return score


if __name__ == "__main__":
    unittest.main()
