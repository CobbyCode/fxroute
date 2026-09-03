#!/usr/bin/env python3
"""Path coverage: 2.2-stereo candidate -> derived -> apply -> stored final state.

Robust companion to test_auto_sub_22_stereo_final_commit.py (which pins
exact intermediate gain levels via side_effect lists and predates the
balance-first flow): this test mocks gain/dip helpers with return_value
so runner-internal call counts cannot drift it, and asserts the full
path from the scored L/R winners to the persisted final state.
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
from measurement.autosub.runners import optimize_22_stereo as runner


def _points(value_db=0.0):
    freqs = [20.0, 25.0, 31.5, 40.0, 50.0, 63.0, 80.0, 100.0,
             125.0, 160.0, 200.0, 250.0, 315.0, 400.0, 500.0, 640.0]
    return [[f, value_db] for f in freqs]


class AutoSub22StereoFinalPathTests(unittest.IsolatedAsyncioTestCase):
    async def _run_path(self):
        job_id = "auto-sub-22s-path"
        original = {
            "mode": runner.OUTPUT_MODE_SUBWOOFER_22_STEREO,
            "crossover_frequency_hz": 80,
            "main_highpass_enabled": True,
            "subwoofers": {
                "sub1": {"level_db": 0.0, "alignment_ms": 0.0, "polarity": "normal"},
                "sub2": {"level_db": 0.0, "alignment_ms": 0.0, "polarity": "normal"},
            },
        }
        state = copy.deepcopy(original)
        target_points = _points()
        job = {
            "id": job_id,
            "mode": runner.OUTPUT_MODE_SUBWOOFER_22_STEREO,
            "status": "preparing",
            "cancel_requested": False,
            "target_curve": {"label": "Neutral", "points": target_points},
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
                "name": f'{kwargs["delay_ms"]:.2f}',
                "status": "completed",
                "scan": kwargs["stage"],
                "points": copy.deepcopy(pts),
                "calibrated_points": copy.deepcopy(pts),
                "normalized_by_db": 0.0,
                "stage_output_peaks": {"stage": kwargs["stage"]},
            }

        def score_candidates(rows, **_kwargs):
            scores = {-2.14: 0.6810, -2.54: 0.6810, 0.0: 0.15, 1.0: 0.05}
            results = []
            for row in rows:
                delay = round(float(row.get("delay_ms", 0.0)), 2)
                score = scores.get(delay, 0.04)
                results.append({
                    **row, "score": score, "final_score": score,
                    "score_pct": round(score * 100.0, 1),
                    "xo_score": 0.75, "timing_band_score": 0.80,
                    "low_guard_loss_db": 0.0, "low_guard_penalty": 0.0,
                })
            results.sort(key=lambda row: row["score"], reverse=True)
            return {
                "winner": results[0],
                "runner_up": results[1] if len(results) > 1 else None,
                "results": results, "confidence": "clear",
            }

        def gain_diagnostics(left, right):
            return {
                "available": True, "gain_calculated": True, "confidence": "medium",
                "recommendation": {"type": "per_channel", "left_delta_db": left, "right_delta_db": right},
                "channels": {
                    "left": {"raw_recommendation_db": left, "target_delta_db": left, "chain_anchor_db": 0.0},
                    "right": {"raw_recommendation_db": right, "target_delta_db": right, "chain_anchor_db": 0.0},
                },
            }

        async def finish_worker(_job, _job_id):
            return None

        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(runner, "_measurement_session", return_value=None))
                stack.enter_context(patch.object(runner, "_dsp_runtime", return_value=None))
                stack.enter_context(patch.object(runner, "_capture_auto_sub_main_references", new=AsyncMock()))
                stack.enter_context(patch.object(runner, "_measure_auto_sub_candidate", side_effect=measure_candidate))
                stack.enter_context(patch.object(runner, "_auto_sub_gate_candidate_rows", side_effect=lambda rows, *_a, **_k: (rows, [])))
                stack.enter_context(patch.object(runner, "score_sub_alignment_candidates", side_effect=score_candidates))
                stack.enter_context(patch.object(runner, "_auto_sub_fine_delay_candidates", return_value=[]))
                stack.enter_context(patch.object(runner, "_auto_sub_remeasure_tiebreak", new=AsyncMock(return_value=None)))
                stack.enter_context(patch.object(runner, "_auto_sub_polarity_decision", return_value={
                    "accepted": False, "score_gain": -0.2, "min_score_gain": 0.03,
                    "reason": "incumbent_protected_unclear_advantage",
                }))
                stack.enter_context(patch.object(runner, "_calculate_auto_sub_gain",
                    side_effect=lambda **_k: gain_diagnostics(0.0, 0.0)))
                stack.enter_context(patch.object(runner, "_auto_sub_gain_deltas", return_value={}))
                stack.enter_context(patch.object(runner, "_auto_sub_target_residual_raw_db",
                    return_value=(0.0, 0.0, [])))
                stack.enter_context(patch.object(runner, "_auto_sub_balance_transfer_deltas", return_value={
                    "available": False, "reason": "test-no-transfer", "alignment_changed": {},
                }))
                stack.enter_context(patch.object(runner, "_auto_sub_gain_verdict", return_value={
                    "accepted": True, "reason": "test-accepted", "channels": {},
                }))
                stack.enter_context(patch.object(runner, "_auto_sub_local_dip_db", return_value=5.0))
                stack.enter_context(patch.object(runner, "_auto_sub_apply_candidate", side_effect=apply_candidate))
                stack.enter_context(patch.object(runner, "_finish_auto_sub_worker", side_effect=finish_worker))
                stack.enter_context(patch.object(runner, "get_audio_output_overview", side_effect=overview))
                stack.enter_context(patch.object(samplerate, "set_audio_output_mode", side_effect=persist))
                stack.enter_context(patch.object(samplerate, "_load_audio_output_mode", side_effect=lambda: copy.deepcopy(state)))
                stack.enter_context(patch("measurement.session._resolve_measurement_start_sample_rate", return_value=48000))

                await runner._run_auto_sub_22_stereo_optimize(
                    job_id=job_id, input_id="test-input",
                    mic_input_channel="1", reference_input_channel="",
                    calibration_ref="", calibration_filename=None, calibration_bytes=None,
                    left_scan_delays=[-2.14, 0.0], right_scan_delays=[-2.54, 0.0],
                    fc=80, original_config_snapshot=original,
                )
        finally:
            runner._AUTO_SUB_JOBS.pop(job_id, None)
        return job, state

    async def test_winner_pair_applied_and_derived_reported(self):
        job, state = await self._run_path()
        self.assertEqual(job["status"], "completed", job.get("error"))
        result = job["result"]
        # Candidate -> derived -> apply -> stored final state.
        self.assertEqual(result["applied_sub1_alignment_ms"], -2.14)
        self.assertEqual(result["applied_sub2_alignment_ms"], -2.54)
        self.assertEqual(result["suggested_sub1_alignment_ms"], -2.14)
        self.assertEqual(result["suggested_sub2_alignment_ms"], -2.54)
        self.assertEqual(state["subwoofers"]["sub1"]["alignment_ms"], -2.14)
        self.assertEqual(state["subwoofers"]["sub2"]["alignment_ms"], -2.54)
        self.assertAlmostEqual(result["derived_main_delay_ms"], 2.54, places=2)
        self.assertAlmostEqual(result["derived_sub1_delay_ms"], 0.40, places=2)
        self.assertAlmostEqual(result["derived_sub2_delay_ms"], 0.0, places=2)


if __name__ == "__main__":
    unittest.main()
