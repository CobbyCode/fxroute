#!/usr/bin/env python3
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.modules.setdefault("multipart", types.SimpleNamespace(__version__="0", multipart=types.SimpleNamespace(parse_options_header=lambda value: (value, {}))))

import main


class AutoSubPolarityTests(unittest.TestCase):
    def test_opposite_polarity(self):
        self.assertEqual(main._auto_sub_opposite_polarity("normal"), "invert")
        self.assertEqual(main._auto_sub_opposite_polarity("invert"), "normal")

    def test_incumbent_is_protected_for_unclear_gain(self):
        result = main._auto_sub_polarity_decision({"score": 0.70}, {"score": 0.729})
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "incumbent_protected_unclear_advantage")

    def test_clear_measured_gain_is_accepted(self):
        result = main._auto_sub_polarity_decision({"score": 0.70}, {"score": 0.731})
        self.assertTrue(result["accepted"])
        self.assertEqual(result["reason"], "alternative_clearly_better")

    def test_22_polarity_override_preserves_levels_and_delays(self):
        snapshot = {"subwoofers": {
            "sub1": {"level_db": -3.0, "alignment_ms": 1.0, "polarity": "normal"},
            "sub2": {"level_db": -5.0, "alignment_ms": 2.0, "polarity": "invert"},
        }}
        result = main._auto_sub_22_candidate_subwoofers(
            snapshot, sub1_alignment_ms=3.0, sub2_alignment_ms=4.0,
            active_subs=("sub1", "sub2"), sub1_polarity="invert", sub2_polarity="normal",
        )
        self.assertEqual(result["sub1"], {"level_db": -3.0, "alignment_ms": 3.0, "polarity": "invert"})
        self.assertEqual(result["sub2"], {"level_db": -5.0, "alignment_ms": 4.0, "polarity": "normal"})

    def test_inactive_sub_level_rule_remains_unchanged(self):
        snapshot = {"subwoofers": {"sub1": {}, "sub2": {}}}
        result = main._auto_sub_22_candidate_subwoofers(
            snapshot, sub1_alignment_ms=0, sub2_alignment_ms=0,
            active_subs=("sub1",), sub1_polarity="invert", sub2_polarity="invert",
        )
        self.assertEqual(result["sub1"]["level_db"], 0.0)
        self.assertEqual(result["sub2"]["level_db"], -80.0)
        self.assertEqual(result["sub2"]["polarity"], "invert")


if __name__ == "__main__":
    unittest.main()
