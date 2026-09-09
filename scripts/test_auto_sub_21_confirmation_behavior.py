#!/usr/bin/env python3
"""Behavioral confirmation contracts for the 2.1 runner.

The After/Confirmation measurement must be built from the final measured
sweep (here: the polarity-refined winner living outside the scanned delay
pools) even when the gain verdict is rejected. Selecting the confirmation
by verdict alone dropped the After curve whenever a polarity refinement
moved the winner pair outside the pools (observed as a graph with only
the Before/Baseline curves).

Drives the real _run_auto_sub_optimize with stubbed backends: scan stages
measure flat (valid) sweeps while only the polarity-refinement sweeps
carry a distinctive ramp. A verdict-gated selection would therefore show
flat consecutive diffs (or nothing); the committed behavior shows the
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
from measurement.autosub.runners import optimize as runner


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


class AutoSub21ConfirmationBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def _run_path(self, gate_veto=False, recheck_outcome="incumbent_kept", wrong_mode=False,
                                         stub_correction_unavailable=False, stub_gain_deltas="__unset__",
                                         stub_verdict_accepted=False):
        recorded_stages: list[str] = []
        recorded_levels: list[tuple] = []
        job_id = "auto-sub-21-confirmation"
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
        job = {
            "id": job_id,
            "mode": runner.OUTPUT_MODE_SUBWOOFER_21,
            "status": "preparing",
            "cancel_requested": False,
            "target_curve": {"label": "Neutral", "points": _flat()},
            "main_target_anchor": {"status": "ready", "target_vertical_offset_db": 0.0},
        }
        runner._AUTO_SUB_JOBS[job_id] = job

        def overview():
            snapshot = copy.deepcopy(state)
            if wrong_mode:
                snapshot["mode"] = "stereo"
            return snapshot

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
            recorded_stages.append(str(kwargs.get("stage", "")))
            recorded_levels.append(
                (str(kwargs.get("stage", "")), float(state["subwoofer"]["sub_level_db"]))
            )
            stage = str(kwargs.get("stage", ""))
            pts = _ramp() if "polarity_fine" in stage else _flat()
            persist(kwargs.get("output_mode", runner.OUTPUT_MODE_SUBWOOFER_21), {
                "crossover_frequency_hz": kwargs["fc"],
                "main_highpass_enabled": kwargs["original_highpass"],
                "sub_alignment_ms": kwargs["delay_ms"],
                "sub_level_db": kwargs["original_level"],
                "sub_polarity": kwargs["original_polarity"],
            })
            p = copy.deepcopy(pts)
            return {
                "delay_ms": float(kwargs["delay_ms"]),
                "name": f'{kwargs["delay_ms"]:.2f}',
                "status": "completed", "status_left": "completed", "status_right": "completed",
                "scan": kwargs["stage"],
                "points": copy.deepcopy(p),
                "points_left": copy.deepcopy(p), "points_right": copy.deepcopy(p),
                "calibrated_points_left": copy.deepcopy(p),
                "calibrated_points_right": copy.deepcopy(p),
                "normalized_by_db_left": 0.0, "normalized_by_db_right": 0.0,
                "combined_candidate": True,
                "stage_output_peaks": {"stage": kwargs["stage"]},
            }

        def score_candidates(rows, **_kwargs):
            results = []
            for row in rows:
                delay = round(float(row.get("delay_ms", 0.0)), 2)
                score = 0.8 if delay == -3.12 else 0.2
                results.append({
                    **row, "score": score, "final_score": score,
                    "score_pct": score * 100.0,
                    "xo_score": score, "timing_band_score": score,
                    "low_guard_loss_db": 0.0, "low_guard_penalty": 0.0,
                    "score_L": score, "score_R": score,
                    "score_L_pct": score * 100.0, "score_R_pct": score * 100.0,
                })
            results.sort(key=lambda row: row["score"], reverse=True)
            return {
                "winner": results[0],
                "runner_up": results[1] if len(results) > 1 else None,
                "results": results, "scored_candidates": rows, "confidence": "clear",
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

        async def restore_apply_candidate(*, output_mode, global_config, subwoofers_config, verify, load_overview=None):
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
                    "accepted": True, "score_gain": 0.5, "min_score_gain": 0.03,
                    "reason": "test-gate",
                }))
                stack.enter_context(patch.object(runner, "_auto_sub_select_polarity_shared_winner", return_value={
                    "accepted": True, "alternative_delay_ms": 2.0,
                    "score_gain": 0.5, "reason": "test-shared",
                }))
                stack.enter_context(patch.object(runner, "_calculate_auto_sub_gain", return_value=copy.deepcopy(gain_diagnostics)))
                stack.enter_context(patch.object(runner, "_auto_sub_gain_deltas", return_value={} if stub_gain_deltas == "__unset__" else stub_gain_deltas))
                if stub_correction_unavailable:
                    stack.enter_context(patch.object(runner, "_auto_sub_gain_response_correction", return_value={
                        "available": False, "reason": "test-unavailable", "channels": {},
                    }))
                stack.enter_context(patch.object(runner, "_auto_sub_gain_verdict", return_value={
                    "accepted": stub_verdict_accepted, "reason": "test", "channels": {},
                }))
                stack.enter_context(patch.object(runner, "_auto_sub_local_dip_db", return_value=5.0))
                stack.enter_context(patch.object(runner, "_auto_sub_dip_guard_should_veto",
                    return_value=(gate_veto, {"failed_sides": ["left"] if gate_veto else [],
                                              "reason": "combined_deterioration" if gate_veto else "no_per_side_trigger"})))
                stack.enter_context(patch.object(runner, "_auto_sub_local_dip_recheck_decision",
                    return_value={"outcome": recheck_outcome, "incumbent_passed": recheck_outcome == "incumbent_kept",
                                  "evidence_available": True, "incumbent_evidence_available": True,
                                  "confirmed_failed_sides": []}))
                stack.enter_context(patch.object(runner, "_auto_sub_apply_candidate", side_effect=apply_candidate))
                stack.enter_context(patch("measurement.autosub.candidates._auto_sub_apply_candidate", side_effect=restore_apply_candidate))
                stack.enter_context(patch.object(runner, "_finish_auto_sub_worker", side_effect=finish_worker))
                stack.enter_context(patch.object(runner, "get_audio_output_overview", side_effect=overview))
                stack.enter_context(patch.object(samplerate, "set_audio_output_mode", side_effect=persist))
                stack.enter_context(patch.object(samplerate, "_load_audio_output_mode", side_effect=overview))
                stack.enter_context(patch("measurement.session._resolve_measurement_start_sample_rate", return_value=48000))

                await runner._run_auto_sub_optimize(
                    job_id=job_id, input_id="test-input", channel="left",
                    mic_input_channel="1", reference_input_channel="",
                    calibration_ref="", calibration_filename=None, calibration_bytes=None,
                    scan_delays=[-3.12, 0.0], fc=80, current_alignment=0.0,
                    original_polarity="normal", original_level=0.0,
                    original_highpass=True, original_config_snapshot=original,
                )
        finally:
            runner._AUTO_SUB_JOBS.pop(job_id, None)
        return job, recorded_stages, recorded_levels

    async def test_rejected_verdict_still_shows_final_sweep(self):
        """The After curve comes from the refined winner, not the verdict."""
        job, _stages, _levels = await self._run_path()
        self.assertEqual(job["status"], "completed", job.get("error"))
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
        self.assertEqual(job["status"], "completed", job.get("error"))
        result = job["result"]
        self.assertIsNotNone(result.get("baseline_measurement"))
        self.assertIsNotNone(result.get("confirmation_measurement"))
        self.assertTrue(
            str(result["confirmation_measurement"].get("name", "")).startswith("AutoSub After ("),
            result["confirmation_measurement"].get("name"),
        )

    async def test_final_peaks_come_from_final_sweep(self):
        """The persisted peaks describe the final sweep, not a stale stage."""
        job, _stages, _levels = await self._run_path()
        self.assertEqual(job["status"], "completed", job.get("error"))
        peaks = (job.get("auto_gain") or {}).get("stage_output_peaks")
        self.assertEqual(peaks, {"stage": "polarity_fine"})

    async def test_unavailable_correction_retains_step1_without_remode(self):
        """An unavailable correction keeps Step 1: no re-verdict, no remode."""
        job, stages, _levels = await self._run_path(
            stub_verdict_accepted=True,
            stub_correction_unavailable=True, stub_gain_deltas={"left": 3.0, "right": 3.0}
        )
        self.assertEqual(job["status"], "completed", job.get("error"))
        auto_gain = job.get("auto_gain") or {}
        self.assertEqual(
            (auto_gain.get("correction_verdict") or {}).get("step1_retained"), True
        )
        self.assertEqual(auto_gain.get("correction_deltas_db"), {})
        self.assertEqual(auto_gain.get("final_deltas_db"), {"left": 3.0, "right": 3.0})
        self.assertNotIn("gain_correction_after", stages)

    async def test_final_recommit_verifies_complete_mode_state(self):
        """A mode-mismatched live state must fail the winner recommit.

        The authoritative recommit verifies the complete mode state, not
        just the alignment value, before keeping the final outcome.
        """
        job, _stages, _levels = await self._run_path(
            gate_veto=True, recheck_outcome="final_kept", wrong_mode=True
        )
        self.assertEqual(job["status"], "failed", job.get("error"))
        self.assertEqual(
            (job.get("confirmation_gate") or {}).get("action"),
            "winner_recommit_failed",
        )

    async def test_gate_revert_reports_original_gain_state(self):
        """A vetoed gate must refresh the gain diagnostics to the committed state.

        Post-mortem of run b3bdd634dbfe read a stale applied level from the
        persisted snapshot while the gate had restored the original state.
        """
        job, _stages, _levels = await self._run_path(gate_veto=True)
        self.assertEqual(job["status"], "completed", job.get("error"))
        auto_gain = job.get("auto_gain") or {}
        self.assertEqual(auto_gain.get("final_level_db"), 0.0)
        self.assertEqual(auto_gain.get("final_deltas_db"), {"left": 0.0, "right": 0.0})


    async def test_scans_run_at_original_level_gain_runs_at_gained_level(self):
        """Delay/Polarity scans see unconditioned levels; only Gain adapts.

        All alignment-scan stages must measure at the original level while
        the gain-verification stage measures at the gained level.
        """
        job, _stages, levels = await self._run_path(
            stub_gain_deltas={"left": 3.0, "right": 3.0}
        )
        self.assertEqual(job["status"], "completed", job.get("error"))
        gained = [stage for stage, level in levels if level != 0.0]
        self.assertTrue(gained, "the gain stage must actually run gained")
        for stage, level in levels:
            if stage == "gain_after":
                self.assertEqual(level, 3.0, f"{stage} must run at the gained level")
            else:
                self.assertEqual(level, 0.0, f"{stage} must run at the original level")


if __name__ == "__main__":
    unittest.main()
