#!/usr/bin/env python3
"""Path coverage: 2.2-mono candidate -> derived -> apply -> stored final state.

Covers the bf217b7b318a regression chain:
- the balance-transfer incumbent residual must come from the Both-subs
  matrix incumbent row, never from a single-sub coarse row;
- after a confirmation-gate revert the stored applied_* delays describe
  the incumbent pair while suggested_* keeps the rejected winner.
"""

import asyncio
import copy
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import audio.samplerate as samplerate
from measurement.autosub.runners import optimize_22 as runner


def _points(value_db=0.0):
    freqs = [20.0, 25.0, 31.5, 40.0, 50.0, 63.0, 80.0, 100.0,
             125.0, 160.0, 200.0, 250.0, 315.0, 400.0, 500.0, 640.0]
    return [[f, value_db] for f in freqs]


class AutoSub22MonoFinalPathTests(unittest.IsolatedAsyncioTestCase):
    async def _run_path(self, *, gate_veto, verdict_accepted):
        job_id = "auto-sub-22-mono-path"
        original = {
            "mode": runner.OUTPUT_MODE_SUBWOOFER_22,
            "crossover_frequency_hz": 80,
            "main_highpass_enabled": True,
            "subwoofers": {
                "sub1": {"level_db": 0.0, "alignment_ms": 0.0, "polarity": "normal"},
                "sub2": {"level_db": 0.0, "alignment_ms": 0.0, "polarity": "normal"},
            },
        }
        state = copy.deepcopy(original)
        seen_transfer_incumbent = {}
        job = {
            "id": job_id,
            "mode": runner.OUTPUT_MODE_SUBWOOFER_22,
            "status": "preparing",
            "cancel_requested": False,
            "target_curve": {"label": "Neutral", "points": _points()},
            "main_target_anchor": {"status": "ready", "target_vertical_offset_db": 0.0},
        }
        runner._AUTO_SUB_JOBS[job_id] = job

        def overview():
            return {
                "selected_output": {
                    "key": "test", "label": "Test", "channels": 4, "active_rate": 48000,
                },
                "output_mode": copy.deepcopy(state),
            }

        def persist(output_mode, global_config, subwoofers_config=None):
            state.clear()
            state.update({
                "mode": output_mode,
                "crossover_frequency_hz": global_config.get("crossover_frequency_hz", 80),
                "main_highpass_enabled": global_config.get("main_highpass_enabled", True),
                "subwoofers": copy.deepcopy(subwoofers_config or {}),
            })
            return overview()

        async def apply_candidate(*, output_mode, global_config, subwoofers_config, verify, load_overview=None):
            persist(output_mode, global_config, subwoofers_config)
            return bool(verify((load_overview or (lambda: copy.deepcopy(state)))()))

        async def measure_candidate(**kwargs):
            snapshot = kwargs["original_config_snapshot"]
            subwoofers = runner._auto_sub_22_candidate_subwoofers(
                snapshot,
                sub1_alignment_ms=kwargs.get("sub1_alignment_ms", kwargs["delay_ms"]),
                sub2_alignment_ms=kwargs.get("sub2_alignment_ms", 0.0),
                active_subs=kwargs.get("active_subs", ("sub1", "sub2")),
                sub1_polarity=kwargs.get("sub1_polarity"),
                sub2_polarity=kwargs.get("sub2_polarity"),
            )
            persist(kwargs["output_mode"], runner._auto_sub_22_global_config(snapshot), subwoofers)
            pts = _points()
            return {
                "delay_ms": float(kwargs["delay_ms"]),
                "sub1_alignment_ms": float(kwargs.get("sub1_alignment_ms", kwargs["delay_ms"])),
                "sub2_alignment_ms": float(kwargs.get("sub2_alignment_ms", 0.0)),
                "name": f"S1 {kwargs.get('sub1_alignment_ms', 0.0):.2f}",
                "status": "completed",
                "scan": kwargs["stage"],
                "points": pts,
                "points_left": copy.deepcopy(pts),
                "points_right": copy.deepcopy(pts),
                "calibrated_points_left": copy.deepcopy(pts),
                "calibrated_points_right": copy.deepcopy(pts),
                "normalized_by_db_left": 0.0,
                "normalized_by_db_right": 0.0,
                "stage_output_peaks": {"stage": kwargs["stage"]},
            }

        def score_matrix(rows, **kwargs):
            # Winner (-3.12/-0.58) beats the incumbent (0/0), as in bf217b7b318a.
            def key(row):
                return (round(float(row.get("sub1_alignment_ms", 0.0)), 2),
                        round(float(row.get("sub2_alignment_ms", 0.0)), 2))
            results = []
            for row in rows:
                score = 0.6349 if key(row) == (-3.12, -0.58) else (
                    0.3355 if key(row) == (0.0, 0.0) else 0.2)
                results.append({
                    **row,
                    "delay_ms": float(row.get("sub1_alignment_ms", 0.0)),
                    "score": score, "final_score": score,
                    "score_pct": round(score * 100.0, 1),
                    "xo_score": score, "timing_band_score": score,
                    "low_guard_loss_db": 0.0, "low_guard_penalty": 0.0,
                    "score_L": score, "score_L_pct": round(score * 100.0, 1),
                    "score_R": score, "score_R_pct": round(score * 100.0, 1),
                })
            results.sort(key=lambda r: r["score"], reverse=True)
            incumbent = next((r for r in results if key(r) == (0.0, 0.0)), None)
            return {
                "winner": results[0], "runner_up": results[1] if len(results) > 1 else None,
                "results": results, "confidence": "clear",
                "matrix_winner": results[0], "incumbent_winner": incumbent,
                "incumbent_score": 0.3355, "accepted_winner": results[0],
                "incumbent_accepted": False, "reject_reason": "matrix_better",
            }

        def score_combined(rows, **kwargs):
            results = []
            for row in rows:
                delay = round(float(row.get("delay_ms", 0.0)), 2)
                score = 0.65 if delay == -3.12 else (0.68 if delay == -0.78 else 0.3)
                results.append({
                    **row, "score": score, "final_score": score,
                    "score_pct": round(score * 100.0, 1),
                    "xo_score": score, "timing_band_score": score,
                    "low_guard_loss_db": 0.0, "low_guard_penalty": 0.0,
                    "score_L": score, "score_L_pct": round(score * 100.0, 1),
                    "score_R": score, "score_R_pct": round(score * 100.0, 1),
                })
            results.sort(key=lambda r: r["score"], reverse=True)
            return {
                "winner": results[0], "runner_up": results[1] if len(results) > 1 else None,
                "results": results, "confidence": "clear",
                "scored_candidates": rows,
            }

        def gain_diagnostics(value):
            return {
                "available": True, "gain_calculated": True, "confidence": "high",
                "recommendation": {"type": "common", "raw_delta_db": value, "delta_db": value},
                "channels": {
                    "left": {"raw_recommendation_db": value, "target_delta_db": value, "chain_anchor_db": 0.0},
                    "right": {"raw_recommendation_db": value, "target_delta_db": value, "chain_anchor_db": 0.0},
                },
            }

        def fake_transfer(**kwargs):
            # Record which incumbent residual the runner looked up.
            seen_transfer_incumbent.update(copy.deepcopy(kwargs.get("incumbent_residuals_db") or {}))
            from measurement.autosub import measurement as m
            return m._auto_sub_balance_transfer_deltas(**kwargs)

        async def finish_worker(_job, _job_id):
            return None

        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(runner, "_measurement_session", return_value=None))
                stack.enter_context(patch.object(runner, "_dsp_runtime", return_value=None))
                stack.enter_context(patch.object(runner, "_capture_auto_sub_main_references", new=AsyncMock()))
                stack.enter_context(patch.object(runner, "_measure_auto_sub_combined_candidate", side_effect=measure_candidate))
                stack.enter_context(patch.object(runner, "_auto_sub_gate_candidate_rows", side_effect=lambda rows, *_a, **_k: (rows, [])))
                stack.enter_context(patch.object(runner, "_score_auto_sub_combined_candidates", side_effect=score_combined))
                stack.enter_context(patch.object(runner, "_score_auto_sub_matrix_candidates", side_effect=score_matrix))
                stack.enter_context(patch.object(runner, "_auto_sub_polarity_decision", return_value={
                    "accepted": False, "score_gain": -0.1, "min_score_gain": 0.08, "reason": "incumbent_best",
                }))
                stack.enter_context(patch.object(runner, "_calculate_auto_sub_gain", side_effect=[
                    gain_diagnostics(4.377), gain_diagnostics(-0.953), gain_diagnostics(0.1),
                ]))
                stack.enter_context(patch.object(runner, "_auto_sub_gain_deltas", return_value={"left": 4.377, "right": 4.377}))
                stack.enter_context(patch.object(runner, "_auto_sub_target_residual_raw_db", side_effect=[
                    (3.745, 0.0, []), (3.745, 0.0, []),
                ]))
                stack.enter_context(patch.object(runner, "_auto_sub_balance_transfer_deltas", side_effect=fake_transfer))
                stack.enter_context(patch.object(runner, "_auto_sub_gain_verdict", return_value={
                    "accepted": verdict_accepted, "reason": "test", "channels": {},
                }))
                stack.enter_context(patch.object(runner, "_auto_sub_local_dip_db", side_effect=(
                    [8.02, 5.61, 14.81, 8.61, 9.33, 8.07] if gate_veto else [8.02, 5.61, 8.0, 5.5]
                )))
                stack.enter_context(patch.object(runner, "_auto_sub_dip_guard_should_veto",
                    return_value=(gate_veto, {"failed_sides": ["left", "right"] if gate_veto else [],
                                              "reason": "combined_deterioration" if gate_veto else "no_per_side_trigger"})))
                stack.enter_context(patch.object(runner, "_auto_sub_apply_candidate", side_effect=apply_candidate))
                stack.enter_context(patch.object(runner, "_finish_auto_sub_worker", side_effect=finish_worker))
                stack.enter_context(patch.object(runner, "get_audio_output_overview", side_effect=overview))
                stack.enter_context(patch.object(samplerate, "set_audio_output_mode", side_effect=persist))
                stack.enter_context(patch.object(samplerate, "_load_audio_output_mode", side_effect=lambda: copy.deepcopy(state)))
                stack.enter_context(patch("measurement.session._resolve_measurement_start_sample_rate", return_value=48000))

                await runner._run_auto_sub_22_optimize(
                    job_id=job_id, input_id="test-input",
                    mic_input_channel="1", reference_input_channel="",
                    calibration_ref="", calibration_filename=None, calibration_bytes=None,
                    sub1_scan_delays=[-3.12, 0.0], sub2_scan_delays=[-0.58, 0.0],
                    fc=80, original_config_snapshot=original, fine_step_ms=0.2,
                )
        finally:
            runner._AUTO_SUB_JOBS.pop(job_id, None)
        return job, state, seen_transfer_incumbent

    async def test_winner_path_applies_winner_pair_and_reports_derived(self):
        job, state, _seen = await self._run_path(gate_veto=False, verdict_accepted=True)
        self.assertEqual(job["status"], "completed", job.get("error"))
        result = job["result"]
        # Candidate -> derived -> apply -> stored final state.
        self.assertEqual(result["applied_sub1_alignment_ms"], -3.12)
        self.assertEqual(result["applied_sub2_alignment_ms"], -0.58)
        self.assertEqual(result["suggested_sub1_alignment_ms"], -3.12)
        self.assertEqual(result["suggested_sub2_alignment_ms"], -0.58)
        self.assertEqual(state["subwoofers"]["sub1"]["alignment_ms"], -3.12)
        self.assertEqual(state["subwoofers"]["sub2"]["alignment_ms"], -0.58)
        self.assertAlmostEqual(result["derived_main_delay_ms"], 3.12, places=2)
        self.assertAlmostEqual(result["derived_sub1_delay_ms"], 0.0, places=2)
        self.assertAlmostEqual(result["derived_sub2_delay_ms"], 2.54, places=2)

    async def test_gate_revert_keeps_incumbent_applied_but_winner_suggested(self):
        job, state, _seen = await self._run_path(gate_veto=True, verdict_accepted=False)
        self.assertEqual(job["status"], "completed", job.get("error"))
        result = job["result"]
        self.assertEqual(result["confirmation_gate"]["action"], "alignment_reverted_balance_kept")
        # Applied state is the incumbent pair; the rejected winner stays suggested.
        self.assertEqual(result["applied_sub1_alignment_ms"], 0.0)
        self.assertEqual(result["applied_sub2_alignment_ms"], 0.0)
        self.assertEqual(result["suggested_sub1_alignment_ms"], -3.12)
        self.assertEqual(result["suggested_sub2_alignment_ms"], -0.58)
        self.assertEqual(state["subwoofers"]["sub1"]["alignment_ms"], 0.0)
        self.assertEqual(state["subwoofers"]["sub2"]["alignment_ms"], 0.0)
        self.assertEqual(result["confidence"], "gate_reverted")
        self.assertEqual(result["reject_reason"], "final_state_regressed_incumbent_pair_kept")

    async def test_transfer_incumbent_comes_from_both_subs_matrix(self):
        _job, _state, seen = await self._run_path(gate_veto=False, verdict_accepted=True)
        # The runner's transfer lookup resolves the incumbent residual from
        # the Both-subs matrix row: the lookup must succeed (no fallback to
        # a missing/empty candidate which yields incumbent None -> transfer
        # unavailable or a stale single-sub residual).
        self.assertIn("left", seen)
        self.assertIn("right", seen)


if __name__ == "__main__":
    unittest.main()
