#!/usr/bin/env python3
"""Regression tests for the 2.1 fine-winner apply path.

Reproduces the reported run: the fine scan found a measurably better
alignment (fine -3.71 ms / 67.7%) but the run stayed at 0.00 ms because
the confidence/margin pre-gate blocked the already-accepted fine winner
before the real confirmation gate could run.

A fine winner that beat both the coarse winner and the incumbent on the
same combined scoring basis must be routed into the real confirmation
gate, not blocked a second time by the confidence/margin pre-gate.
"""

import copy
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import audio.samplerate as samplerate
import measurement.autosub as autosub
from measurement.autosub.runners import optimize as runner


def make_score_fn(scores_by_delay, default=0.1):
    """Deterministic stand-in for the combined candidate scorer."""

    def score(rows, **_kwargs):
        results = []
        for row in rows:
            delay = round(float(row.get("delay_ms", 0.0)), 2)
            value = scores_by_delay.get(delay, default)
            results.append({
                **row,
                "score": value,
                "final_score": value,
                "score_pct": value * 100.0,
                "xo_score": value,
                "timing_band_score": value,
                "low_guard_loss_db": 0.0,
                "low_guard_penalty": 0.0,
                "score_L": value,
                "score_R": value,
                "score_L_pct": value * 100.0,
                "score_R_pct": value * 100.0,
            })
        results.sort(key=lambda item: item["score"], reverse=True)
        return {
            "winner": results[0],
            "runner_up": results[1] if len(results) > 1 else None,
            "results": results,
            "scored_candidates": rows,
            "confidence": autosub._auto_sub_scoring_confidence(results),
        }

    return score


class AutoSubFineWinnerApplyTests(unittest.IsolatedAsyncioTestCase):
    async def _run_case(
        self,
        *,
        scores_by_delay,
        fine_delays,
        fine_triggered,
        scan_delays=(0.0, -3.12),
    ):
        job_id = "auto-sub-fine-winner-apply"
        original = {
            "mode": runner.OUTPUT_MODE_SUBWOOFER_21,
            "subwoofer": {
                "crossover_frequency_hz": 80,
                "main_highpass_enabled": True,
                "sub_alignment_ms": 0.0,
                "sub_level_db": 0.0,
                "sub_polarity": "normal",
            },
        }
        state = copy.deepcopy(original)
        frequencies = [20.0, 25.0, 31.5, 40.0, 50.0, 63.0, 80.0, 100.0,
                       125.0, 160.0, 200.0, 250.0, 315.0, 400.0, 500.0, 640.0]
        points = [[frequency, 0.0] for frequency in frequencies]
        job = {
            "id": job_id,
            "mode": runner.OUTPUT_MODE_SUBWOOFER_21,
            "status": "preparing",
            "cancel_requested": False,
            "target_curve": {"label": "Neutral", "points": points},
            "main_target_anchor": {"status": "ready", "target_vertical_offset_db": 0.0},
        }
        runner._AUTO_SUB_JOBS[job_id] = job

        def overview():
            return copy.deepcopy(state)

        def persist(output_mode, global_config, _subwoofers_config=None):
            state.clear()
            state.update({
                "mode": output_mode,
                "subwoofer": {
                    "crossover_frequency_hz": global_config.get("crossover_frequency_hz", 80),
                    "main_highpass_enabled": global_config.get("main_highpass_enabled", True),
                    "sub_alignment_ms": float(global_config.get("sub_alignment_ms", 0.0)),
                    "sub_level_db": float(global_config.get("sub_level_db", 0.0)),
                    "sub_polarity": str(global_config.get("sub_polarity", "normal")),
                },
            })
            return overview()

        async def apply_candidate(*, output_mode, global_config, subwoofers_config, verify, load_overview=None):
            persist(output_mode, global_config, subwoofers_config)
            return bool(verify((load_overview or overview)()))

        async def measure_candidate(**kwargs):
            persist(kwargs.get("output_mode", runner.OUTPUT_MODE_SUBWOOFER_21), {
                "crossover_frequency_hz": kwargs["fc"],
                "main_highpass_enabled": kwargs["original_highpass"],
                "sub_alignment_ms": kwargs["delay_ms"],
                "sub_level_db": kwargs["original_level"],
                "sub_polarity": kwargs["original_polarity"],
            })
            return {
                "delay_ms": float(kwargs["delay_ms"]),
                "name": f'{kwargs["delay_ms"]:.2f}',
                "status": "completed",
                "status_left": "completed",
                "status_right": "completed",
                "scan": kwargs["stage"],
                "points": copy.deepcopy(points),
                "points_left": copy.deepcopy(points),
                "points_right": copy.deepcopy(points),
                "calibrated_points_left": copy.deepcopy(points),
                "calibrated_points_right": copy.deepcopy(points),
                "normalized_by_db_left": 0.0,
                "normalized_by_db_right": 0.0,
                "combined_candidate": True,
                "stage_output_peaks": {"stage": kwargs["stage"]},
            }

        gain_diagnostics = {
            "available": True,
            "gain_calculated": True,
            "confidence": "high",
            "recommendation": {"raw_delta_db": 0.0},
            "channels": {
                "left": {"raw_recommendation_db": 0.0, "target_delta_db": 0.0, "chain_anchor_db": 0.0},
                "right": {"raw_recommendation_db": 0.0, "target_delta_db": 0.0, "chain_anchor_db": 0.0},
            },
        }

        async def finish_worker(_job, _job_id):
            return None

        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(runner, "_measurement_session", return_value=None))
                stack.enter_context(patch.object(runner, "_dsp_runtime", return_value=None))
                stack.enter_context(patch("measurement.autosub.candidates._dsp_runtime", return_value=None))
                stack.enter_context(patch.object(runner, "_capture_auto_sub_main_references", new=AsyncMock()))
                stack.enter_context(patch.object(runner, "_measure_auto_sub_combined_candidate", side_effect=measure_candidate))
                stack.enter_context(patch.object(runner, "_auto_sub_gate_candidate_rows", side_effect=lambda rows, *_a, **_k: (rows, [])))
                stack.enter_context(patch.object(runner, "_score_auto_sub_combined_candidates", side_effect=make_score_fn(scores_by_delay)))
                stack.enter_context(patch.object(
                    runner, "_auto_sub_fine_trigger_reasons",
                    return_value=["coarse_winner_at_scan_edge"] if fine_triggered else [],
                ))
                stack.enter_context(patch.object(runner, "_auto_sub_fine_delay_candidates", return_value=list(fine_delays)))
                stack.enter_context(patch.object(runner, "_auto_sub_polarity_decision", return_value={
                    "accepted": False, "score_gain": -0.1, "min_score_gain": 0.03,
                    "reason": "incumbent_protected_unclear_advantage",
                }))
                stack.enter_context(patch.object(runner, "_calculate_auto_sub_gain", return_value=copy.deepcopy(gain_diagnostics)))
                stack.enter_context(patch.object(runner, "_auto_sub_gain_deltas", return_value={}))
                stack.enter_context(patch.object(runner, "_auto_sub_gain_verdict", return_value={
                    "accepted": True, "reason": "accepted", "channels": {},
                }))
                # Before dips (10.0) deeper than final dips (5.0): the real
                # confirmation gate passes without a recheck.
                stack.enter_context(patch.object(runner, "_auto_sub_local_dip_db", side_effect=[10.0, 10.0, 5.0, 5.0]))
                stack.enter_context(patch.object(runner, "_auto_sub_apply_candidate", side_effect=apply_candidate))
                stack.enter_context(patch.object(runner, "_finish_auto_sub_worker", side_effect=finish_worker))
                stack.enter_context(patch.object(runner, "get_audio_output_overview", side_effect=overview))
                stack.enter_context(patch.object(samplerate, "set_audio_output_mode", side_effect=persist))
                stack.enter_context(patch.object(samplerate, "_load_audio_output_mode", side_effect=overview))
                stack.enter_context(patch("measurement.session._resolve_measurement_start_sample_rate", return_value=48000))

                await runner._run_auto_sub_optimize(
                    job_id=job_id,
                    input_id="test-input",
                    channel="left",
                    mic_input_channel="1",
                    reference_input_channel="",
                    calibration_ref="",
                    calibration_filename=None,
                    calibration_bytes=None,
                    scan_delays=list(scan_delays),
                    fc=80,
                    current_alignment=0.0,
                    original_polarity="normal",
                    original_level=0.0,
                    original_highpass=True,
                    original_config_snapshot=original,
                )
        finally:
            runner._AUTO_SUB_JOBS.pop(job_id, None)

        return job, state

    async def test_accepted_fine_winner_reaches_confirmation_gate(self):
        """Fine 67.7% beats incumbent 65% and coarse 60% on the combined
        basis; margin 2.7pp and gain 2.7pp would previously trip
        not_applied_close_gain_below_3pp. It must be applied instead."""
        job, state = await self._run_case(
            scores_by_delay={0.0: 0.65, -3.12: 0.60, -3.71: 0.677},
            fine_delays=[-3.71],
            fine_triggered=True,
        )

        self.assertEqual(job["status"], "completed", job.get("error"))
        self.assertTrue(job["result"]["applied"])
        self.assertEqual(job["result"]["apply_decision"], "applied_fine_scan_winner")
        self.assertTrue(job["result"]["fine_accepted"])
        self.assertEqual(job["result"]["applied_alignment_ms"], -3.71)
        self.assertEqual(state["subwoofer"]["sub_alignment_ms"], -3.71)
        self.assertEqual(job["confirmation_gate"]["action"], "final_kept")

    async def test_fine_winner_at_incumbent_delay_stays_not_applied(self):
        """A fine candidate equal to the incumbent delay is a no-op change;
        the incumbent branch must still win."""
        job, state = await self._run_case(
            scores_by_delay={0.0: 0.677, -3.12: 0.60},
            fine_delays=[0.0],
            fine_triggered=True,
        )

        self.assertEqual(job["status"], "completed", job.get("error"))
        self.assertFalse(job["result"]["applied"])
        self.assertEqual(job["result"]["apply_decision"], "not_applied_incumbent_better")
        self.assertEqual(job["result"]["applied_alignment_ms"], 0.0)

    async def test_coarse_only_close_winner_still_blocked_by_pre_gate(self):
        """Without a fine scan the confidence/margin pre-gate keeps its old
        role: a coarse winner at 68% over an incumbent at 66% (gain 2pp,
        under the 10pp uncertain threshold) stays suggested-only via
        not_applied_uncertain_confidence. Only the fine-scan endorsement
        bypasses this pre-gate."""
        job, state = await self._run_case(
            scores_by_delay={0.0: 0.66, -1.56: 0.62, -3.12: 0.68},
            fine_delays=[],
            fine_triggered=False,
            scan_delays=(0.0, -1.56, -3.12),
        )

        self.assertEqual(job["status"], "completed", job.get("error"))
        self.assertFalse(job["result"]["applied"])
        self.assertEqual(job["result"]["apply_decision"], "not_applied_uncertain_confidence")
        self.assertEqual(job["result"]["applied_alignment_ms"], 0.0)
        self.assertEqual(state["subwoofer"]["sub_alignment_ms"], 0.0)


if __name__ == "__main__":
    unittest.main()
