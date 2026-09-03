#!/usr/bin/env python3
"""Regression coverage for the authoritative 2.2-stereo AutoSub commit."""

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
from measurement.autosub.runners import optimize_22_stereo as runner


class AutoSub22StereoFinalCommitTests(unittest.IsolatedAsyncioTestCase):
    async def _run_reference_case(
        self, *, fail_final_commit=False, deep_bass_regression=False,
        final_confirmation_missing=False,
    ):
        job_id = "auto-sub-final-commit"
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
        target_points = [[20.0, 0.0], [30.0, 0.0], [40.0, 0.0], [80.0, 0.0], [160.0, 0.0], [320.0, 0.0], [600.0, 0.0]]
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

        apply_count = 0

        async def apply_candidate(*, output_mode, global_config, subwoofers_config, verify, load_overview=None):
            nonlocal apply_count
            apply_count += 1
            persist(output_mode, global_config, subwoofers_config)
            if fail_final_commit and apply_count == 1:
                return False
            return bool(verify((load_overview or (lambda: state))()))

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
            points = copy.deepcopy(target_points)
            if deep_bass_regression and kwargs["stage"] == "deep_bass_check":
                points = [[hz, -4.0 if hz <= 40.0 else db] for hz, db in points]
            if final_confirmation_missing and kwargs["stage"] == "final_commit_confirmation":
                points = []
            return {
                "delay_ms": float(kwargs["delay_ms"]),
                "name": f'{kwargs["delay_ms"]:.2f}',
                "status": "completed",
                "scan": kwargs["stage"],
                "points": points,
                "calibrated_points": copy.deepcopy(points),
                "normalized_by_db": 0.0,
                "stage_output_peaks": {"stage": kwargs["stage"]},
            }

        def score_candidates(rows, **_kwargs):
            scores = {-2.54: 0.6810, -2.14: 0.5544, 0.0: 0.15, 1.0: 0.05}
            results = []
            for row in rows:
                delay = round(float(row.get("delay_ms", 0.0)), 2)
                score = scores.get(delay, 0.04)
                results.append({
                    **row,
                    "score": score,
                    "final_score": score,
                    "score_pct": round(score * 100.0, 1),
                    "xo_score": 0.75,
                    "timing_band_score": 0.80,
                    "low_guard_loss_db": 0.0,
                    "low_guard_penalty": 0.0,
                })
            results.sort(key=lambda row: row["score"], reverse=True)
            return {
                "winner": results[0],
                "runner_up": results[1] if len(results) > 1 else None,
                "results": results,
                "confidence": "clear",
            }

        def gain_diagnostics(left, right):
            return {
                "available": True,
                "gain_calculated": True,
                "confidence": "medium",
                "recommendation": {
                    "type": "per_channel", "left_delta_db": left, "right_delta_db": right,
                },
                "channels": {
                    "left": {
                        "raw_recommendation_db": left, "target_delta_db": left, "chain_anchor_db": 0.0,
                    },
                    "right": {
                        "raw_recommendation_db": right, "target_delta_db": right, "chain_anchor_db": 0.0,
                    },
                },
            }

        async def finish_worker(_job, _job_id):
            return None

        polarity_gate = [
            {"accepted": True, "score_gain": 0.2, "min_score_gain": 0.03, "reason": "alternative_clearly_better"},
            {"accepted": False, "score_gain": -0.2, "min_score_gain": 0.03, "reason": "incumbent_protected_unclear_advantage"},
        ]

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
                stack.enter_context(patch.object(runner, "_auto_sub_polarity_decision", side_effect=polarity_gate))
                stack.enter_context(patch.object(runner, "_auto_sub_select_polarity_shared_winner", return_value={
                    "accepted": True, "alternative_delay_ms": 1.0, "reason": "alternative_clearly_better",
                }))
                stack.enter_context(patch.object(runner, "_calculate_auto_sub_gain", side_effect=[
                    gain_diagnostics(3.111, 0.412),
                    gain_diagnostics(0.850, 0.773),
                    gain_diagnostics(0.613, 0.252),
                ]))
                stack.enter_context(patch.object(runner, "_auto_sub_target_residual_raw_db", side_effect=[
                    (0.625, 0.0, []), (0.158, 0.0, []),
                ]))
                stack.enter_context(patch.object(runner, "_auto_sub_balance_transfer_deltas", return_value={
                    "available": True,
                    "deltas_db": {"left": 0.225, "right": 0.6155},
                    "channels": {},
                    "reason": "Balance trim transferred to the accepted configuration",
                }))
                stack.enter_context(patch.object(runner, "_auto_sub_local_dip_db", side_effect=[
                    11.51, 7.91, 8.26, 12.51, 10.21, 8.32,
                ]))
                stack.enter_context(patch.object(runner, "_auto_sub_apply_candidate", side_effect=apply_candidate))
                stack.enter_context(patch.object(runner, "_finish_auto_sub_worker", side_effect=finish_worker))
                stack.enter_context(patch.object(runner, "get_audio_output_overview", side_effect=overview))
                stack.enter_context(patch.object(samplerate, "set_audio_output_mode", side_effect=persist))
                stack.enter_context(patch.object(samplerate, "_load_audio_output_mode", side_effect=lambda: copy.deepcopy(state)))
                stack.enter_context(patch("measurement.session._resolve_measurement_start_sample_rate", return_value=48000))

                await runner._run_auto_sub_22_stereo_optimize(
                    job_id=job_id,
                    input_id="test-input",
                    mic_input_channel="1",
                    reference_input_channel="",
                    calibration_ref="",
                    calibration_filename=None,
                    calibration_bytes=None,
                    left_scan_delays=[-2.14, 0.0],
                    right_scan_delays=[-2.54, 0.0],
                    fc=80,
                    original_config_snapshot=original,
                )
        finally:
            runner._AUTO_SUB_JOBS.pop(job_id, None)

        return job, state, original

    async def test_confirmation_recheck_keeps_winner_alignment_polarity_and_balanced_gains(self):
        """A diagnostic incumbent recheck must not become the last config writer.

        The alignment, dip and gain values mirror job auto-sub-fc8eb23e2926.
        The left polarity is flipped in this fixture so the same regression
        also catches a final commit that restores the snapshot polarity.

        Since the combined dip-guard change the fixture dips (R +4.6 with L
        improved -3.25, combined +0.48) no longer veto: the gate keeps the
        winner (final_kept) and the final state carries the transferred
        balance trim on top of the balanced levels.
        """
        job, state, _original = await self._run_reference_case()

        self.assertEqual(job["status"], "completed", job.get("error"))
        self.assertEqual(job["result"]["confirmation_gate"]["action"], "final_kept")
        self.assertEqual(state["subwoofers"]["sub1"], {
            "level_db": 3.3360000000000003, "alignment_ms": -2.14, "polarity": "invert",
        })
        self.assertEqual(state["subwoofers"]["sub2"], {
            "level_db": 1.0275, "alignment_ms": -2.54, "polarity": "normal",
        })
        self.assertEqual(job["result"]["applied_sub1_alignment_ms"], -2.14)
        self.assertEqual(job["result"]["applied_sub2_alignment_ms"], -2.54)
        self.assertEqual(job["polarity_check"]["left"]["selected"], "invert")
        self.assertEqual(job["result"]["confirmation_gate"]["failed_sides"], ["right"])
        self.assertEqual(job["auto_gain"]["final_levels_db"], {"sub1": 3.3360000000000003, "sub2": 1.0275})
        self.assertEqual(job["auto_gain"]["final_deltas_db"], {"left": 0.225, "right": 0.6155})

    async def test_failed_final_commit_restores_and_verifies_original_state(self):
        job, state, original = await self._run_reference_case(fail_final_commit=True)

        self.assertEqual(job["status"], "failed")
        self.assertEqual(state, original)
        self.assertEqual(
            job["error"]["detail"],
            "Winner apply failed - original config restored",
        )

    async def test_polarity_revert_captures_final_evidence_at_balanced_gain(self):
        job, state, _original = await self._run_reference_case(deep_bass_regression=True)

        self.assertEqual(job["status"], "completed", job.get("error"))
        self.assertEqual(state["subwoofers"]["sub1"], {
            "level_db": 3.3360000000000003, "alignment_ms": -2.14, "polarity": "normal",
        })
        self.assertTrue(job["deep_bass_check"]["reverted_polarity"])
        self.assertEqual(job["result"]["confirmation_gate"]["action"], "final_kept")
        self.assertEqual(job["auto_gain"]["stage_output_peaks"]["left"], {
            "stage": "gain_after",
        })

    async def test_missing_exact_confirmation_does_not_publish_single_sub_scan(self):
        job, _state, _original = await self._run_reference_case(
            deep_bass_regression=True, final_confirmation_missing=True,
        )

        self.assertEqual(job["status"], "completed", job.get("error"))
        self.assertTrue(job["deep_bass_check"]["reverted_polarity"])
        self.assertEqual(job["result"]["confirmation_gate"]["action"], "final_kept")
        confirmation = job["result"]["confirmation_measurement"]
        self.assertIsNotNone(confirmation)
        for trace in confirmation.get("traces", []):
            self.assertTrue(trace.get("label", "").startswith("After"))
            self.assertGreaterEqual(len(trace.get("points", [])), 3)


if __name__ == "__main__":
    unittest.main()
