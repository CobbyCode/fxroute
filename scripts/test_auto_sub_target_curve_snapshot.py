#!/usr/bin/env python3
"""Focused tests for immutable AutoSub Target-Curve transport (no Gain logic)."""

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import main


class AutoSubTargetCurveSnapshotTests(unittest.TestCase):
    def validate(self, payload):
        return main._validate_auto_sub_target_curve_snapshot(json.dumps(payload))

    def test_builtin_curve_is_complete_and_detached(self):
        payload = {"key": "bass_shelf", "label": "Bass Shelf", "provenance": "built_in", "points": [[20, 4], [200, 0], [20000, 0]]}
        snapshot, error = self.validate(payload)
        self.assertIsNone(error)
        self.assertEqual(snapshot, {**payload, "points": [[20.0, 4.0], [200.0, 0.0], [20000.0, 0.0]]})
        payload["points"][0][1] = 99
        self.assertEqual(snapshot["points"][0][1], 4.0)

    def test_uploaded_house_curve_is_complete(self):
        payload = {"key": "house:abc", "label": "room-target.txt", "provenance": "uploaded", "points": [[18.5, 6.25], [80, 2], [1000, -1.5]]}
        snapshot, error = self.validate(payload)
        self.assertIsNone(error)
        self.assertEqual(snapshot["key"], "house:abc")
        self.assertEqual(snapshot["label"], "room-target.txt")
        self.assertEqual(snapshot["points"], payload["points"])

    def test_running_job_snapshot_does_not_follow_later_selection(self):
        selected = {"key": "neutral", "label": "Neutral", "provenance": "built_in", "points": [[20, 0], [20000, 0]]}
        job_snapshot, error = self.validate(selected)
        self.assertIsNone(error)
        selected.update({"key": "harman", "label": "Harman-style"})
        selected["points"][:] = [[20, 5], [20000, -5]]
        self.assertEqual(job_snapshot["key"], "neutral")
        self.assertEqual(job_snapshot["points"], [[20.0, 0.0], [20000.0, 0.0]])

    def test_invalid_or_missing_points_are_rejected_without_neutral(self):
        invalid = [
            "",
            json.dumps({"key": "x", "label": "X", "provenance": "built_in", "points": [[20, 0]]}),
            json.dumps({"key": "x", "label": "X", "provenance": "built_in", "points": [[20, 0], [20, 1]]}),
            json.dumps({"key": "x", "label": "X", "provenance": "built_in", "points": [[20, 0], [-30, 1]]}),
            '{"key":"x","label":"X","provenance":"built_in","points":[[20,0],[30,NaN]]}',
        ]
        for raw in invalid:
            with self.subTest(raw=raw):
                snapshot, error = main._validate_auto_sub_target_curve_snapshot(raw)
                self.assertIsNone(snapshot)
                self.assertTrue(error)
                self.assertNotEqual(snapshot, {"key": "neutral", "points": [[20, 0], [20000, 0]]})

    def test_display_offset_cannot_mutate_target_or_raw_reference_points(self):
        target, error = self.validate({"key": "neutral", "label": "Neutral", "provenance": "built_in", "points": [[20, 0], [20000, 0]]})
        self.assertIsNone(error)
        raw = {"points_left": [[20, -10], [100, -5], [300, -2]], "points_right": [[20, -8], [100, -4], [300, -1]]}
        raw_before = copy.deepcopy(raw)
        target_before = copy.deepcopy(target)
        main._auto_sub_measurement_from_sweep(raw, "Before", "AutoSub Before")
        self.assertEqual(raw, raw_before)
        self.assertEqual(target, target_before)

    def test_frontend_resolves_exact_option_and_sends_full_snapshot(self):
        source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("find((option) => option.key === key)", source)
        self.assertIn("provenance: key.startsWith('house:') ? 'uploaded' : 'built_in'", source)
        self.assertIn("JSON.stringify(targetCurveSnapshot)", source)
        self.assertNotIn("function getAutoSubTargetCurveSnapshot() {\n    const conv = ensureMeasurementConvolverState();\n    const curve = getMeasurementConvolverCurve", source)


if __name__ == "__main__":
    unittest.main()
