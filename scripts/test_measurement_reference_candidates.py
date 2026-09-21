#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""One take, several loopback channels: the store admits the usable reference.

Speaker Align records every configured loopback candidate in a single capture
and the store's existing electrical-reference evaluation decides which channel
actually carries that way.  These checks pin that reuse: the same thresholds,
the best usable candidate, and the untouched rejection when nothing qualifies.
"""

import tempfile
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from measurement.store import MeasurementStore


def analysis_for(*, peak_dbfs=-20.0, start_score=0.99, end_score=0.98,
                 sharpness_db=54.0, clipped=False):
    """Minimal analyzer-shaped analysis: only the ER evidence is meaningful."""
    return {
        "sample_rate": 48_000,
        "quality_checks": {"status": "pass", "items": []},
        "clock": {"start_score": start_score, "end_score": end_score},
        "reference_path": {
            "channel": "reference",
            "peak_dbfs": peak_dbfs,
            "rms_dbfs": peak_dbfs - 8.0,
            "start_score": start_score,
            "end_score": end_score,
            "alignment_score": min(start_score, end_score),
            "ir_sharpness_db": sharpness_db,
            "clipped": clipped,
        },
    }


def candidate(channel_index, *, error=None, **evidence):
    return {
        "channel_index": channel_index,
        "channel_label": f"input_{channel_index + 1}_electrical_reference",
        "analysis": None if error else analysis_for(**evidence),
        "error": error,
    }


class ReferenceCandidateSelectionTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory(prefix="reference-candidates-")
        self.addCleanup(self._directory.cleanup)
        self.store = MeasurementStore(home=Path(self._directory.name))

    def select(self, *candidates):
        return self.store._select_electrical_reference_candidate(list(candidates))

    @staticmethod
    def reference(candidate_record):
        return candidate_record["analysis"]["reference_path"]

    def test_silent_sibling_does_not_block_the_carrier_channel(self):
        chosen = self.select(
            candidate(6, error="RuntimeError: Input_7_electrical_reference channel was effectively silent"),
            candidate(7),
        )
        self.assertEqual(chosen["channel_index"], 7)
        self.assertTrue(self.reference(chosen)["electrical_reference_used"])
        diagnostics = self.reference(chosen)["electrical_reference_candidates"]
        self.assertEqual([item["input_channel"] for item in diagnostics], [7, 8])
        self.assertFalse(diagnostics[0]["usable"])
        self.assertIn("silent", diagnostics[0]["analysis_error"])
        self.assertTrue(diagnostics[1]["usable"])

    def test_first_channel_wins_only_when_it_is_the_usable_one(self):
        chosen = self.select(candidate(6), candidate(7, error="silent"))
        self.assertEqual(chosen["channel_index"], 6)
        self.assertTrue(self.reference(chosen)["electrical_reference_used"])

    def test_both_usable_candidates_pick_the_better_reference_evidence(self):
        # Equal alignment, different sharpness: the sharper reference wins.
        chosen = self.select(candidate(6, sharpness_db=30.0), candidate(7, sharpness_db=54.0))
        self.assertEqual(chosen["channel_index"], 7)
        self.assertAlmostEqual(self.reference(chosen)["confidence"], 0.9, places=3)

    def test_alignment_breaks_a_confidence_tie_before_channel_order(self):
        chosen = self.select(
            candidate(6, sharpness_db=60.0, start_score=0.90, end_score=0.90),
            candidate(7, sharpness_db=60.0, start_score=0.97, end_score=0.98),
        )
        self.assertEqual(chosen["channel_index"], 7)

    def test_reference_level_breaks_the_last_tie_before_channel_order(self):
        chosen = self.select(
            candidate(6, sharpness_db=60.0, peak_dbfs=-40.0),
            candidate(7, sharpness_db=60.0, peak_dbfs=-30.0),
        )
        self.assertEqual(chosen["channel_index"], 7)
        # Identical evidence keeps the lower input channel.
        self.assertEqual(self.select(candidate(6), candidate(7))["channel_index"], 6)

    def test_candidate_is_judged_by_the_existing_electrical_reference_rule(self):
        for rejected in (
            {"sharpness_db": 17.9},                      # below the 18 dB floor
            {"start_score": 0.83, "end_score": 0.99},    # below the 0.84 alignment floor
            {"peak_dbfs": -80.0},                        # below the -70 dBFS floor
            {"clipped": True},
        ):
            with self.subTest(rejected=rejected):
                chosen = self.select(candidate(6, **rejected), candidate(7))
                self.assertEqual(chosen["channel_index"], 7)
                self.assertTrue(self.reference(chosen)["electrical_reference_used"])

    def test_no_usable_candidate_returns_the_first_analyzed_one_unchanged(self):
        chosen = self.select(
            candidate(6, error="RuntimeError: silent"),
            candidate(7, sharpness_db=5.0),
        )
        self.assertEqual(chosen["channel_index"], 7)
        reference = self.reference(chosen)
        self.assertFalse(reference.get("electrical_reference_used", False))
        self.assertNotEqual(reference.get("usable"), True)
        diagnostics = reference["electrical_reference_candidates"]
        self.assertEqual([item["usable"] for item in diagnostics], [False, False])
        self.assertTrue(diagnostics[1]["warning"])

    def test_diagnostics_stay_json_safe(self):
        import json
        chosen = self.select(candidate(6, error="RuntimeError: silent"), candidate(7))
        json.dumps(self.reference(chosen))


if __name__ == "__main__":
    unittest.main(verbosity=2)
