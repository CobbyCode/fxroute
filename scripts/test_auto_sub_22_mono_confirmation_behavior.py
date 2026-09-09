#!/usr/bin/env python3
"""Behavioral confirmation contracts for the 2.2-mono runner.

The After/Confirmation measurement must be built from the final measured
sweep (here: the polarity-refined winner living outside the scanned delay
pools) even when the gain verdict is rejected. Selecting the confirmation
by verdict alone dropped the After curve in that case.

Drives the real _run_auto_sub_22_optimize with stubbed backends: scan
stages measure flat (valid) sweeps while only the polarity-refinement
sweeps carry a distinctive ramp. A verdict-gated selection would therefore
show flat consecutive diffs (or nothing); the committed behavior shows the
winner ramp shape (anchor shifts are additive per trace, so consecutive
differences are the robust observable).
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
from measurement.autosub.runners import optimize_22 as runner


def _flat(value_db=0.0):
    freqs = [20.0, 25.0, 31.5, 40.0, 50.0, 63.0, 80.0, 100.0,
             125.0, 160.0, 200.0, 250.0, 315.0, 400.0, 500.0, 640.0]
    return [[f, value_db] for f in freqs]


def _ramp():
    freqs = [20.0, 25.0, 31.5, 40.0, 50.0, 63.0, 80.0, 100.0,
             125.0, 160.0, 200.0, 250.0, 315.0, 400.0, 500.0, 640.0]
    return [[f, round(i * 0.5, 6)] for i, f in enumerate(freqs)]


def _diffs(points):
    levels = [float(p[1]) for p in points]
    return [round(levels[i + 1] - levels[i], 6) for i in range(len(levels) - 1)]


class AutoSub22MonoConfirmationBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def _run_path(self, verdict_accepted=False,
                                    stub_response_unavailable=False,
                                    stub_gain_deltas="__unset__"):
        recorded_stages: list[str] = []
        recorded_levels: list[tuple] = []
        job_id = "auto-sub-22m-confirmation"
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
        job = {
            "id": job_id,
            "mode": runner.OUTPUT_MODE_SUBWOOFER_22,
            "status": "preparing",
            "cancel_requested": False,
            "target_curve": {"label": "Neutral", "points": _flat()},
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

        async def restore_apply_candidate(*, output_mode, global_config, subwoofers_config, verify, load_overview=None):
            persist(output_mode, global_config, subwoofers_config)
            return bool(verify(copy.deepcopy(state)))

        async def measure_candidate(**kwargs):
            recorded_stages.append(str(kwargs.get("stage", "")))
            recorded_levels.append((
                str(kwargs.get("stage", "")),
                tuple(sorted(kwargs.get("active_subs", ("sub1", "sub2")))),
                float(state["subwoofers"]["sub1"]["level_db"]),
                float(state["subwoofers"]["sub2"]["level_db"]),
            ))
            stage = str(kwargs.get("stage", ""))
            pts = _ramp() if "polarity_refine" in stage else _flat()
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
            p = copy.deepcopy(pts)
            return {
                "delay_ms": float(kwargs["delay_ms"]),
                "sub1_alignment_ms": float(kwargs.get("sub1_alignment_ms", kwargs["delay_ms"])),
                "sub2_alignment_ms": float(kwargs.get("sub2_alignment_ms", 0.0)),
                "name": f"S1 {kwargs.get('sub1_alignment_ms', 0.0):.2f}",
                "status": "completed",
                "scan": kwargs["stage"],
                "points": copy.deepcopy(p),
                "points_left": copy.deepcopy(p),
                "points_right": copy.deepcopy(p),
                "calibrated_points_left": copy.deepcopy(p),
                "calibrated_points_right": copy.deepcopy(p),
                "normalized_by_db_left": 0.0,
                "normalized_by_db_right": 0.0,
                "stage_output_peaks": {"stage": kwargs["stage"]},
            }

        def score_matrix(rows, **kwargs):
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
            # Polarity gate rows carry tested_polarities; rank an alternative
            # first there so the refinement path engages. Coarse rows keep
            # the established winner.
            if any("tested_polarities" in row for row in rows):
                ranked = sorted(rows, key=lambda row: 0.0 if round(float(row.get("delay_ms", 0.0)), 2) == 1.0 else 0.3)
                first, rest = ranked[0], ranked[1:]
                results = [{**first, "score": 0.9, "final_score": 0.9, "score_pct": 90.0}]
                results.extend({**row, "score": 0.3, "final_score": 0.3, "score_pct": 30.0} for row in rest)
                return {"winner": results[0], "results": results, "confidence": "clear"}
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
                    "accepted": True, "score_gain": 0.5, "min_score_gain": 0.03,
                    "reason": "test-gate",
                }))
                stack.enter_context(patch.object(runner, "_calculate_auto_sub_gain", side_effect=[
                    gain_diagnostics(4.377), gain_diagnostics(-0.953), gain_diagnostics(0.1),
                ]))
                stack.enter_context(patch.object(runner, "_auto_sub_gain_deltas",
                    return_value={} if stub_gain_deltas == "__unset__" else stub_gain_deltas))
                stack.enter_context(patch.object(runner, "_auto_sub_gain_verdict", return_value={
                    "accepted": verdict_accepted, "reason": "test", "channels": {},
                }))
                if stub_response_unavailable:
                    stack.enter_context(patch.object(runner, "_auto_sub_gain_response_correction", return_value={
                        "available": False, "reason": "test-unavailable", "channels": {},
                    }))
                stack.enter_context(patch.object(runner, "_auto_sub_local_dip_db", side_effect=[8.02, 5.61, 14.81, 8.61, 9.33, 8.07]))
                stack.enter_context(patch.object(runner, "_auto_sub_dip_guard_should_veto",
                    return_value=(False, {"failed_sides": [], "reason": "no_per_side_trigger"})))
                stack.enter_context(patch.object(runner, "_auto_sub_apply_candidate", side_effect=apply_candidate))
                stack.enter_context(patch("measurement.autosub.candidates._auto_sub_apply_candidate", side_effect=restore_apply_candidate))
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
        self.assertEqual(job["status"], "completed", job.get("error"))
        return job, recorded_stages, recorded_levels

    async def test_rejected_verdict_still_shows_final_sweep(self):
        """The After curve comes from the refined winner, not the verdict."""
        job, _stages, _levels = await self._run_path()
        result = job["result"]
        confirmation = result.get("confirmation_measurement")
        self.assertIsNotNone(
            confirmation,
            "a rejected gain verdict must not drop the After measurement",
        )
        left = [trace for trace in confirmation.get("traces", []) if trace.get("role") == "left"]
        self.assertTrue(left, "confirmation must carry a left trace")
        self.assertEqual(
            _diffs(left[0]["points"]),
            [0.5] * 15,
            "confirmation must show the refined-winner ramp, not the flat scan fallback",
        )

    async def test_confirmation_payload_keeps_before_and_after(self):
        """The result payload carries both baseline and confirmation."""
        job, _stages, _levels = await self._run_path()
        result = job["result"]
        self.assertIsNotNone(result.get("baseline_measurement"))
        self.assertIsNotNone(result.get("confirmation_measurement"))
        self.assertTrue(
            str(result["confirmation_measurement"].get("name", "")).startswith("AutoSub 2.2 Optimized ("),
            result["confirmation_measurement"].get("name"),
        )

    async def test_unavailable_correction_retains_step1_without_remode(self):
        """An unavailable correction keeps Step 1: no re-verdict, no remode."""
        job, stages, _levels = await self._run_path(
            verdict_accepted=True,
            stub_response_unavailable=True,
            stub_gain_deltas={"left": 2.0, "right": 2.0},
        )
        auto_gain = job.get("auto_gain") or {}
        self.assertEqual(
            (auto_gain.get("correction_verdict") or {}).get("step1_retained"), True
        )
        self.assertNotIn("gain_correction_after", stages)


    async def test_scans_run_at_original_level_gain_runs_at_gained_level(self):
        """Delay/Polarity scans see unconditioned levels; only Gain adapts.

        Before the gain-verification stage, no scan may observe an upward
        adapted level (the -80 dB entries are mute isolation between
        consecutive measures, not adaptation); the gain stage itself runs
        at the gained level.
        """
        _job, _stages, levels = await self._run_path(
            verdict_accepted=True, stub_gain_deltas={"left": 3.0, "right": 3.0},
        )
        first_gained = next(
            index for index, (_, _, sub1, sub2) in enumerate(levels)
            if sub1 > 0.0 or sub2 > 0.0
        )
        for stage, _active, sub1, sub2 in levels[:first_gained]:
            self.assertLessEqual(sub1, 0.0, f"{stage} must not observe adapted gain")
            self.assertLessEqual(sub2, 0.0, f"{stage} must not observe adapted gain")
        gained_stage = levels[first_gained][0]
        self.assertEqual(gained_stage, "gain_after")
        self.assertEqual(
            (levels[first_gained][2], levels[first_gained][3]), (3.0, 3.0),
            "gain_after must run at the gained level",
        )


if __name__ == "__main__":
    unittest.main()
