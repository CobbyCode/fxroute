#!/usr/bin/env python3
"""Gate-confidence regression: decisive vs fragile direct locks.

Uses the eight real stored IR segments (two stable left pairs, two
stable right-from-left pairs, one divergent right pair, three
reproducible right pairs) plus minimal synthetic cases.
"""

import json
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from measurement.hybrid import analyze_direct_window

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "direct-gate-segments.json").read_text()
)


def run_fixture(entry):
    return analyze_direct_window(
        np.array(entry["samples"], dtype=np.float64),
        entry["sample_rate"],
        entry["direct_arrival_index"] - entry["start_index"],
        timing_metadata={"confidence": 1.0, "selection_rule": "test", "selected_score": 0.5},
    )


class GateConfidenceTests(unittest.TestCase):
    def test_stable_left_segments_are_fully_confident(self):
        for entry in [item for item in FIXTURES if item["position"] == "left"]:
            with self.subTest(entry["id"]):
                result = run_fixture(entry)
                self.assertTrue(result["usable"])
                self.assertEqual(result["gate_confidence"], 1.0)
                self.assertEqual(
                    result["first_reflection_index"] + entry["start_index"],
                    entry["direct_arrival_index"]
                    + round((result["first_reflection_ms"] or 0) * entry["sample_rate"] / 1000),
                )

    def test_fragile_right_segments_have_zero_confidence(self):
        for entry in [item for item in FIXTURES if item["position"] == "right-A"]:
            with self.subTest(entry["id"]):
                result = run_fixture(entry)
                self.assertTrue(result["usable"])
                self.assertEqual(result["gate_confidence"], 0.0)

    def test_mid_confidence_right_from_left_segments(self):
        for entry in [item for item in FIXTURES if item["position"] == "right-from-left"]:
            with self.subTest(entry["id"]):
                result = run_fixture(entry)
                self.assertTrue(result["usable"])
                self.assertGreater(result["gate_confidence"], 0.5)
                self.assertLess(result["gate_confidence"], 1.0)

    def test_gate_metrics_are_reported(self):
        result = run_fixture(FIXTURES[0])
        metrics = result["reflection_detection"]["gate_metrics"]
        self.assertGreater(metrics["contrast"], 1.0)
        self.assertGreaterEqual(metrics["cluster"], 1)
        self.assertGreater(metrics["runner_ratio"], 0.0)

    def test_first_match_selection_is_unchanged(self):
        impulse = np.zeros(4000)
        impulse[500] = 1.0
        impulse[620] = 0.06
        impulse[900] = 0.35
        result = analyze_direct_window(impulse, 48000, 500)
        # Weak early event is still selected first; only confidence drops.
        self.assertEqual(result["status"], "ok")
        self.assertLessEqual(result["first_reflection_index"], 620 + 48)
        self.assertEqual(result["gate_confidence"], 0.0)

    def test_strong_clustered_reflection_is_confident(self):
        impulse = np.zeros(4000)
        impulse[500] = 1.0
        impulse[700:716] = 0.45
        result = analyze_direct_window(impulse, 48000, 500)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["gate_confidence"], 1.0)

    def test_missing_reflection_has_zero_confidence(self):
        impulse = np.zeros(4000)
        impulse[500] = 1.0
        result = analyze_direct_window(impulse, 48000, 500)
        self.assertFalse(result["usable"])
        self.assertEqual(result["gate_confidence"], 0.0)


if __name__ == "__main__":
    unittest.main()
