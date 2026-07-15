#!/usr/bin/env python3
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.modules.setdefault("multipart", types.SimpleNamespace(__version__="0", multipart=types.SimpleNamespace(parse_options_header=lambda value: (value, {}))))
import main


def diagnostic(left, right, calculated=True):
    return {
        "gain_calculated": calculated,
        "recommendation": {"delta_db": (left + right) / 2, "left_delta_db": left, "right_delta_db": right},
        "channels": {
            "left": {"raw_recommendation_db": left, "target_delta_db": left},
            "right": {"raw_recommendation_db": right, "target_delta_db": right},
        },
    }


class AutoGainApplyRevertTests(unittest.TestCase):
    def test_21_and_22_mono_use_same_common_delta(self):
        source = diagnostic(2.0, 4.0)
        self.assertEqual(main._auto_sub_gain_deltas(source, main.OUTPUT_MODE_SUBWOOFER_21), {"left": 2.0, "right": 2.0})
        self.assertEqual(main._auto_sub_gain_deltas(source, main.OUTPUT_MODE_SUBWOOFER_22), {"left": 2.0, "right": 2.0})

    def test_22_stereo_preserves_separate_deltas(self):
        self.assertEqual(
            main._auto_sub_gain_deltas(diagnostic(2.0, -1.0), main.OUTPUT_MODE_SUBWOOFER_22_STEREO),
            {"left": 2.0, "right": -1.0},
        )

    def test_second_feedback_step_is_limited_to_one_db(self):
        self.assertEqual(
            main._auto_sub_gain_deltas(diagnostic(-9.0, -7.0), main.OUTPUT_MODE_SUBWOOFER_21, max_abs_db=1.0),
            {"left": -1.0, "right": -1.0},
        )

    def test_22_snapshot_applies_equal_delta_and_preserves_relative_gain(self):
        snapshot = {"subwoofers": {"sub1": {"level_db": -5.0}, "sub2": {"level_db": -2.0}}}
        updated = main._auto_sub_22_snapshot_with_gain(snapshot, left_delta_db=3.0, right_delta_db=3.0)
        self.assertEqual(updated["subwoofers"]["sub1"]["level_db"], -2.0)
        self.assertEqual(updated["subwoofers"]["sub2"]["level_db"], 1.0)
        self.assertEqual(updated["subwoofers"]["sub2"]["level_db"] - updated["subwoofers"]["sub1"]["level_db"], 3.0)
        self.assertEqual(snapshot["subwoofers"]["sub1"]["level_db"], -5.0)

    def test_verification_accepts_improvement(self):
        verdict = main._auto_sub_gain_verdict(diagnostic(3.0, -2.0), diagnostic(0.4, -0.2), main.OUTPUT_MODE_SUBWOOFER_21)
        self.assertTrue(verdict["accepted"])

    def test_verification_reverts_when_either_channel_is_worse(self):
        verdict = main._auto_sub_gain_verdict(diagnostic(1.0, 1.0), diagnostic(0.2, 1.4), main.OUTPUT_MODE_SUBWOOFER_22_STEREO)
        self.assertFalse(verdict["accepted"])
        self.assertFalse(verdict["channels"]["right"]["accepted"])

    def test_verification_tolerates_quarter_db_measurement_noise(self):
        verdict = main._auto_sub_gain_verdict(diagnostic(1.0, 1.0), diagnostic(1.24, 1.25), main.OUTPUT_MODE_SUBWOOFER_21)
        self.assertTrue(verdict["accepted"])

    def test_unavailable_diagnostics_never_apply(self):
        self.assertEqual(main._auto_sub_gain_deltas(diagnostic(1, 1, calculated=False), main.OUTPUT_MODE_SUBWOOFER_21), {})
        verdict = main._auto_sub_gain_verdict(diagnostic(1, 1, calculated=False), diagnostic(0, 0), main.OUTPUT_MODE_SUBWOOFER_21)
        self.assertFalse(verdict["accepted"])

    def test_response_correction_uses_measured_sensitivity(self):
        correction = main._auto_sub_gain_response_correction(
            diagnostic(-2.0, -2.4), diagnostic(-1.0, -1.2),
            {"left": -2.0, "right": -2.0}, main.OUTPUT_MODE_SUBWOOFER_21,
        )
        self.assertTrue(correction["available"])
        self.assertAlmostEqual(correction["channels"]["left"]["response_change_per_db"], 0.5)
        self.assertEqual(correction["deltas_db"], {"left": -2.0, "right": -2.0})

    def test_response_correction_rejects_wrong_direction(self):
        correction = main._auto_sub_gain_response_correction(
            diagnostic(-2.0, -2.0), diagnostic(-3.0, -3.0),
            {"left": -2.0, "right": -2.0}, main.OUTPUT_MODE_SUBWOOFER_21,
        )
        self.assertFalse(correction["available"])
        self.assertIn("implausible", correction["reason"])


if __name__ == "__main__":
    unittest.main()
