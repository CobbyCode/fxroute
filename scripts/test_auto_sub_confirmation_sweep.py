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
            "left_confirm = _points_sweep(final_gain_left)",
            source,
            "2.2 Stereo left confirmation must take the final measured sweep regardless of the step-1 verdict",
        )
        self.assertIn(
            "right_confirm = _points_sweep(final_gain_right)",
            source,
            "2.2 Stereo right confirmation must take the final measured sweep regardless of the step-1 verdict",
        )
        self.assertIn(
            "if not exact_confirmation_required:",
            source,
            "2.2 Stereo may use scan fallbacks only when an exact final-state capture is not required",
        )
        self.assertNotIn(
            'final_gain_left if accepted_step1_sides["left"] else',
            source,
            "2.2 Stereo confirmation must not be gated on the per-side step-1 verdict",
        )

    def test_21_confirmation_gate_syncs_gain_diagnostics(self):
        """The gain diagnostics must reflect the state the gate committed.

        Post-mortem of run b3bdd634dbfe read ``final_level_db: 6.13,
        applied: true`` from the persisted snapshot while the confirmation
        gate had restored the original state (0.0 dB). Both gate-failure
        branches must refresh ``job["auto_gain"]`` like the 2.2 runners do.
        """
        source = inspect.getsource(autosub._run_auto_sub_optimize)
        self.assertIn(
            '"final_level_db": balanced_level',
            source,
            "gate-kept balance must refresh the gain diagnostics final level",
        )
        self.assertIn(
            '"final_level_db": original_level',
            source,
            "gate revert must refresh the gain diagnostics final level",
        )
        self.assertIn(
            '"final_deltas_db": {"left": 0.0, "right": 0.0}',
            source,
            "gate revert must zero the gain diagnostics deltas",
        )

    def test_21_confirmation_recheck_uses_paired_decision(self):
        source = inspect.getsource(autosub._run_auto_sub_optimize)
        self.assertIn(
            "_auto_sub_local_dip_recheck_decision(",
            source,
            "2.1 must compare the final and incumbent recheck before falling back",
        )
        self.assertIn(
            'recheck_outcome == "final_kept"',
            source,
            "a non-reproduced dip regression must recommit the scored final state",
        )
        self.assertIn(
            'overview.get("mode") == OUTPUT_MODE_SUBWOOFER_21',
            source,
            "the authoritative winner recommit must verify the complete 2.1 mode state",
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
