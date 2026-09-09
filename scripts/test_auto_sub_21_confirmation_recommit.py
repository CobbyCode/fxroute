#!/usr/bin/env python3
"""Behavioral regression coverage for the 2.1 confirmation recommit."""

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


class AutoSub21ConfirmationRecommitTests(unittest.IsolatedAsyncioTestCase):
    async def _run_reference_case(self, *, fail_recommit=False, fail_restore=False):
        job_id = "auto-sub-21-confirmation-recommit"
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

        apply_count = 0

        async def apply_candidate(*, output_mode, global_config, subwoofers_config, verify, load_overview=None):
            nonlocal apply_count
            apply_count += 1
            persist(output_mode, global_config, subwoofers_config)
            if fail_recommit and apply_count == 2:
                state["mode"] = "stereo"
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

        def score_candidates(rows, **_kwargs):
            results = []
            for row in rows:
                delay = round(float(row.get("delay_ms", 0.0)), 2)
                score = 0.8 if delay in (0.78, 0.0) else 0.1
                if any(round(float(candidate.get("delay_ms", 0.0)), 2) == 0.78 for candidate in rows):
                    score = 0.8 if delay == 0.78 else 0.2
                results.append({
                    **row,
                    "score": score,
                    "final_score": score,
                    "score_pct": score * 100.0,
                    "xo_score": score,
                    "timing_band_score": score,
                    "low_guard_loss_db": 0.0,
                    "low_guard_penalty": 0.0,
                    "score_L": score,
                    "score_R": score,
                    "score_L_pct": score * 100.0,
                    "score_R_pct": score * 100.0,
                })
            results.sort(key=lambda row: row["score"], reverse=True)
            return {
                "winner": results[0],
                "runner_up": results[1] if len(results) > 1 else None,
                "results": results,
                "scored_candidates": rows,
                "confidence": "clear",
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

        async def restore_apply_candidate(*, output_mode, global_config, subwoofers_config, verify, load_overview=None):
            # The verified restore helper lives in candidates and calls
            # candidates._auto_sub_apply_candidate; route it through the same
            # fake persist/overview so the restore state stays test-local.
            if fail_restore:
                return False
            persist(output_mode, global_config, subwoofers_config)
            return bool(verify(overview()))

        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(runner, "_measurement_session", return_value=None))
                stack.enter_context(patch.object(runner, "_dsp_runtime", return_value=None))
                stack.enter_context(patch("measurement.autosub.candidates._dsp_runtime", return_value=None))
                stack.enter_context(patch.object(runner, "_capture_auto_sub_main_references", new=AsyncMock()))
                stack.enter_context(patch.object(runner, "_measure_auto_sub_combined_candidate", side_effect=measure_candidate))
                stack.enter_context(patch.object(runner, "_auto_sub_gate_candidate_rows", side_effect=lambda rows, *_a, **_k: (rows, [])))
                stack.enter_context(patch.object(runner, "_score_auto_sub_combined_candidates", side_effect=score_candidates))
                stack.enter_context(patch.object(runner, "_auto_sub_fine_trigger_reasons", return_value=[]))
                stack.enter_context(patch.object(runner, "_auto_sub_polarity_decision", return_value={
                    "accepted": False, "score_gain": -0.1, "min_score_gain": 0.03,
                    "reason": "incumbent_protected_unclear_advantage",
                }))
                stack.enter_context(patch.object(runner, "_calculate_auto_sub_gain", return_value=copy.deepcopy(gain_diagnostics)))
                stack.enter_context(patch.object(runner, "_auto_sub_gain_deltas", return_value={}))
                stack.enter_context(patch.object(runner, "_auto_sub_gain_verdict", return_value={
                    "accepted": True, "reason": "accepted", "channels": {},
                }))
                stack.enter_context(patch.object(runner, "_auto_sub_local_dip_db", side_effect=[
                    7.25, 6.25, 10.40, 10.20, 10.30, 10.10,
                ]))
                stack.enter_context(patch.object(runner, "_auto_sub_apply_candidate", side_effect=apply_candidate))
                stack.enter_context(patch("measurement.autosub.candidates._auto_sub_apply_candidate", side_effect=restore_apply_candidate))
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
                    scan_delays=[0.0, 0.78],
                    fc=80,
                    current_alignment=0.0,
                    original_polarity="normal",
                    original_level=0.0,
                    original_highpass=True,
                    original_config_snapshot=original,
                )
        finally:
            runner._AUTO_SUB_JOBS.pop(job_id, None)

        return job, state, original, apply_count

    async def test_non_reproduced_regression_recommits_exact_winner_state(self):
        job, state, _original, apply_count = await self._run_reference_case()

        self.assertEqual(job["status"], "completed", job.get("error"))
        self.assertEqual(apply_count, 2)
        self.assertEqual(state, {
            "mode": runner.OUTPUT_MODE_SUBWOOFER_21,
            "subwoofer": {
                "crossover_frequency_hz": 80,
                "main_highpass_enabled": True,
                "sub_alignment_ms": 0.78,
                "sub_level_db": 0.0,
                "sub_polarity": "normal",
            },
        })
        self.assertEqual(job["confirmation_gate"]["confirmed_failed_sides"], [])
        self.assertEqual(job["confirmation_gate"]["action"], "final_kept")
        self.assertEqual(job["result"]["applied_alignment_ms"], 0.78)

    async def test_failed_winner_recommit_restores_original_state(self):
        job, state, original, apply_count = await self._run_reference_case(fail_recommit=True)

        self.assertEqual(apply_count, 2)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["confirmation_gate"]["action"], "winner_recommit_failed")
        self.assertEqual(state, original)

    async def test_restore_verification_failure_fails_the_job(self):
        # A restore the live state cannot confirm (even after the one
        # re-apply) must fail the job instead of ending on a silently
        # different topology.
        job, _state, _original, apply_count = await self._run_reference_case(
            fail_recommit=True, fail_restore=True,
        )

        self.assertEqual(apply_count, 2)
        self.assertEqual(job["status"], "failed")
        self.assertIn("failed to restore the original config", job["message"])
        self.assertIn("restore verification failed", str(job.get("error") or {}))


if __name__ == "__main__":
    unittest.main()
