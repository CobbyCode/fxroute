#!/usr/bin/env python3
"""Focused tests for the diagnostic-only AutoSub Main/Target anchor gate."""

import copy
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import main


def log_points(low=20.0, high=1000.0, count=80, db=-12.0):
    return [[low * ((high / low) ** (index / (count - 1))), db] for index in range(count)]


def references(points=None, hp=True):
    points = points or log_points()
    return {
        "status": "completed", "main_highpass_enabled": hp,
        "left": {"status": "completed", "points": copy.deepcopy(points), "exact_sub_mute": True,
                 "normalized_by_db": -20.0, "crossover_frequency_hz": 80, "main_highpass_enabled": hp,
                 "sweep_id": "left-sweep", "measurement_channel": "left", "sample_rate": 48000},
        "right": {"status": "completed", "points": copy.deepcopy(points), "exact_sub_mute": True,
                  "normalized_by_db": -20.0, "crossover_frequency_hz": 80, "main_highpass_enabled": hp,
                  "sweep_id": "right-sweep", "measurement_channel": "right", "sample_rate": 48000},
    }


class MainTargetAnchorTests(unittest.TestCase):
    def setUp(self):
        self.target = {"key": "house", "label": "House", "provenance": "uploaded", "points": [[20, 4], [80, 2], [320, 0], [20000, -2]]}

    def analyze(self, refs=None, target=None, hp=True):
        return main._analyze_auto_sub_main_target_anchor(
            target_curve=self.target if target is None else target,
            main_references=references(hp=hp) if refs is None else refs,
            crossover_hz=80, main_highpass_enabled=hp,
        )

    def test_ready_gate_uses_transfer_derived_lower_and_bass_anchor_upper_without_gain(self):
        result = self.analyze()
        self.assertEqual(result["status"], "ready")
        self.assertGreater(result["usable_band_hz"][0], 80.0)
        self.assertLess(result["usable_band_hz"][0], 160.0)
        self.assertEqual(result["usable_band_hz"][1], 320.0)
        self.assertAlmostEqual(result["lr24_attenuation_at_lower_bound_db"], -1.0, places=5)
        self.assertGreaterEqual(result["usable_span_octaves"], 1.0)
        self.assertFalse(result["gain_calculated"])
        self.assertNotIn("gain_db", result)
        self.assertTrue(all(side["point_count"] >= 8 for side in result["sides"].values()))

    def test_log_frequency_interpolation_is_exact_at_geometric_midpoint(self):
        interpolated = main._auto_sub_log_interpolate_points([[100, 0], [400, 12]], [200])
        self.assertEqual(interpolated, [[200.0, 6.0]])

    def test_lr24_transfer_matches_cascaded_helper_response(self):
        self.assertAlmostEqual(main._auto_sub_lr24_highpass_attenuation_db(80, 80, 48000), -6.0206, places=3)
        threshold_hz = main._auto_sub_lr24_frequency_for_attenuation(80, 48000, -1.0)
        self.assertAlmostEqual(main._auto_sub_lr24_highpass_attenuation_db(threshold_hz, 80, 48000), -1.0, places=6)

    def test_target_is_interpolated_on_each_real_main_raster_without_extrapolation(self):
        left = log_points(40, 500, 60, -10)
        right = log_points(50, 450, 55, -11)
        refs = references(left)
        refs["right"]["points"] = right
        result = self.analyze(refs=refs)
        self.assertEqual(result["common_support_hz"], [50.0, 450.0])
        for side in ("left", "right"):
            aligned = result["sides"][side]["aligned_points"]
            source_frequencies = {round(point[0], 9) for point in refs[side]["points"]}
            self.assertTrue(all(round(point[0], 9) in source_frequencies for point in aligned))
            self.assertTrue(all(result["usable_band_hz"][0] <= point[0] <= 320 for point in aligned))

    def test_missing_side_and_insufficient_band_are_explicitly_unavailable(self):
        missing = references()
        missing["right"]["points"] = []
        self.assertIn("right", self.analyze(refs=missing)["reason"])
        narrow = references(log_points(110, 180, 20))
        result = self.analyze(refs=narrow)
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("octaves", result["reason"])

    def test_no_hp_still_uses_xo_as_lower_guard(self):
        result = self.analyze(hp=False)
        self.assertEqual(result["usable_band_hz"], [80.0, 320.0])

    def test_snapshots_and_display_offset_are_not_mutated(self):
        target = copy.deepcopy(self.target)
        refs = references()
        target_before, refs_before = copy.deepcopy(target), copy.deepcopy(refs)
        first = self.analyze(refs=refs, target=target)
        main._auto_sub_shared_bass_offset({"points": [[20, -1], [80, 1]]})
        second = self.analyze(refs=refs, target=target)
        self.assertEqual(target, target_before)
        self.assertEqual(refs, refs_before)
        self.assertEqual(first, second)

    def test_missing_exact_mute_confirmation_is_rejected(self):
        refs = references()
        refs["right"]["exact_sub_mute"] = False
        result = self.analyze(refs=refs)
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("exact-sub-mute", result["reason"])

    def test_result_diagnostics_are_deep_copied_at_finalize(self):
        anchor = self.analyze()
        job = {
            "status": "completed", "result": {}, "target_curve": self.target,
            "auto_gain": {"available": False}, "main_references": references(),
            "main_target_anchor": anchor,
        }
        main._finalize_autosub_job(job, "test-job")
        anchor["sides"]["left"]["aligned_points"][0][1] = 999
        self.assertNotEqual(job["result"]["main_target_anchor"]["sides"]["left"]["aligned_points"][0][1], 999)


if __name__ == "__main__":
    unittest.main()
