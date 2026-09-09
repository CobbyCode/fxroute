#!/usr/bin/env python3
"""Regression: fine winner 65.2% must not silently become 0.00 ms at 26.8%.

The 2026-09-02 05:34 run scored fine -3.71 ms at 65.2% (L 70.3 / R 63.9)
against incumbent 0.0 ms at 26.8%, accepted the fine winner, then the
final confirmation gate reverted the alignment to the incumbent on the
right local-dip regression (before 4.86 -> final 8.25). The stored state
must stay consistent: the real committed alignment is 0.0 ms, and the
displayed score must describe that alignment (the incumbent's 26.8%),
not the uncommitted fine candidate's 65.2%. A silent mismatch would let
'AutoSub applied: 0.00 ms (was 0.00 ms) Score 65.2%' fake a successful
delay change that never landed on the hardware.
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
from measurement.autosub.runners import optimize as runner


def _make_score_fn(scores_by_delay, default=0.1):
    from measurement import autosub as autosub_mod

    def score(rows, **_kwargs):
        results = []
        for row in rows:
            delay = round(float(row.get("delay_ms", 0.0)), 2)
            value = scores_by_delay.get(delay, default)
            results.append({
                **row, "score": value, "final_score": value,
                "score_pct": value * 100.0, "xo_score": value, "timing_band_score": value,
                "low_guard_loss_db": 0.0, "low_guard_penalty": 0.0,
                "score_L": value, "score_R": value,
                "score_L_pct": value * 100.0, "score_R_pct": value * 100.0,
            })
        results.sort(key=lambda item: item["score"], reverse=True)
        return {
            "winner": results[0], "runner_up": results[1] if len(results) > 1 else None,
            "results": results, "scored_candidates": rows,
            "confidence": autosub_mod._auto_sub_scoring_confidence(results),
        }

    return score


class AutoSubGateRevertDisplayTests(unittest.IsolatedAsyncioTestCase):
    async def _run_gate_revert_case(self):
        job_id = "auto-sub-gate-revert"
        original = {
            "mode": runner.OUTPUT_MODE_SUBWOOFER_21,
            "subwoofer": {
                "crossover_frequency_hz": 80, "main_highpass_enabled": True,
                "sub_alignment_ms": 0.0, "sub_level_db": 0.0, "sub_polarity": "normal",
            },
        }
        state = copy.deepcopy(original)
        frequencies = [20.0, 25.0, 31.5, 40.0, 50.0, 63.0, 80.0, 100.0,
                       125.0, 160.0, 200.0, 250.0, 315.0, 400.0, 500.0, 640.0]
        points = [[f, 0.0] for f in frequencies]
        job = {
            "id": job_id, "mode": runner.OUTPUT_MODE_SUBWOOFER_21,
            "status": "preparing", "cancel_requested": False,
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
                "delay_ms": float(kwargs["delay_ms"]), "name": f'{kwargs["delay_ms"]:.2f}',
                "status": "completed", "status_left": "completed", "status_right": "completed",
                "scan": kwargs["stage"], "points": copy.deepcopy(points),
                "points_left": copy.deepcopy(points), "points_right": copy.deepcopy(points),
                "calibrated_points_left": copy.deepcopy(points), "calibrated_points_right": copy.deepcopy(points),
                "normalized_by_db_left": 0.0, "normalized_by_db_right": 0.0,
                "combined_candidate": True, "stage_output_peaks": {"stage": kwargs["stage"]},
            }

        gain_diagnostics = {
            "available": True, "gain_calculated": True, "confidence": "high",
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
                stack.enter_context(patch.object(runner, "_score_auto_sub_combined_candidates", side_effect=_make_score_fn({0.0: 0.268, -3.12: 0.576, -3.71: 0.652})))
                stack.enter_context(patch.object(runner, "_auto_sub_fine_trigger_reasons", return_value=["coarse_winner_at_scan_edge"]))
                stack.enter_context(patch.object(runner, "_auto_sub_fine_delay_candidates", return_value=[-3.71]))
                stack.enter_context(patch.object(runner, "_auto_sub_polarity_decision", return_value={"accepted": False, "score_gain": -0.1, "min_score_gain": 0.03, "reason": "incumbent_protected_unclear_advantage"}))
                stack.enter_context(patch.object(runner, "_calculate_auto_sub_gain", return_value=copy.deepcopy(gain_diagnostics)))
                stack.enter_context(patch.object(runner, "_auto_sub_gain_deltas", return_value={}))
                stack.enter_context(patch.object(runner, "_auto_sub_gain_verdict", return_value={"accepted": True, "reason": "accepted", "channels": {}}))
                stack.enter_context(patch.object(runner, "_auto_sub_local_dip_db", side_effect=[
                    3.0, 3.0,     # before left/right (balance_sweep) shallow
                    12.0, 12.8,   # final left/right (winner introduces deep notch on both sides, combined +9 -> veto even with large gain)
                    3.10, 3.05,   # recheck left/right (incumbent stable -> incumbent_passed True -> incumbent_kept)
                ]))
                stack.enter_context(patch.object(runner, "_auto_sub_apply_candidate", side_effect=apply_candidate))
                stack.enter_context(patch.object(runner, "_finish_auto_sub_worker", side_effect=finish_worker))
                stack.enter_context(patch.object(runner, "get_audio_output_overview", side_effect=overview))
                stack.enter_context(patch.object(samplerate, "set_audio_output_mode", side_effect=persist))
                stack.enter_context(patch.object(samplerate, "_load_audio_output_mode", side_effect=overview))
                stack.enter_context(patch("measurement.session._resolve_measurement_start_sample_rate", return_value=48000))
                await runner._run_auto_sub_optimize(
                    job_id=job_id, input_id="test-input", channel="left",
                    mic_input_channel="1", reference_input_channel="",
                    calibration_ref="", calibration_filename=None, calibration_bytes=None,
                    scan_delays=[0.0, -3.12], fc=80, current_alignment=0.0,
                    original_polarity="normal", original_level=0.0, original_highpass=True,
                    original_config_snapshot=original,
                )
        finally:
            runner._AUTO_SUB_JOBS.pop(job_id, None)
        return job, state

    async def test_gate_revert_score_matches_committed_alignment(self):
        """When the confirmation gate reverts to the incumbent, the committed
        score must describe the committed alignment (incumbent 26.8%), not the
        uncommitted fine winner (65.2%). Otherwise the status line
        'applied: 0.00 ms Score 65.2%' fakes a delay change that never landed."""
        job, state = await self._run_gate_revert_case()

        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["result"]["confirmation_gate"]["action"], "alignment_reverted_balance_kept")
        self.assertEqual(job["result"]["applied_alignment_ms"], 0.0)
        self.assertEqual(job["result"]["suggested_alignment_ms"], -3.71)
        self.assertEqual(state["subwoofer"]["sub_alignment_ms"], 0.0)
        self.assertFalse(job["result"]["fine_accepted"])
        self.assertEqual(job["result"]["reject_reason"], "final_state_regressed_incumbent_alignment_kept")
        self.assertEqual(job["result"]["winner"]["delay_ms"], 0.0)
        self.assertAlmostEqual(job["result"]["winner"]["score_pct"], 26.8, places=1)
        self.assertEqual(job["result"]["confidence"], "gate_reverted")
        self.assertEqual(job["result"]["accepted_winner"]["delay_ms"], 0.0)

    async def test_gate_revert_without_score_divergence(self):
        job, _state = await self._run_gate_revert_case()
        self.assertEqual(job["result"]["winner"]["delay_ms"], job["result"]["applied_alignment_ms"])
        self.assertEqual(job["result"]["fine_winner"]["delay_ms"], -3.71)


if __name__ == "__main__":
    unittest.main()
