#!/usr/bin/env python3
"""Unit tests for the dipping-guard veto logic.

The 2.1 job 42eb6a557d9f exposed a per-side OR that vetoed a globally
better winner (65.2% vs 26.8%, +38 pp, L -7.65 / R +3.39) on a single-side
+3.39 trigger, even though the combined dip barely moved (+0.67). A real
2.2-stereo rejection at 74 Hz with +3.17 on one side and small scoring
gain should still veto.
"""

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from measurement.autosub.measurement import (
    _auto_sub_dip_guard_combined_db,
    _auto_sub_dip_guard_should_veto,
)


class AutoSubDipGuardTests(unittest.TestCase):
    def test_42eb_single_side_no_combined_veto(self):
        before = {"left": 15.05, "right": 4.86}
        final = {"left": 7.40, "right": 8.25}
        veto, diag = _auto_sub_dip_guard_should_veto(before, final)
        self.assertFalse(veto, diag)
        self.assertEqual(diag["reason"], "single_side_only_no_combined_deterioration")
        self.assertAlmostEqual(diag["combined_delta_db"], 0.67, places=1)

    def test_combined_deterioration_still_vetoes(self):
        before = {"left": 10.0, "right": 10.0}
        final = {"left": 14.0, "right": 13.0}
        veto, diag = _auto_sub_dip_guard_should_veto(before, final)
        self.assertTrue(veto, diag)
        self.assertEqual(diag["reason"], "combined_deterioration")

    def test_no_per_side_trigger_never_vetoes(self):
        before = {"left": 10.0, "right": 10.0}
        final = {"left": 11.0, "right": 11.5}
        veto, diag = _auto_sub_dip_guard_should_veto(before, final)
        self.assertFalse(veto)

    def test_combined_db_weighting_matches_scoring(self):
        self.assertAlmostEqual(_auto_sub_dip_guard_combined_db({"left": 10.0, "right": 5.0}), 0.6*5.0 + 0.4*7.5, places=4)
        self.assertIsNone(_auto_sub_dip_guard_combined_db({"left": 5.0, "right": None}))

    def test_rejected_74hz_like_case_still_vetoes_with_combined(self):
        before = {"left": 5.0, "right": 5.0}
        final = {"left": 8.2, "right": 8.0}
        veto, diag = _auto_sub_dip_guard_should_veto(before, final)
        self.assertTrue(veto)


if __name__ == "__main__":
    unittest.main()
