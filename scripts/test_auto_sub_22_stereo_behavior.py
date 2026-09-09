#!/usr/bin/env python3
"""Behavioral step-1/correction/probe contracts for the 2.2-stereo runner.

Drives the real _run_auto_sub_22_stereo_optimize with stubbed backends and
pins observable outcomes (applied state, confirmation content, verdict
payloads) instead of source text:

- mixed verdict sides keep per-side finals (accepted side from its
  gain-after sweep, rejected side from its winner) and apply per-side
  levels;
- an unavailable correction retains Step 1 without remode;
- an accepted corridor probe applies only the improved side and shows
  the corrected content, while a rejected probe keeps gain-after
  content with the rejection message;
- a fully rejected Step 1 restores the polarity snapshot (winner
  alignments), not the pristine original.
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


FREQS = [40.0 * (4.0 ** (index / 48.0)) for index in range(49)]


def _flat(value_db=0.0):
    return [[f, value_db] for f in FREQS]


def _ramp():
    return [[f, round(i * 0.15, 6)] for i, f in enumerate(FREQS)]


def _dip(depth_db):
    return [[f, (-depth_db if 14 <= i <= 30 else 0.0)] for i, f in enumerate(FREQS)]


def _diffs(points):
    levels = [float(p[1]) for p in points]
    return [round(levels[i + 1] - levels[i], 6) for i in range(len(levels) - 1)]


_UNAVAILABLE_CORRECTION = {
    "available": False,
    "reason": "Measured final Gain correction is implausible",
    "channels": {
        "left": {"response_change_per_db": 0.3},
        "right": {"response_change_per_db": 0.3},
    },
}


class AutoSub22StereoBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, *, verdict, response_correction, gain_deltas,
                   gain_after_pts="flat", correction_pts="flat"):
        """Run one stereo scenario.

        gain_after_pts/correction_pts select per-side point shapes: "flat",
        "ramp", "dip" (deep, -11 dB in-band) or "shallow" (-7 dB), either as
        one mode for both sides or {"left": ..., "right": ...}. Calculator
        diagnostics dispatch on curve content so score comparisons stay
        deterministic regardless of call order.
        """
        job_id = "auto-sub-22s-behavior"
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
        target_points = _flat()
        job = {
            "id": job_id,
            "mode": runner.OUTPUT_MODE_SUBWOOFER_22_STEREO,
            "status": "preparing",
            "cancel_requested": False,
            "target_curve": {"label": "Neutral", "points": target_points},
            "main_target_anchor": {"status": "ready", "target_vertical_offset_db": 0.0},
        }
        runner._AUTO_SUB_JOBS[job_id] = job
        recorded_stages: list[str] = []
        recorded_levels: list[tuple] = []

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

        def points_for(kind, channel):
            mode = kind.get(channel, "flat") if isinstance(kind, dict) else kind
            if mode == "ramp":
                return copy.deepcopy(_ramp())
            if mode == "dip":
                return copy.deepcopy(_dip(11.0))
            if mode == "shallow":
                return copy.deepcopy(_dip(7.0))
            return copy.deepcopy(_flat())

        async def apply_candidate(*, output_mode, global_config, subwoofers_config, verify, load_overview=None):
            persist(output_mode, global_config, subwoofers_config)
            return bool(verify((load_overview or (lambda: copy.deepcopy(state)))()))

        async def measure_candidate(**kwargs):
            stage = str(kwargs.get("stage", ""))
            recorded_stages.append(stage)
            recorded_levels.append((
                stage,
                float(state["subwoofers"]["sub1"]["level_db"]),
                float(state["subwoofers"]["sub2"]["level_db"]),
            ))
            channel = kwargs.get("measure_channel", "")
            if "gain_correction_after" in stage:
                pts = points_for(correction_pts, channel)
            elif stage == "gain_after":
                pts = points_for(gain_after_pts, channel)
            else:
                pts = copy.deepcopy(_flat())
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

        def calc_dispatch(**kwargs):
            # Deep-dip curves (gain-after in probe runs) report a large
            # remaining error; everything else reports a small one. Content
            # dispatch keeps the score comparison deterministic regardless
            # of call order.
            curves = kwargs.get("winner_curves") or {}
            deep = any(
                min((float(p[1]) for p in (curves.get(side) or [])), default=0.0) < -9.0
                for side in ("left", "right")
            )
            left, right = (2.0, 2.0) if deep else (0.5, 0.5)
            return gain_diagnostics(left, right)

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
                stack.enter_context(patch.object(runner, "_calculate_auto_sub_gain", side_effect=calc_dispatch))
                stack.enter_context(patch.object(runner, "_auto_sub_gain_deltas", return_value=gain_deltas))
                stack.enter_context(patch.object(runner, "_auto_sub_gain_verdict", return_value=verdict))
                stack.enter_context(patch.object(runner, "_auto_sub_gain_response_correction", return_value=response_correction))
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
        self.assertEqual(job["status"], "completed", job.get("error"))
        return job, state, recorded_stages, recorded_levels

    @staticmethod
    def _trace(traces, role):
        matches = [t for t in traces if t.get("role") == role]
        assert matches, f"missing {role} trace"
        return matches[0]

    async def test_mixed_verdict_keeps_per_side_finals(self):
        """Accepted left shows gain-after content, rejected right its winner."""
        job, state, _stages, _levels = await self._run(
            verdict={
                "accepted": True, "reason": "test",
                "channels": {"left": {"accepted": True}, "right": {"accepted": False}},
            },
            response_correction=dict(_UNAVAILABLE_CORRECTION),
            gain_deltas={"left": 2.0, "right": -1.0},
            gain_after_pts="ramp",
        )
        result = job["result"]
        confirmation = result.get("confirmation_measurement")
        self.assertIsNotNone(confirmation)
        left = self._trace(confirmation.get("traces", []), "left")
        right = self._trace(confirmation.get("traces", []), "right")
        self.assertEqual(
            _diffs(left["points"]), _diffs(_ramp()),
            "accepted left must show the gain-after ramp, not a scan",
        )
        self.assertEqual(
            _diffs(right["points"]), _diffs(_flat()),
            "rejected right must show its flat winner",
        )
        self.assertEqual(state["subwoofers"]["sub1"]["level_db"], 2.0)
        self.assertEqual(state["subwoofers"]["sub2"]["level_db"], 0.0)

    async def test_unavailable_correction_retains_step1(self):
        """An unavailable correction keeps Step 1 without remode."""
        job, state, stages, _levels = await self._run(
            verdict={"accepted": True, "reason": "test",
                     "channels": {"left": {"accepted": True}, "right": {"accepted": True}}},
            response_correction=dict(_UNAVAILABLE_CORRECTION),
            gain_deltas={"left": 2.0, "right": -1.0},
        )
        auto_gain = job.get("auto_gain") or {}
        self.assertEqual(
            (auto_gain.get("correction_verdict") or {}).get("step1_retained"), True
        )
        self.assertNotIn("gain_correction_after", stages)
        self.assertEqual(state["subwoofers"]["sub1"]["level_db"], 2.0)
        self.assertEqual(state["subwoofers"]["sub2"]["level_db"], -1.0)

    async def test_accepted_probe_applies_improved_side_only(self):
        """An accepted corridor probe applies only the improved side."""
        job, state, _stages, _levels = await self._run(
            verdict={
                "accepted": True, "reason": "test",
                "channels": {"left": {"accepted": True}, "right": {"accepted": True}},
            },
            response_correction=dict(_UNAVAILABLE_CORRECTION),
            gain_deltas={"left": 2.0, "right": 2.0},
            gain_after_pts={"left": "dip", "right": "flat"},
            correction_pts="shallow",
        )
        auto_gain = job.get("auto_gain") or {}
        verdict = auto_gain.get("correction_verdict") or {}
        self.assertTrue(verdict.get("accepted"))
        channels = verdict.get("channels") or {}
        self.assertTrue((channels.get("left") or {}).get("accepted"))
        self.assertFalse((channels.get("right") or {}).get("accepted"))
        result = job.get("result") or {}
        confirmation = result.get("confirmation_measurement")
        self.assertIsNotNone(confirmation)
        left = self._trace(confirmation.get("traces", []), "left")
        self.assertEqual(
            _diffs(left["points"]), _diffs(_dip(7.0)),
            "accepted left must show the corrected content, not gain-after",
        )

    async def test_rejected_probe_keeps_gain_after_content(self):
        """A rejected probe keeps gain-after content with the rejection message."""
        job, _state, _stages, _levels = await self._run(
            verdict={
                "accepted": True, "reason": "test",
                "channels": {"left": {"accepted": True}, "right": {"accepted": True}},
            },
            response_correction=dict(_UNAVAILABLE_CORRECTION),
            gain_deltas={"left": 2.0, "right": 2.0},
            gain_after_pts="dip",
            correction_pts="dip",
        )
        auto_gain = job.get("auto_gain") or {}
        verdict = auto_gain.get("correction_verdict") or {}
        self.assertFalse(verdict.get("accepted"))
        self.assertIn("rejected", str(verdict.get("reason", "")))
        result = job.get("result") or {}
        confirmation = result.get("confirmation_measurement")
        self.assertIsNotNone(confirmation)
        left = self._trace(confirmation.get("traces", []), "left")
        self.assertEqual(
            _diffs(left["points"]), _diffs(_dip(11.0)),
            "rejected probe must leave the gain-after content in place",
        )

    async def test_scans_run_at_original_level_gain_runs_at_gained_level(self):
        """Delay/Polarity scans see unconditioned levels; only Gain adapts.

        Before the gain-verification stage, no scan may observe an upward
        adapted level (muted entries are isolation, not adaptation); the
        gain stage itself runs at the gained level.
        """
        _job, _state, _stages, levels = await self._run(
            verdict={"accepted": True, "reason": "test", "channels": {}},
            response_correction=dict(_UNAVAILABLE_CORRECTION),
            gain_deltas={"left": 3.0, "right": 3.0},
        )
        first_gained = next(
            index for index, (_, sub1, sub2) in enumerate(levels)
            if sub1 > 0.0 or sub2 > 0.0
        )
        for stage, sub1, sub2 in levels[:first_gained]:
            self.assertLessEqual(sub1, 0.0, f"{stage} must not observe adapted gain")
            self.assertLessEqual(sub2, 0.0, f"{stage} must not observe adapted gain")
        gained_stage = levels[first_gained][0]
        self.assertEqual(gained_stage, "gain_after")
        self.assertEqual(
            (levels[first_gained][1], levels[first_gained][2]), (3.0, 3.0),
            "gain_after must run at the gained level",
        )

    async def test_rejected_step1_restores_polarity_snapshot(self):
        """A fully rejected Step 1 restores winner alignments, not pristine zeros."""
        job, state, _stages, _levels = await self._run(
            verdict={
                "accepted": False, "reason": "test",
                "channels": {"left": {"accepted": False}, "right": {"accepted": False}},
            },
            response_correction=dict(_UNAVAILABLE_CORRECTION),
            gain_deltas={"left": 2.0, "right": -1.0},
        )
        self.assertEqual(
            state["subwoofers"]["sub1"]["alignment_ms"], -2.14,
            "rollback must restore the winner alignment, not the original",
        )
        self.assertEqual(
            state["subwoofers"]["sub2"]["alignment_ms"], -2.54,
            "rollback must restore the winner alignment, not the original",
        )


if __name__ == "__main__":
    unittest.main()
