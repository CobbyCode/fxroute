#!/usr/bin/env python3
import sys
import types
import unittest
import inspect
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
    @staticmethod
    def _curve(value_fn):
        return [[40.0 * (4.0 ** (index / 48.0)), value_fn(index)] for index in range(49)]

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

    def test_response_correction_preserves_sub_two_db_value(self):
        correction = main._auto_sub_gain_response_correction(
            diagnostic(-3.704, -3.704), diagnostic(-1.704, -1.704),
            {"left": -2.0, "right": -2.0}, main.OUTPUT_MODE_SUBWOOFER_21,
        )
        self.assertTrue(correction["available"])
        self.assertEqual(correction["raw_deltas_db"], {"left": -1.704, "right": -1.704})
        self.assertEqual(correction["applied_deltas_db"], {"left": -1.704, "right": -1.704})

    def test_response_correction_limits_plausible_value_to_two_db(self):
        correction = main._auto_sub_gain_response_correction(
            diagnostic(-5.725, -5.725), diagnostic(-3.725, -3.725),
            {"left": -2.0, "right": -2.0}, main.OUTPUT_MODE_SUBWOOFER_22,
        )
        self.assertTrue(correction["available"])
        self.assertEqual(correction["raw_deltas_db"], {"left": -3.725, "right": -3.725})
        self.assertEqual(correction["applied_deltas_db"], {"left": -2.0, "right": -2.0})
        self.assertEqual(correction["deltas_db"], {"left": -2.0, "right": -2.0})

    def test_response_correction_still_rejects_values_above_six_db(self):
        correction = main._auto_sub_gain_response_correction(
            diagnostic(-9.0, -9.0), diagnostic(-7.0, -7.0),
            {"left": -2.0, "right": -2.0}, main.OUTPUT_MODE_SUBWOOFER_22_STEREO,
        )
        self.assertFalse(correction["available"])
        self.assertEqual(correction["raw_deltas_db"], {"left": -7.0, "right": -7.0})
        self.assertEqual(correction["applied_deltas_db"], {})
        self.assertEqual(correction["reason"], "Measured final Gain correction is implausible")

    def test_response_correction_rejects_wrong_direction(self):
        correction = main._auto_sub_gain_response_correction(
            diagnostic(-2.0, -2.0), diagnostic(-3.0, -3.0),
            {"left": -2.0, "right": -2.0}, main.OUTPUT_MODE_SUBWOOFER_21,
        )
        self.assertFalse(correction["available"])
        self.assertIn("implausible", correction["reason"])

    def test_22_stereo_keeps_accepted_step1_when_optional_correction_is_unavailable(self):
        source = inspect.getsource(main._run_auto_sub_22_stereo_optimize)
        fallback = source.split('if not correction_plan.get("available"):', 1)[1].split(
            'if any(abs(value) > 0.0005 for value in correction_deltas.values()):', 1
        )[0]
        self.assertIn('"step1_retained": True', fallback)
        self.assertIn("_auto_sub_stereo_probe_plan(", fallback)
        self.assertNotIn('gain_verdict =', fallback)
        self.assertNotIn('set_audio_output_mode(', fallback)

    def test_22_stereo_retains_only_independently_improved_step1_side(self):
        source = inspect.getsource(main._run_auto_sub_22_stereo_optimize)
        self.assertIn('accepted_step1_sides = {', source)
        self.assertIn('if accepted_step1_sides[side] else 0.0', source)
        self.assertIn('elif not all(accepted_step1_sides.values()):', source)
        self.assertIn('Retained improved Stereo side; restored regressed side', source)
        self.assertIn('"accepted_step1" if step1_retained else "restored"', source)

    def test_22_stereo_probe_requires_broad_third_octave_violation(self):
        target = {"points": self._curve(lambda _index: 0.0)}
        anchor = {"status": "ready", "target_vertical_offset_db": 0.0}
        broad_peak = self._curve(lambda index: 11.0 if 14 <= index <= 30 else 0.0)
        narrow_peak = self._curve(lambda index: 20.0 if index == 24 else 0.0)
        broad = main._auto_sub_stereo_corridor_violation(
            points=broad_peak, target_curve=target, anchor=anchor, crossover_hz=80, direction=-1.0,
        )
        narrow = main._auto_sub_stereo_corridor_violation(
            points=narrow_peak, target_curve=target, anchor=anchor, crossover_hz=80, direction=-1.0,
        )
        self.assertTrue(broad["relevant"])
        self.assertGreater(broad["severity_db"], 0.0)
        self.assertFalse(narrow["relevant"])

    def test_22_stereo_probe_plans_only_eligible_side_at_one_db(self):
        target = {"points": self._curve(lambda _index: 0.0)}
        anchor = {"status": "ready", "target_vertical_offset_db": 0.0}
        broad_peak = self._curve(lambda index: 11.0 if 14 <= index <= 30 else 0.0)
        flat = self._curve(lambda _index: 0.0)
        correction_plan = {
            "available": False, "reason": "Measured final Gain correction is implausible",
            "channels": {
                "left": {"response_change_per_db": 0.3333},
                "right": {"response_change_per_db": 0.628},
            },
        }
        plan = main._auto_sub_stereo_probe_plan(
            correction_plan=correction_plan, gain_after=diagnostic(-0.476, -4.77),
            gain_deltas={"left": -0.714, "right": -2.0},
            accepted_step1_sides={"left": True, "right": True},
            after_points={"left": flat, "right": broad_peak}, target_curve=target,
            anchor=anchor, crossover_hz=80,
        )
        self.assertTrue(plan["available"])
        self.assertEqual(plan["deltas_db"], {"right": -1.0})
        self.assertFalse(plan["channels"]["left"]["eligible"])
        self.assertTrue(plan["channels"]["right"]["eligible"])

    def test_22_stereo_probe_acceptance_is_per_side_and_requires_both_improvements(self):
        source = inspect.getsource(main._run_auto_sub_22_stereo_optimize)
        self.assertIn("score_better = after_score < before_score", source)
        self.assertIn('float(after_corridor.get("severity_db", 0.0)) < float(before_corridor.get("severity_db", 0.0))', source)
        self.assertIn("correction_deltas.get(side, 0.0) if accepted_probe_sides[side] else 0.0", source)
        self.assertIn("Stereo corridor probe rejected; Step 1 retained", source)

    def test_22_mono_keeps_accepted_step1_when_optional_correction_is_unavailable(self):
        source = inspect.getsource(main._run_auto_sub_22_optimize)
        fallback = source.split('if not correction_plan.get("available"):', 1)[1].split(
            'elif abs(correction_delta) > 0.0005:', 1
        )[0]
        self.assertIn('"step1_retained": True', fallback)
        self.assertNotIn('gain_verdict =', fallback)
        self.assertNotIn('set_audio_output_mode(', fallback)

    def test_21_keeps_accepted_step1_when_optional_correction_is_unavailable(self):
        source = inspect.getsource(main._run_auto_sub_optimize)
        fallback = source.split('if not correction_plan.get("available"):', 1)[1].split(
            'elif abs(correction_delta) > 0.0005:', 1
        )[0]
        self.assertIn('"step1_retained": True', fallback)
        self.assertNotIn('gain_verdict =', fallback)
        self.assertNotIn('set_audio_output_mode(', fallback)


if __name__ == "__main__":
    unittest.main()
