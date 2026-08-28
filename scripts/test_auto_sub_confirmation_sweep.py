#!/usr/bin/env python3
"""AutoSub confirmation measurement contract for all supported modes.

Regression coverage: the After/Confirmation measurement must be built from
the final measured sweeps (gain verification or polarity-refined winner) in
every runner, independent of the gain verdict. Selecting the confirmation by
verdict alone dropped the After curve whenever a polarity refinement moved
the winner pair outside the scanned delay pools (observed as a graph with
only the Before/Baseline curves).
"""
import inspect
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import measurement.autosub as autosub


class AutoSubConfirmationSweepTests(unittest.TestCase):
    def test_21_confirmation_prefers_final_measured_sweep(self):
        source = inspect.getsource(autosub._run_auto_sub_optimize)
        self.assertIn(
            "_points_sweep(final_gain_sweep) or _points_sweep(",
            source,
            "2.1 confirmation must take the final measured sweep regardless of the gain verdict",
        )
        self.assertIn(
            "_auto_sub_result_for_delay(all_sweep_results, confirm_delay)",
            source,
            "2.1 confirmation must keep the scan-sweep fallback at the confirmed delay",
        )
        # The verdict-gated selection dropped the After measurement whenever
        # the gain verdict was rejected and the winner delay left the pool.
        self.assertNotIn(
            'final_gain_sweep if gain_verdict["accepted"] else',
            source,
            "2.1 confirmation must not be gated on the gain verdict",
        )

    def test_22_confirmation_prefers_final_measured_sweep(self):
        source = inspect.getsource(autosub._run_auto_sub_22_optimize)
        self.assertIn(
            "confirm_22_sweep = final_gain_sweep",
            source,
            "2.2 confirmation must take the final measured sweep regardless of the gain verdict",
        )
        self.assertIn(
            "round(float(best_sub1), 2)",
            source,
            "2.2 confirmation must keep the winner-pair scan fallback",
        )
        self.assertNotIn(
            'final_gain_sweep if gain_verdict["accepted"] else',
            source,
            "2.2 confirmation must not be gated on the gain verdict",
        )

    def test_22_confirmation_caption_uses_selected_sweep_pair(self):
        source = inspect.getsource(autosub._run_auto_sub_22_optimize)
        self.assertIn(
            'confirm_22_sweep.get("sub1_alignment_ms", best_sub1)',
            source,
            "2.2 confirmation caption must describe the sweep that is actually shown",
        )

    def test_22_stereo_confirmation_prefers_final_measured_sweeps(self):
        source = inspect.getsource(autosub._run_auto_sub_22_stereo_optimize)
        self.assertIn(
            "_points_sweep(final_gain_left) or _auto_sub_result_for_delay(all_left_sweeps, best_left)",
            source,
            "2.2 Stereo left confirmation must take the final measured sweep regardless of the step-1 verdict",
        )
        self.assertIn(
            "_points_sweep(final_gain_right) or _auto_sub_result_for_delay(all_right_sweeps, best_right)",
            source,
            "2.2 Stereo right confirmation must take the final measured sweep regardless of the step-1 verdict",
        )
        self.assertNotIn(
            'final_gain_left if accepted_step1_sides["left"] else',
            source,
            "2.2 Stereo confirmation must not be gated on the per-side step-1 verdict",
        )

    def test_result_payload_keeps_baseline_and_confirmation(self):
        for runner in (
            autosub._run_auto_sub_optimize,
            autosub._run_auto_sub_22_optimize,
            autosub._run_auto_sub_22_stereo_optimize,
        ):
            source = inspect.getsource(runner)
            self.assertIn('"baseline_measurement": baseline_measurement', source)
            self.assertIn('"confirmation_measurement": confirmation_measurement', source)


if __name__ == "__main__":
    unittest.main()
