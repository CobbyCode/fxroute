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
import measurement.autosub as autosub
import measurement.analyzer as measurement_analyzer


def log_points(low=20.0, high=20000.0, count=192, db=-12.0):
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
        return autosub._analyze_auto_sub_main_target_anchor(
            target_curve=self.target if target is None else target,
            main_references=references(hp=hp) if refs is None else refs,
            crossover_hz=80, main_highpass_enabled=hp,
        )

    def test_ready_gate_uses_normal_measurement_broadband_reference_without_gain(self):
        result = self.analyze()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["reference_band_hz"], [120.0, 8000.0])
        self.assertEqual(result["usable_band_hz"], [120.0, 8000.0])
        self.assertGreater(result["usable_span_octaves"], 6.0)
        self.assertFalse(result["gain_calculated"])
        self.assertNotIn("gain_db", result)
        self.assertTrue(all(side["point_count"] >= 8 for side in result["sides"].values()))

    def test_shared_level_reference_matches_normal_measurement_semantics(self):
        points = [[80, 100], [120, -20], [1000, -10], [8000, 0], [10000, 100]]
        self.assertEqual(measurement_analyzer.measurement_level_reference_db(points), -10.0)

    def test_log_frequency_interpolation_is_exact_at_geometric_midpoint(self):
        interpolated = autosub._auto_sub_log_interpolate_points([[100, 0], [400, 12]], [200])
        self.assertEqual(interpolated, [[200.0, 6.0]])

    def test_target_is_interpolated_on_each_real_main_raster_without_extrapolation(self):
        left = log_points(40, 12000, 160, -10)
        right = log_points(50, 10000, 155, -11)
        refs = references(left)
        refs["right"]["points"] = right
        result = self.analyze(refs=refs)
        self.assertEqual(result["common_support_hz"], [50.0, 10000.0])
        for side in ("left", "right"):
            aligned = result["sides"][side]["aligned_points"]
            source_frequencies = {round(point[0], 9) for point in refs[side]["points"]}
            self.assertTrue(all(round(point[0], 9) in source_frequencies for point in aligned))
            self.assertTrue(all(120 <= point[0] <= 8000 for point in aligned))

    def test_missing_side_and_insufficient_band_are_explicitly_unavailable(self):
        missing = references()
        missing["right"]["points"] = []
        self.assertIn("right", self.analyze(refs=missing)["reason"])
        narrow = references(log_points(110, 180, 20))
        result = self.analyze(refs=narrow)
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue("points" in result["reason"] or "octaves" in result["reason"])

    def test_main_highpass_does_not_narrow_broadband_level_reference(self):
        result = self.analyze(hp=False)
        self.assertEqual(result["usable_band_hz"], [120.0, 8000.0])

    def test_target_shape_and_local_bass_excursion_do_not_bias_absolute_height(self):
        targets = [
            {"key": "neutral", "label": "Neutral", "provenance": "built_in", "points": [[20, 0], [20000, 0]]},
            {"key": "bk", "label": "BK", "provenance": "built_in", "points": [[20, 2], [100, 1.5], [1000, 0], [20000, -3.5]]},
            {"key": "harman", "label": "Harman", "provenance": "built_in", "points": [[20, 5], [120, 2], [1000, 0], [20000, -5]]},
            {"key": "house:test", "label": "Custom", "provenance": "uploaded", "points": [[20, 7], [70, 4], [700, 1], [7000, -2], [20000, -4]]},
        ]
        frequencies = [point[0] for point in log_points()]
        for target in targets:
            target_values = dict(autosub._auto_sub_log_interpolate_points(target["points"], frequencies))
            measured = [
                [frequency, target_values[frequency] - 24.0 + (12.0 if 130 <= frequency <= 300 else 0.0)]
                for frequency in frequencies
            ]
            with self.subTest(target=target["key"]):
                result = self.analyze(refs=references(measured), target=target)
                self.assertEqual(result["status"], "ready")
                self.assertAlmostEqual(result["target_vertical_offset_db"], -24.0, places=3)

    def test_snapshots_and_display_offset_are_not_mutated(self):
        target = copy.deepcopy(self.target)
        refs = references()
        target_before, refs_before = copy.deepcopy(target), copy.deepcopy(refs)
        first = self.analyze(refs=refs, target=target)
        autosub._auto_sub_shared_bass_offset({"points": [[20, -1], [80, 1]]})
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
        autosub._finalize_autosub_job(job, "test-job")
        anchor["sides"]["left"]["aligned_points"][0][1] = 999
        self.assertNotEqual(job["result"]["main_target_anchor"]["sides"]["left"]["aligned_points"][0][1], 999)


if __name__ == "__main__":
    unittest.main()
