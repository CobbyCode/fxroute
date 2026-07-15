#!/usr/bin/env python3
"""Focused regression tests for diagnostic-only AutoSub candidate bookkeeping."""

import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main


def candidate(delay, left=3, right=3, **extra):
    return {
        "delay_ms": delay,
        "points_left": [[20.0 + i, -10.0] for i in range(left)],
        "points_right": [[20.0 + i, -10.0] for i in range(right)],
        "status_left": "completed",
        "status_right": "completed",
        **extra,
    }


class CandidateLedgerTests(unittest.TestCase):
    def ledger(self, candidates, scoring, **kwargs):
        with patch.object(main.logger, "info"):
            return main._auto_sub_candidate_ledger(candidates, scoring, **kwargs)

    def test_complete_pool_exclusion_and_roles_without_mutation(self):
        candidates = [candidate(0), candidate(1), candidate(2, right=0)]
        scoring = {
            "score_mode": "lr_combined",
            "results": [
                {"delay_ms": 1, "score": 0.8, "final_score": 0.75, "score_pct": 75.0},
                {"delay_ms": 0, "score": 0.7, "final_score": 0.65, "score_pct": 65.0},
            ],
            "winner": {"delay_ms": 1, "score": 0.8},
        }
        original_candidates = copy.deepcopy(candidates)
        original_scoring = copy.deepcopy(scoring)
        rows = self.ledger(
            candidates,
            scoring,
            mode="2.1",
            phase="coarse",
            roles={"coarse_winner": scoring["winner"], "final_accepted_winner": scoring["winner"]},
        )
        self.assertEqual(rows[2]["exclusion_reason"], "single_side_excluded_because_complete_candidates_available")
        self.assertEqual(rows[0]["score"], 0.7)
        self.assertEqual(rows[0]["final_score"], 0.65)
        self.assertEqual(rows[0]["requested_delay_ms"], 0)
        self.assertEqual(rows[1]["roles"], ["coarse_winner", "final_accepted_winner"])
        self.assertEqual(scoring["winner"], original_scoring["winner"])
        self.assertEqual(scoring["results"], original_scoring["results"])
        self.assertEqual(candidates, original_candidates)

    def test_majority_fallback_and_insufficient_reasons(self):
        candidates = [candidate(0, right=0), candidate(1, right=0), candidate(2, left=0)]
        scoring = {"score_mode": "left_fallback", "results": [{"delay_ms": 0, "score": 0.9}, {"delay_ms": 1, "score": 0.8}]}
        rows = self.ledger(candidates, scoring, mode="2.2_mono", phase="sub1_coarse")
        self.assertIsNone(rows[0]["exclusion_reason"])
        self.assertEqual(rows[2]["exclusion_reason"], "excluded_by_majority_side_fallback")

        rows = self.ledger([candidate(3, left=0, right=0)], {"results": []}, mode="2.1", phase="fine")
        self.assertEqual(rows[0]["exclusion_reason"], "both_insufficient_points")

    def test_matrix_pair_key_and_stereo_relevant_channel(self):
        matrix_candidate = candidate(1, sub1_alignment_ms=1.0, sub2_alignment_ms=2.0)
        matrix_score = {"sub1_alignment_ms": 1.0, "sub2_alignment_ms": 2.0, "score": 0.75}
        rows = self.ledger(
            [matrix_candidate], {"score_mode": "lr_combined_matrix", "results": [matrix_score]},
            mode="2.2_mono", phase="matrix", roles={"matrix_winner": matrix_score},
        )
        self.assertTrue(rows[0]["included_in_scoring"])
        self.assertEqual(rows[0]["roles"], ["matrix_winner"])

        stereo_candidate = {"delay_ms": 4.0, "points": [[20, -1], [30, -1], [40, -1]], "status": "completed"}
        rows = self.ledger(
            [stereo_candidate], {"results": [{"delay_ms": 4.0, "score": 0.6}]},
            mode="2.2_stereo", phase="right_coarse", channel="right",
        )
        self.assertEqual(rows[0]["points_right"], 3)
        self.assertTrue(rows[0]["eligible_for_scoring"])
        rows = self.ledger(
            [{"delay_ms": 5.0, "points": [], "status": "completed"}], {"results": []},
            mode="2.2_stereo", phase="right_fine", channel="right",
        )
        self.assertEqual(rows[0]["exclusion_reason"], "right_insufficient_points")

    def test_complete_candidates_can_exist_only_in_combined_decision_pool(self):
        coarse = [candidate(0), candidate(1, right=0)]
        fine = [candidate(2)]
        scoring = {
            "score_mode": "lr_combined",
            "results": [{"delay_ms": 0, "score": 0.8}, {"delay_ms": 2, "score": 0.9}],
        }
        rows = self.ledger(
            coarse, scoring, mode="2.1", phase="coarse", decision_pool=coarse + fine,
        )
        self.assertEqual(rows[1]["exclusion_reason"], "single_side_excluded_because_complete_candidates_available")

    def test_combined_missing_side_status_is_not_synthesized(self):
        measured = candidate(0, status="completed")
        measured.pop("status_right")
        rows = self.ledger(
            [measured], {"results": [{"delay_ms": 0, "score": 0.5}]}, mode="2.1", phase="coarse",
        )
        self.assertEqual(rows[0]["status_left"], "completed")
        self.assertIsNone(rows[0]["status_right"])

    def test_unscored_requested_incumbent_keeps_role(self):
        candidates = [candidate(0, right=0), candidate(1)]
        scoring = {"score_mode": "lr_combined", "results": [{"delay_ms": 1, "score": 0.8}]}
        rows = self.ledger(
            candidates, scoring, mode="2.1", phase="coarse",
            requested_incumbent={"delay_ms": 0},
        )
        self.assertFalse(rows[0]["included_in_scoring"])
        self.assertIn("incumbent", rows[0]["roles"])


if __name__ == "__main__":
    unittest.main()
