#!/usr/bin/env python3
import sys
import types
import unittest
import inspect
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import main
from dsp.runtime import BassManagementConfig
import measurement.autosub as autosub


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
        self.assertEqual(autosub._auto_sub_gain_deltas(source, main.OUTPUT_MODE_SUBWOOFER_21), {"left": 3.0, "right": 3.0})
        self.assertEqual(autosub._auto_sub_gain_deltas(source, main.OUTPUT_MODE_SUBWOOFER_22), {"left": 3.0, "right": 3.0})

    def test_22_stereo_preserves_separate_deltas(self):
        self.assertEqual(
            autosub._auto_sub_gain_deltas(diagnostic(2.0, -1.0), main.OUTPUT_MODE_SUBWOOFER_22_STEREO),
            {"left": 2.0, "right": -1.0},
        )

    def test_second_feedback_step_is_limited_to_one_db(self):
        self.assertEqual(
            autosub._auto_sub_gain_deltas(diagnostic(-9.0, -7.0), main.OUTPUT_MODE_SUBWOOFER_21, max_abs_db=1.0),
            {"left": -1.0, "right": -1.0},
        )

    def test_22_snapshot_applies_equal_delta_and_preserves_relative_gain(self):
        snapshot = {"subwoofers": {"sub1": {"level_db": -5.0}, "sub2": {"level_db": -2.0}}}
        updated = autosub._auto_sub_22_snapshot_with_gain(snapshot, left_delta_db=3.0, right_delta_db=3.0)
        self.assertEqual(updated["subwoofers"]["sub1"]["level_db"], -2.0)
        self.assertEqual(updated["subwoofers"]["sub2"]["level_db"], 1.0)
        self.assertEqual(updated["subwoofers"]["sub2"]["level_db"] - updated["subwoofers"]["sub1"]["level_db"], 3.0)
        self.assertEqual(snapshot["subwoofers"]["sub1"]["level_db"], -5.0)

    def test_verification_accepts_improvement(self):
        verdict = autosub._auto_sub_gain_verdict(diagnostic(3.0, -2.0), diagnostic(0.4, -0.2), main.OUTPUT_MODE_SUBWOOFER_21)
        self.assertTrue(verdict["accepted"])

    def test_verification_reverts_when_either_channel_is_worse(self):
        verdict = autosub._auto_sub_gain_verdict(diagnostic(1.0, 1.0), diagnostic(0.2, 1.4), main.OUTPUT_MODE_SUBWOOFER_22_STEREO)
        self.assertFalse(verdict["accepted"])
        self.assertFalse(verdict["channels"]["right"]["accepted"])

    def test_verification_tolerates_quarter_db_measurement_noise(self):
        verdict = autosub._auto_sub_gain_verdict(diagnostic(1.0, 1.0), diagnostic(1.24, 1.25), main.OUTPUT_MODE_SUBWOOFER_21)
        self.assertTrue(verdict["accepted"])

    def test_unavailable_diagnostics_never_apply(self):
        self.assertEqual(autosub._auto_sub_gain_deltas(diagnostic(1, 1, calculated=False), main.OUTPUT_MODE_SUBWOOFER_21), {})
        verdict = autosub._auto_sub_gain_verdict(diagnostic(1, 1, calculated=False), diagnostic(0, 0), main.OUTPUT_MODE_SUBWOOFER_21)
        self.assertFalse(verdict["accepted"])

    def test_response_correction_uses_measured_sensitivity(self):
        correction = autosub._auto_sub_gain_response_correction(
            diagnostic(-2.0, -2.4), diagnostic(-1.0, -1.2),
            {"left": -2.0, "right": -2.0}, main.OUTPUT_MODE_SUBWOOFER_21,
        )
        self.assertTrue(correction["available"])
        self.assertAlmostEqual(correction["channels"]["left"]["response_change_per_db"], 0.5)
        self.assertEqual(correction["deltas_db"], {"left": -2.0, "right": -2.0})

    def test_response_correction_preserves_sub_two_db_value(self):
        correction = autosub._auto_sub_gain_response_correction(
            diagnostic(-3.704, -3.704), diagnostic(-1.704, -1.704),
            {"left": -2.0, "right": -2.0}, main.OUTPUT_MODE_SUBWOOFER_21,
        )
        self.assertTrue(correction["available"])
        self.assertEqual(correction["raw_deltas_db"], {"left": -1.704, "right": -1.704})
        self.assertEqual(correction["applied_deltas_db"], {"left": -1.704, "right": -1.704})

    def test_response_correction_keeps_total_search_within_six_db(self):
        correction = autosub._auto_sub_gain_response_correction(
            diagnostic(-5.725, -5.725), diagnostic(-3.725, -3.725),
            {"left": -2.0, "right": -2.0}, main.OUTPUT_MODE_SUBWOOFER_22,
        )
        self.assertTrue(correction["available"])
        self.assertEqual(correction["raw_deltas_db"], {"left": -3.725, "right": -3.725})
        self.assertEqual(correction["applied_deltas_db"], {"left": -3.725, "right": -3.725})
        self.assertEqual(correction["deltas_db"], {"left": -3.725, "right": -3.725})

    def test_default_first_step_supports_full_six_db_range(self):
        self.assertEqual(
            autosub._auto_sub_gain_deltas(diagnostic(9.0, 9.0), main.OUTPUT_MODE_SUBWOOFER_21),
            {"left": 6.0, "right": 6.0},
        )

    def test_stage_peak_prediction_blocks_unsafe_plus_six_candidate(self):
        profile = {
            "sweep_start_hz": 20.0, "sweep_end_hz": 600.0,
            "sweep_seconds": 0.1, "tail_seconds": 0.1,
        }
        safe_config = BassManagementConfig(
            output_mode=main.OUTPUT_MODE_SUBWOOFER_21, output_key="test", output_label="test",
            output_channels=4, sample_rate=48000, crossover_frequency_hz=80,
            main_highpass_enabled=True, sub_level_db=0.0, sub_alignment_ms=2.0,
            sub_polarity="normal",
        )
        unsafe_config = BassManagementConfig(
            **{**safe_config.__dict__, "sub_level_db": 6.0, "sub2_level_db": 6.0},
        )
        safe = autosub._auto_sub_stage_peak_prediction(
            sweep_profile=profile, sample_rate=48000, channel="stereo", config=safe_config,
        )
        unsafe = autosub._auto_sub_stage_peak_prediction(
            sweep_profile=profile, sample_rate=48000, channel="stereo", config=unsafe_config,
        )
        self.assertTrue(safe["safe"])
        self.assertFalse(unsafe["safe"])
        self.assertGreater(unsafe["dbfs"]["output_3"], 0.0)
        self.assertEqual(safe["sink_gain"], 1.0)

    def test_stage_peak_prediction_folds_sink_gain_into_all_outputs(self):
        profile = {
            "sweep_start_hz": 20.0, "sweep_end_hz": 600.0,
            "sweep_seconds": 0.1, "tail_seconds": 0.1,
        }
        config = BassManagementConfig(
            output_mode=main.OUTPUT_MODE_SUBWOOFER_22_STEREO, output_key="test", output_label="test",
            output_channels=4, sample_rate=48000, crossover_frequency_hz=80,
            main_highpass_enabled=True, sub_level_db=0.0, sub_alignment_ms=0.0,
            sub_polarity="normal",
        )
        full = autosub._auto_sub_stage_peak_prediction(
            sweep_profile=profile, sample_rate=48000, channel="stereo", config=config,
        )
        quiet = autosub._auto_sub_stage_peak_prediction(
            sweep_profile=profile, sample_rate=48000, channel="stereo", config=config,
            sink_gain=0.31,
        )
        self.assertEqual(quiet["sink_gain"], 0.31)
        for key, value in full["linear"].items():
            self.assertAlmostEqual(quiet["linear"][key], value * 0.31, places=12)
        self.assertAlmostEqual(quiet["maximum_dbfs"], full["maximum_dbfs"] + 20.0 * math.log10(0.31), places=3)

    def test_stage_peak_prediction_22_stereo_bass_plus_two_allowed_with_reduced_sink(self):
        profile = {
            "sweep_start_hz": 20.0, "sweep_end_hz": 600.0,
            "sweep_seconds": 0.1, "tail_seconds": 0.1,
        }
        config = BassManagementConfig(
            output_mode=main.OUTPUT_MODE_SUBWOOFER_22_STEREO, output_key="test", output_label="test",
            output_channels=4, sample_rate=48000, crossover_frequency_hz=80,
            main_highpass_enabled=True, sub_level_db=2.0, sub_alignment_ms=0.0,
            sub_polarity="normal",
        )
        # The stereo-bass routing feeds the full channel amplitude to the sub.
        # At reduced master volume the +2 dB candidate is safely below the
        # DAC full-scale (the user scenario: loud listening volume turned down).
        at_31 = autosub._auto_sub_stage_peak_prediction(
            sweep_profile=profile, sample_rate=48000, channel="left", config=config,
            sink_gain=0.31,
        )
        self.assertTrue(at_31["safe"])
        self.assertLess(at_31["maximum_dbfs"], 0.0)
        # The same candidate must also be safe at full master volume: the
        # LR24 lowpass peak of the short sweep stays under full-scale.
        at_full = autosub._auto_sub_stage_peak_prediction(
            sweep_profile=profile, sample_rate=48000, channel="left", config=config,
        )
        self.assertTrue(at_full["safe"])

    def test_sink_gain_from_master_percent_is_cubic(self):
        # Measured on the .104 PipeWire ALSA sink (UMC204HD, pipewire 1.6.8):
        # the volume transfer curve is the PA cubic (percent/100)**3, e.g.
        # 31% -> -30.5 dB and 10% -> -60.0 dB, not the linear percent/100.
        self.assertEqual(autosub.auto_sub_sink_gain_from_master_percent(100), 1.0)
        self.assertEqual(autosub.auto_sub_sink_gain_from_master_percent(0), 0.0)
        self.assertAlmostEqual(autosub.auto_sub_sink_gain_from_master_percent(31), 0.31 ** 3, places=12)
        self.assertAlmostEqual(autosub.auto_sub_sink_gain_from_master_percent(10), 0.10 ** 3, places=12)
        self.assertAlmostEqual(
            20.0 * math.log10(autosub.auto_sub_sink_gain_from_master_percent(31)), -30.5, places=1)
        self.assertAlmostEqual(
            20.0 * math.log10(autosub.auto_sub_sink_gain_from_master_percent(10)), -60.0, places=1)
        self.assertEqual(autosub.auto_sub_sink_gain_from_master_percent(-5), 0.0)
        self.assertEqual(autosub.auto_sub_sink_gain_from_master_percent(150), 1.0)

    def test_stage_peak_prediction_22_stereo_bass_plus_six_blocked_even_reduced_sink(self):
        profile = {
            "sweep_start_hz": 20.0, "sweep_end_hz": 600.0,
            "sweep_seconds": 0.1, "tail_seconds": 0.1,
        }
        config = BassManagementConfig(
            output_mode=main.OUTPUT_MODE_SUBWOOFER_22_STEREO, output_key="test", output_label="test",
            output_channels=4, sample_rate=48000, crossover_frequency_hz=80,
            main_highpass_enabled=True, sub_level_db=6.0, sub_alignment_ms=0.0,
            sub_polarity="normal",
        )
        # At reduced master the real cubic sink gain leaves plenty of
        # headroom (31% -> 0.31**3 = 0.0298): +6 dB stays far below 0 dBFS.
        moderate = autosub._auto_sub_stage_peak_prediction(
            sweep_profile=profile, sample_rate=48000, channel="left", config=config,
            sink_gain=autosub.auto_sub_sink_gain_from_master_percent(31),
        )
        self.assertTrue(moderate["safe"])
        # At full master volume the same +6 dB candidate genuinely clips the
        # float→integer conversion and must stay blocked.
        full = autosub._auto_sub_stage_peak_prediction(
            sweep_profile=profile, sample_rate=48000, channel="left", config=config,
        )
        self.assertFalse(full["safe"])
        # The same candidate is also unsafe at 90% master (0.9**3 = 0.729):
        # 0.8 * 1.995 * 0.729 = 1.16 exceeds 0 dBFS at the DAC.
        near_full = autosub._auto_sub_stage_peak_prediction(
            sweep_profile=profile, sample_rate=48000, channel="left", config=config,
            sink_gain=autosub.auto_sub_sink_gain_from_master_percent(90),
        )
        self.assertFalse(near_full["safe"])
        # At 70% master (0.7**3 = 0.343) the same candidate is safe:
        # 0.8 * 1.995 * 0.343 = 0.55 (-5.2 dBFS).
        reduced = autosub._auto_sub_stage_peak_prediction(
            sweep_profile=profile, sample_rate=48000, channel="left", config=config,
            sink_gain=autosub.auto_sub_sink_gain_from_master_percent(70),
        )
        self.assertTrue(reduced["safe"])

    def test_stage_peak_comparison_folds_sink_gain_into_measured(self):
        predicted = {
            "dbfs": {"output_1": -12.0, "output_2": -12.0, "output_3": -12.0, "output_4": -12.0},
        }
        measured = {key: 10.0 ** (db / 20.0) for key, db in predicted["dbfs"].items()}
        comparison = autosub._auto_sub_stage_peak_comparison(predicted, measured, sink_gain=0.31)
        self.assertTrue(comparison["measured_safe"])
        for key, db in comparison["measured"]["dbfs"].items():
            self.assertAlmostEqual(db, -12.0 + 20.0 * math.log10(0.31), places=3)

    def test_four_stage_peak_channels_compare_plausibly(self):
        predicted = {
            "dbfs": {"output_1": -3.0, "output_2": -4.0, "output_3": -5.0, "output_4": -6.0},
        }
        measured = {
            key: 10.0 ** (db / 20.0)
            for key, db in predicted["dbfs"].items()
        }
        comparison = autosub._auto_sub_stage_peak_comparison(predicted, measured)
        self.assertEqual(set(comparison["measured"]["dbfs"]), {"output_1", "output_2", "output_3", "output_4"})
        self.assertFalse(comparison["relevant_mismatch"])

    def test_peak_safety_failure_aborts_and_final_result_persists_peaks(self):
        candidate_source = inspect.getsource(autosub._measure_auto_sub_candidate)
        finalize_source = inspect.getsource(autosub._finalize_autosub_job)
        self.assertIn("raise AutoSubPeakSafetyError", candidate_source)
        self.assertIn("except AutoSubPeakSafetyError:", candidate_source)
        self.assertIn('"stage_output_peaks": (final_gain_sweep or {}).get("stage_output_peaks")',
                      inspect.getsource(autosub._run_auto_sub_optimize))
        self.assertIn('job["result"]["auto_gain"]', finalize_source)

    def test_response_correction_still_rejects_values_above_six_db(self):
        correction = autosub._auto_sub_gain_response_correction(
            diagnostic(-9.0, -9.0), diagnostic(-7.0, -7.0),
            {"left": -2.0, "right": -2.0}, main.OUTPUT_MODE_SUBWOOFER_22_STEREO,
        )
        self.assertFalse(correction["available"])
        self.assertEqual(correction["raw_deltas_db"], {"left": -7.0, "right": -7.0})
        self.assertEqual(correction["applied_deltas_db"], {})
        self.assertEqual(correction["reason"], "Measured final Gain correction is implausible")

    def test_response_correction_rejects_wrong_direction(self):
        correction = autosub._auto_sub_gain_response_correction(
            diagnostic(-2.0, -2.0), diagnostic(-3.0, -3.0),
            {"left": -2.0, "right": -2.0}, main.OUTPUT_MODE_SUBWOOFER_21,
        )
        self.assertFalse(correction["available"])
        self.assertIn("implausible", correction["reason"])

    def test_22_stereo_keeps_accepted_step1_when_optional_correction_is_unavailable(self):
        source = inspect.getsource(autosub._run_auto_sub_22_stereo_optimize)
        fallback = source.split('if not correction_plan.get("available"):', 1)[1].split(
            'if any(abs(value) > 0.0005 for value in correction_deltas.values()):', 1
        )[0]
        self.assertIn('"step1_retained": True', fallback)
        self.assertIn("_auto_sub_stereo_probe_plan(", fallback)
        self.assertNotIn('gain_verdict =', fallback)
        self.assertNotIn('set_audio_output_mode(', fallback)

    def test_22_stereo_retains_only_independently_improved_step1_side(self):
        source = inspect.getsource(autosub._run_auto_sub_22_stereo_optimize)
        self.assertIn('accepted_step1_sides = {', source)
        self.assertIn('if accepted_step1_sides[side] else 0.0', source)
        self.assertIn('elif not all(accepted_step1_sides.values()):', source)
        self.assertIn('Retained improved Stereo side; restored regressed side', source)
        self.assertIn('"accepted_step1" if step1_retained else "restored"', source)

    def test_22_stereo_gain_rollback_preserves_selected_polarities(self):
        source = inspect.getsource(autosub._run_auto_sub_22_stereo_optimize)
        rollback = source.split('if not step1_retained:', 1)[1].split(
            'elif not all(accepted_step1_sides.values()):', 1
        )[0]
        self.assertIn('_auto_sub_22_global_config(polarity_snapshot)', rollback)
        self.assertIn('_auto_sub_22_candidate_subwoofers(\n                    polarity_snapshot,', rollback)
        self.assertNotIn('_auto_sub_22_global_config(original_config_snapshot)', rollback)

    def test_22_stereo_probe_requires_broad_third_octave_violation(self):
        target = {"points": self._curve(lambda _index: 0.0)}
        anchor = {"status": "ready", "target_vertical_offset_db": 0.0}
        broad_peak = self._curve(lambda index: 11.0 if 14 <= index <= 30 else 0.0)
        narrow_peak = self._curve(lambda index: 20.0 if index == 24 else 0.0)
        broad = autosub._auto_sub_stereo_corridor_violation(
            points=broad_peak, target_curve=target, anchor=anchor, crossover_hz=80, direction=-1.0,
        )
        narrow = autosub._auto_sub_stereo_corridor_violation(
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
        plan = autosub._auto_sub_stereo_probe_plan(
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
        source = inspect.getsource(autosub._run_auto_sub_22_stereo_optimize)
        self.assertIn("score_better = after_score < before_score", source)
        self.assertIn('float(after_corridor.get("severity_db", 0.0)) < float(before_corridor.get("severity_db", 0.0))', source)
        self.assertIn("correction_deltas.get(side, 0.0) if accepted_probe_sides[side] else 0.0", source)
        self.assertIn("Stereo corridor probe rejected; Step 1 retained", source)

    def test_22_mono_keeps_accepted_step1_when_optional_correction_is_unavailable(self):
        source = inspect.getsource(autosub._run_auto_sub_22_optimize)
        fallback = source.split('if not correction_plan.get("available"):', 1)[1].split(
            'elif abs(correction_delta) > 0.0005:', 1
        )[0]
        self.assertIn('"step1_retained": True', fallback)
        self.assertNotIn('gain_verdict =', fallback)
        self.assertNotIn('set_audio_output_mode(', fallback)

    def test_21_keeps_accepted_step1_when_optional_correction_is_unavailable(self):
        source = inspect.getsource(autosub._run_auto_sub_optimize)
        fallback = source.split('if not correction_plan.get("available"):', 1)[1].split(
            'elif abs(correction_delta) > 0.0005:', 1
        )[0]
        self.assertIn('"step1_retained": True', fallback)
        self.assertNotIn('gain_verdict =', fallback)
        self.assertNotIn('set_audio_output_mode(', fallback)


if __name__ == "__main__":
    unittest.main()
