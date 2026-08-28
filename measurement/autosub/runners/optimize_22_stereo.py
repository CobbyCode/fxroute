# SPDX-License-Identifier: AGPL-3.0-only

"""The 2.2 stereo-bass AutoSub optimize runner."""

from __future__ import annotations

import asyncio
import copy
import json
import logging

from audio.samplerate import (
    OUTPUT_MODE_SUBWOOFER_22_STEREO,
    get_audio_output_overview,
    set_audio_output_mode,
)
from dsp.runtime import BassManagementConfig
from measurement.store import score_sub_alignment_candidates
from typing import Any
from uuid import uuid4
from ..candidates import (
    _auto_sub_22_candidate_subwoofers,
    _auto_sub_22_global_config,
    _auto_sub_22_stereo_name,
    _auto_sub_22_sub,
    _auto_sub_22_verify_alignment,
    _auto_sub_apply_candidate,
    _auto_sub_clamped_delay,
    _auto_sub_fine_delay_candidates,
    _auto_sub_opposite_polarity,
    _auto_sub_polarity_decision,
    _restore_auto_sub_original_config,
    _auto_sub_snapshot_copy,
    _auto_sub_step_ms,
    _auto_sub_sweep_profile,
)
from ..deps import (
    _AUTO_SUB_JOBS,
    _auto_sub_cancel_requested,
    _auto_sub_lock,
    _dsp_runtime,
    _measurement_session,
)
from ..jobs import (
    _finish_auto_sub_worker,
    _log_auto_sub_timing_summary,
)
from ..measurement import (
    _auto_sub_22_snapshot_with_gain,
    _auto_sub_gain_deltas,
    _auto_sub_gain_log_line,
    _auto_sub_gain_log_score,
    _auto_sub_gain_response_correction,
    _auto_sub_gain_verdict,
    _auto_sub_stereo_corridor_violation,
    _auto_sub_stereo_probe_plan,
    _calculate_auto_sub_gain,
    _capture_auto_sub_main_references,
    _measure_auto_sub_candidate,
)
from ..scoring import (
    _auto_sub_best_scan_result,
    _auto_sub_candidate_ledger,
    _auto_sub_delay_key,
    _auto_sub_has_points,
    _auto_sub_rank_results,
    _auto_sub_result_for_delay,
    _auto_sub_select_accepted_winner,
    _auto_sub_shared_bass_offset,
)

logger = logging.getLogger(__name__)


async def _run_auto_sub_22_stereo_optimize(
    job_id: str,
    input_id: str,
    mic_input_channel: str,
    reference_input_channel: str,
    calibration_ref: str,
    calibration_filename: str | None,
    calibration_bytes: bytes | None,
    left_scan_delays: list[float],
    right_scan_delays: list[float],
    fc: int,
    original_config_snapshot: dict[str, Any],
    entry_epoch: int | None = None,
) -> None:
    measurement_sr_session = _measurement_session()
    from measurement.session import (
        MeasurementEntryInvalidated,
        _resolve_measurement_start_sample_rate,
    )
    global _auto_sub_lock
    from audio.samplerate import _load_audio_output_mode, set_audio_output_mode

    job = _AUTO_SUB_JOBS.get(job_id)
    if not job:
        _auto_sub_lock.release()
        return

    async def _restore_original_config() -> None:
        await _restore_auto_sub_original_config(original_config_snapshot)

    original_left = _auto_sub_22_sub(original_config_snapshot, "sub1")
    original_right = _auto_sub_22_sub(original_config_snapshot, "sub2")
    original_left_alignment = float(original_left.get("alignment_ms", 0.0) or 0.0)
    original_right_alignment = float(original_right.get("alignment_ms", 0.0) or 0.0)

    def _valid(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [result for result in results if _auto_sub_has_points(result, "points")]

    def _reference_points(results: list[dict[str, Any]], delay_ms: float) -> list[list[float]] | None:
        valid = _valid(results)
        if not valid:
            return None
        reference = min(valid, key=lambda result: abs(float(result.get("delay_ms", 0.0) or 0.0) - delay_ms))
        points = reference.get("points") or []
        return points if isinstance(points, list) and len(points) >= 3 else None

    try:
        if measurement_sr_session is not None:
            try:
                await measurement_sr_session.register_auto_sub(job_id, entry_epoch=entry_epoch)
            except MeasurementEntryInvalidated:
                logger.info(
                    "AUTOSUB job=%s entry invalidated by measurement window close",
                    job_id,
                )
                job["status"] = "cancelled"
                job["message"] = "Auto Sub Optimize cancelled because the measurement window was closed."
                return
        if _auto_sub_cancel_requested(job):
            logger.info("AUTOSUB job=%s cancel observed (before sweeps)", job_id)
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return

        auto_sub_sweep_profile = _auto_sub_sweep_profile(fc)
        auto_sub_rate = _resolve_measurement_start_sample_rate()
        await _capture_auto_sub_main_references(
            job=job, fc=fc, input_id=input_id,
            mic_input_channel=mic_input_channel, reference_input_channel=reference_input_channel,
            calibration_ref=calibration_ref, calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
            auto_sub_rate=auto_sub_rate, output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
            original_config_snapshot=original_config_snapshot,
        )
        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return
        step_ms = _auto_sub_step_ms(fc)
        planned_left_fine_total = 6
        planned_right_fine_total = 6
        planned_sweep_total = (
            len(left_scan_delays)
            + planned_left_fine_total
            + len(right_scan_delays)
            + planned_right_fine_total
        )

        left_results: list[dict[str, Any]] = []
        job["stage"] = "left_sub"
        for idx, delay_ms in enumerate(left_scan_delays):
            sweep_index = idx + 1
            left_results.append(await _measure_auto_sub_candidate(
                delay_ms=delay_ms,
                job=job,
                candidate_index=sweep_index,
                total=planned_sweep_total,
                stage="left_sub",
                fc=fc,
                input_id=input_id,
                channel="left",
                mic_input_channel=mic_input_channel,
                reference_input_channel=reference_input_channel,
                calibration_ref=calibration_ref,
                calibration_filename=calibration_filename,
                calibration_bytes=calibration_bytes,
                auto_sub_sweep_profile=auto_sub_sweep_profile,
                auto_sub_rate=auto_sub_rate,
                original_level=0.0,
                original_polarity="normal",
                original_highpass=True,
                measurement_label=f"Optimizing Left Sub: L sweep {idx + 1}/{len(left_scan_delays)} @ {delay_ms:.2f} ms",
                candidate_current=idx + 1,
                candidate_total=len(left_scan_delays),
                measure_channel="left",
                output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
                original_config_snapshot=original_config_snapshot,
                sub1_alignment_ms=delay_ms,
                sub2_alignment_ms=original_right_alignment,
                active_subs=("sub1",),
            ))
            if isinstance(job.get("progress"), dict):
                job["progress"]["sweep_current"] = sweep_index
                job["progress"]["sweep_total"] = planned_sweep_total
            if _auto_sub_cancel_requested(job):
                job["message"] = "Auto Sub Optimize cancelled."
                await _restore_original_config()
                return

        left_valid = _valid(left_results)
        if not left_valid:
            job["status"] = "failed"
            job["message"] = "No valid Left Sub sweep results to score"
            job["error"] = {"detail": "Left Sub sweeps failed or produced insufficient data"}
            await _restore_original_config()
            return
        left_coarse_scoring = score_sub_alignment_candidates(
            left_valid,
            crossover_hz=fc,
            low_guard_reference_delay_ms=original_left_alignment,
        )
        _auto_sub_rank_results(left_coarse_scoring["results"])
        left_coarse_winner = left_coarse_scoring["winner"]
        left_coarse_runner_up = left_coarse_scoring.get("runner_up")
        left_fine_delays = _auto_sub_fine_delay_candidates(
            left_coarse_winner,
            left_coarse_runner_up,
            step_ms,
            {round(float(delay), 2) for delay in left_scan_delays},
        )
        left_fine_results: list[dict[str, Any]] = []
        left_fine_valid: list[dict[str, Any]] = []
        left_fine_scoring: dict[str, Any] | None = None
        left_fine_winner: dict[str, Any] | None = None
        left_low_guard_reference_points = _reference_points(left_valid, original_left_alignment)
        job["fine_scan"] = {
            "enabled": True,
            "triggered": bool(left_fine_delays),
            "status": "left_running" if left_fine_delays else "left_skipped",
            "fine_step_ms": step_ms / 4.0,
            "left": {
                "status": "running" if left_fine_delays else "skipped",
                "coarse_winner": left_coarse_winner,
                "coarse_runner_up": left_coarse_runner_up,
                "candidates": left_fine_delays,
            },
            "right": {"status": "pending", "candidates": []},
        }
        if left_fine_delays:
            job["stage"] = "left_fine"
            for idx, delay_ms in enumerate(left_fine_delays):
                sweep_index = len(left_scan_delays) + idx + 1
                left_fine_results.append(await _measure_auto_sub_candidate(
                    delay_ms=delay_ms,
                    job=job,
                    candidate_index=sweep_index,
                    total=planned_sweep_total,
                    stage="left_fine",
                    fc=fc,
                    input_id=input_id,
                    channel="left",
                    mic_input_channel=mic_input_channel,
                    reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref,
                    calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes,
                    auto_sub_sweep_profile=auto_sub_sweep_profile,
                    auto_sub_rate=auto_sub_rate,
                    original_level=0.0,
                    original_polarity="normal",
                    original_highpass=True,
                    measurement_label=f"Optimizing Left Sub Fine: L sweep {idx + 1}/{len(left_fine_delays)} @ {delay_ms:.2f} ms",
                    candidate_current=idx + 1,
                    candidate_total=len(left_fine_delays),
                    measure_channel="left",
                    output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
                    original_config_snapshot=original_config_snapshot,
                    sub1_alignment_ms=delay_ms,
                    sub2_alignment_ms=original_right_alignment,
                    active_subs=("sub1",),
                ))
                if isinstance(job.get("progress"), dict):
                    job["progress"]["sweep_current"] = sweep_index
                    job["progress"]["sweep_total"] = planned_sweep_total
                if _auto_sub_cancel_requested(job):
                    job["message"] = "Auto Sub Optimize cancelled."
                    await _restore_original_config()
                    return
            left_fine_valid = _valid(left_fine_results)
            if left_fine_valid:
                left_fine_scoring = score_sub_alignment_candidates(
                    left_fine_valid,
                    crossover_hz=fc,
                    low_guard_reference_points=left_low_guard_reference_points,
                    low_guard_reference_delay_ms=original_left_alignment,
                )
                _auto_sub_rank_results(left_fine_scoring["results"])
                left_fine_winner = left_fine_scoring["winner"]
                job["fine_scan"]["left"].update({
                    "status": "completed",
                    "winner": left_fine_winner,
                    "runner_up": left_fine_scoring.get("runner_up"),
                    "results": left_fine_scoring["results"],
                    "valid_count": len(left_fine_valid),
                    "sweep_count": len(left_fine_delays),
                })
            else:
                job["fine_scan"]["left"].update({
                    "status": "no_valid_results",
                    "winner": None,
                    "runner_up": None,
                    "results": left_fine_results,
                    "valid_count": 0,
                    "sweep_count": len(left_fine_delays),
                })

        left_final_valid = left_valid + left_fine_valid
        left_scoring = score_sub_alignment_candidates(
            left_final_valid,
            crossover_hz=fc,
            low_guard_reference_delay_ms=original_left_alignment,
        )
        _auto_sub_rank_results(left_scoring["results"])
        left_scan_by_delay: dict[float, str] = {}
        for result in left_valid:
            left_scan_by_delay[_auto_sub_delay_key(result)] = "coarse"
        for result in left_fine_valid:
            left_scan_by_delay[_auto_sub_delay_key(result)] = "fine"
        for result in left_scoring["results"]:
            result["scan"] = left_scan_by_delay.get(_auto_sub_delay_key(result), result.get("scan", "coarse"))
        left_coarse_accepted_candidate = _auto_sub_best_scan_result(left_scoring["results"], "coarse") or left_coarse_winner
        left_fine_accepted_candidate = _auto_sub_best_scan_result(left_scoring["results"], "fine")
        left_incumbent_winner = _auto_sub_result_for_delay(left_scoring["results"], original_left_alignment)
        left_acceptance = _auto_sub_select_accepted_winner(
            coarse_winner=left_coarse_accepted_candidate,
            fine_winner=left_fine_accepted_candidate,
            incumbent_winner=left_incumbent_winner,
        )
        left_winner = left_acceptance["accepted_winner"]
        best_left = _auto_sub_clamped_delay(float(left_winner.get("delay_ms", original_left_alignment) or original_left_alignment))
        job["fine_scan"]["left"]["final_winner"] = left_winner
        job["fine_scan"]["left"]["final_results"] = left_scoring["results"]
        job["fine_scan"]["left"]["accepted_winner"] = left_winner
        job["fine_scan"]["left"]["fine_accepted"] = left_acceptance["fine_accepted"]
        job["fine_scan"]["left"]["reject_reason"] = left_acceptance["reject_reason"]
        job["fine_scan"]["left"]["incumbent_winner"] = left_incumbent_winner
        job["fine_scan"]["left"]["incumbent_score"] = left_acceptance["incumbent_score"]
        job["fine_scan"]["status"] = "right_pending"

        right_results: list[dict[str, Any]] = []
        job["stage"] = "right_sub"
        for idx, delay_ms in enumerate(right_scan_delays):
            sweep_index = len(left_scan_delays) + len(left_fine_delays) + idx + 1
            right_results.append(await _measure_auto_sub_candidate(
                delay_ms=delay_ms,
                job=job,
                candidate_index=sweep_index,
                total=planned_sweep_total,
                stage="right_sub",
                fc=fc,
                input_id=input_id,
                channel="right",
                mic_input_channel=mic_input_channel,
                reference_input_channel=reference_input_channel,
                calibration_ref=calibration_ref,
                calibration_filename=calibration_filename,
                calibration_bytes=calibration_bytes,
                auto_sub_sweep_profile=auto_sub_sweep_profile,
                auto_sub_rate=auto_sub_rate,
                original_level=0.0,
                original_polarity="normal",
                original_highpass=True,
                measurement_label=f"Optimizing Right Sub: R sweep {idx + 1}/{len(right_scan_delays)} @ {delay_ms:.2f} ms",
                candidate_current=idx + 1,
                candidate_total=len(right_scan_delays),
                measure_channel="right",
                output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
                original_config_snapshot=original_config_snapshot,
                sub1_alignment_ms=best_left,
                sub2_alignment_ms=delay_ms,
                active_subs=("sub2",),
            ))
            if isinstance(job.get("progress"), dict):
                job["progress"]["sweep_current"] = sweep_index
                job["progress"]["sweep_total"] = planned_sweep_total
            if _auto_sub_cancel_requested(job):
                job["message"] = "Auto Sub Optimize cancelled."
                await _restore_original_config()
                return

        right_valid = _valid(right_results)
        if not right_valid:
            job["status"] = "failed"
            job["message"] = "No valid Right Sub sweep results to score"
            job["error"] = {"detail": "Right Sub sweeps failed or produced insufficient data"}
            await _restore_original_config()
            return
        right_coarse_scoring = score_sub_alignment_candidates(
            right_valid,
            crossover_hz=fc,
            low_guard_reference_delay_ms=original_right_alignment,
        )
        _auto_sub_rank_results(right_coarse_scoring["results"])
        right_coarse_winner = right_coarse_scoring["winner"]
        right_coarse_runner_up = right_coarse_scoring.get("runner_up")
        right_fine_delays = _auto_sub_fine_delay_candidates(
            right_coarse_winner,
            right_coarse_runner_up,
            step_ms,
            {round(float(delay), 2) for delay in right_scan_delays},
        )
        right_fine_results: list[dict[str, Any]] = []
        right_fine_valid: list[dict[str, Any]] = []
        right_fine_scoring: dict[str, Any] | None = None
        right_fine_winner: dict[str, Any] | None = None
        right_low_guard_reference_points = _reference_points(right_valid, original_right_alignment)
        actual_sweep_total = (
            len(left_scan_delays)
            + len(left_fine_delays)
            + len(right_scan_delays)
            + len(right_fine_delays)
        )
        job["fine_scan"].update({
            "triggered": bool(left_fine_delays or right_fine_delays),
            "status": "right_running" if right_fine_delays else "right_skipped",
        })
        job["fine_scan"]["right"] = {
            "status": "running" if right_fine_delays else "skipped",
            "coarse_winner": right_coarse_winner,
            "coarse_runner_up": right_coarse_runner_up,
            "candidates": right_fine_delays,
        }
        if right_fine_delays:
            job["stage"] = "right_fine"
            for idx, delay_ms in enumerate(right_fine_delays):
                sweep_index = len(left_scan_delays) + len(left_fine_delays) + len(right_scan_delays) + idx + 1
                right_fine_results.append(await _measure_auto_sub_candidate(
                    delay_ms=delay_ms,
                    job=job,
                    candidate_index=sweep_index,
                    total=actual_sweep_total,
                    stage="right_fine",
                    fc=fc,
                    input_id=input_id,
                    channel="right",
                    mic_input_channel=mic_input_channel,
                    reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref,
                    calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes,
                    auto_sub_sweep_profile=auto_sub_sweep_profile,
                    auto_sub_rate=auto_sub_rate,
                    original_level=0.0,
                    original_polarity="normal",
                    original_highpass=True,
                    measurement_label=f"Optimizing Right Sub Fine: R sweep {idx + 1}/{len(right_fine_delays)} @ {delay_ms:.2f} ms",
                    candidate_current=idx + 1,
                    candidate_total=len(right_fine_delays),
                    measure_channel="right",
                    output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
                    original_config_snapshot=original_config_snapshot,
                    sub1_alignment_ms=best_left,
                    sub2_alignment_ms=delay_ms,
                    active_subs=("sub2",),
                ))
                if isinstance(job.get("progress"), dict):
                    job["progress"]["sweep_current"] = sweep_index
                    job["progress"]["sweep_total"] = actual_sweep_total
                if _auto_sub_cancel_requested(job):
                    job["message"] = "Auto Sub Optimize cancelled."
                    await _restore_original_config()
                    return
            right_fine_valid = _valid(right_fine_results)
            if right_fine_valid:
                right_fine_scoring = score_sub_alignment_candidates(
                    right_fine_valid,
                    crossover_hz=fc,
                    low_guard_reference_points=right_low_guard_reference_points,
                    low_guard_reference_delay_ms=original_right_alignment,
                )
                _auto_sub_rank_results(right_fine_scoring["results"])
                right_fine_winner = right_fine_scoring["winner"]
                job["fine_scan"]["right"].update({
                    "status": "completed",
                    "winner": right_fine_winner,
                    "runner_up": right_fine_scoring.get("runner_up"),
                    "results": right_fine_scoring["results"],
                    "valid_count": len(right_fine_valid),
                    "sweep_count": len(right_fine_delays),
                })
            else:
                job["fine_scan"]["right"].update({
                    "status": "no_valid_results",
                    "winner": None,
                    "runner_up": None,
                    "results": right_fine_results,
                    "valid_count": 0,
                    "sweep_count": len(right_fine_delays),
                })

        right_final_valid = right_valid + right_fine_valid
        right_scoring = score_sub_alignment_candidates(
            right_final_valid,
            crossover_hz=fc,
            low_guard_reference_delay_ms=original_right_alignment,
        )
        _auto_sub_rank_results(right_scoring["results"])
        right_scan_by_delay: dict[float, str] = {}
        for result in right_valid:
            right_scan_by_delay[_auto_sub_delay_key(result)] = "coarse"
        for result in right_fine_valid:
            right_scan_by_delay[_auto_sub_delay_key(result)] = "fine"
        for result in right_scoring["results"]:
            result["scan"] = right_scan_by_delay.get(_auto_sub_delay_key(result), result.get("scan", "coarse"))
        right_coarse_accepted_candidate = _auto_sub_best_scan_result(right_scoring["results"], "coarse") or right_coarse_winner
        right_fine_accepted_candidate = _auto_sub_best_scan_result(right_scoring["results"], "fine")
        right_incumbent_winner = _auto_sub_result_for_delay(right_scoring["results"], original_right_alignment)
        right_acceptance = _auto_sub_select_accepted_winner(
            coarse_winner=right_coarse_accepted_candidate,
            fine_winner=right_fine_accepted_candidate,
            incumbent_winner=right_incumbent_winner,
        )
        right_winner = right_acceptance["accepted_winner"]
        best_right = _auto_sub_clamped_delay(float(right_winner.get("delay_ms", original_right_alignment) or original_right_alignment))
        job["fine_scan"]["right"]["final_winner"] = right_winner
        job["fine_scan"]["right"]["final_results"] = right_scoring["results"]
        job["fine_scan"]["right"]["accepted_winner"] = right_winner
        job["fine_scan"]["right"]["fine_accepted"] = right_acceptance["fine_accepted"]
        job["fine_scan"]["right"]["reject_reason"] = right_acceptance["reject_reason"]
        job["fine_scan"]["right"]["incumbent_winner"] = right_incumbent_winner
        job["fine_scan"]["right"]["incumbent_score"] = right_acceptance["incumbent_score"]
        job["fine_scan"]["status"] = "completed"

        candidate_ledger = (
            _auto_sub_candidate_ledger(
                left_results, left_scoring, mode="2.2_stereo", phase="left_coarse", channel="left",
                roles={
                    "coarse_winner": left_coarse_accepted_candidate,
                    "final_accepted_winner": left_winner,
                },
                requested_incumbent={"delay_ms": original_left_alignment},
            )
            + _auto_sub_candidate_ledger(
                left_fine_results, left_scoring, mode="2.2_stereo", phase="left_fine", channel="left",
                roles={"fine_winner": left_fine_accepted_candidate, "final_accepted_winner": left_winner},
            )
            + _auto_sub_candidate_ledger(
                right_results, right_scoring, mode="2.2_stereo", phase="right_coarse", channel="right",
                roles={
                    "coarse_winner": right_coarse_accepted_candidate,
                    "final_accepted_winner": right_winner,
                },
                requested_incumbent={"delay_ms": original_right_alignment},
            )
            + _auto_sub_candidate_ledger(
                right_fine_results, right_scoring, mode="2.2_stereo", phase="right_fine", channel="right",
                roles={"fine_winner": right_fine_accepted_candidate, "final_accepted_winner": right_winner},
            )
        )

        sub_config = _auto_sub_22_global_config(original_config_snapshot)
        subwoofers_config = _auto_sub_22_candidate_subwoofers(
            original_config_snapshot,
            sub1_alignment_ms=best_left,
            sub2_alignment_ms=best_right,
            active_subs=("sub1", "sub2"),
        )
        apply_ok = await _auto_sub_apply_candidate(
            output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
            global_config=sub_config,
            subwoofers_config=subwoofers_config,
            verify=lambda overview: _auto_sub_22_verify_alignment(overview, best_left, best_right),
            load_overview=_load_audio_output_mode,
        )

        if not apply_ok:
            job["status"] = "failed"
            job["message"] = f"Scoring succeeded but failed to apply Left/Right pair {best_left:.2f} / {best_right:.2f} ms"
            job["error"] = {"detail": "Winner apply failed - original config restored"}
            await _restore_original_config()
            return

        derived_delays: dict[str, Any] = {}
        try:
            config = BassManagementConfig.from_overview(await asyncio.to_thread(get_audio_output_overview))
            derived_delays = {
                "derived_main_delay_ms": round(config.derived_main_delay_ms, 2),
                "derived_sub1_delay_ms": round(config.derived_sub1_delay_ms, 2),
                "derived_sub2_delay_ms": round(config.derived_sub2_delay_ms, 2),
            }
        except Exception:
            derived_delays = {}

        left_score = float(left_winner.get("score", 0.0) or 0.0)
        right_score = float(right_winner.get("score", 0.0) or 0.0)
        gain_left_winner = _auto_sub_result_for_delay(list(left_results) + list(left_fine_results), best_left) or {}
        gain_right_winner = _auto_sub_result_for_delay(list(right_results) + list(right_fine_results), best_right) or {}
        selected_left_polarity = str(original_left.get("polarity", "normal"))
        selected_right_polarity = str(original_right.get("polarity", "normal"))
        stereo_polarity: dict[str, Any] = {}

        async def _check_stereo_polarity(
            side: str, incumbent: dict[str, Any], delay: float, incumbent_polarity: str,
        ) -> tuple[dict[str, Any], float, str, dict[str, Any]]:
            opposite = _auto_sub_opposite_polarity(incumbent_polarity)
            is_left = side == "left"
            alt = await _measure_auto_sub_candidate(
                delay_ms=delay, job=job, candidate_index=1, total=1, stage=f"{side}_polarity_check", fc=fc,
                input_id=input_id, channel=side, mic_input_channel=mic_input_channel,
                reference_input_channel=reference_input_channel, calibration_ref=calibration_ref,
                calibration_filename=calibration_filename, calibration_bytes=calibration_bytes,
                auto_sub_sweep_profile=auto_sub_sweep_profile, auto_sub_rate=auto_sub_rate,
                original_level=0.0, original_polarity="normal", original_highpass=True,
                measure_channel=side, output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
                original_config_snapshot=original_config_snapshot,
                sub1_alignment_ms=delay if is_left else best_left,
                sub2_alignment_ms=best_right if is_left else delay,
                active_subs=("sub1",) if is_left else ("sub2",),
                sub1_polarity=opposite if is_left else selected_left_polarity,
                sub2_polarity=selected_right_polarity if is_left else opposite,
            )
            rows = [dict(incumbent, delay_ms=0.0, points=incumbent.get("points") or []), dict(alt, delay_ms=1.0)]
            scoring = score_sub_alignment_candidates(rows, crossover_hz=fc, low_guard_reference_delay_ms=0.0)
            scored_incumbent = _auto_sub_result_for_delay(scoring["results"], 0.0) or {}
            scored_alt = _auto_sub_result_for_delay(scoring["results"], 1.0) or {}
            decision = _auto_sub_polarity_decision(scored_incumbent, scored_alt)
            decision.update({"incumbent": incumbent_polarity, "alternative": opposite, "selected": incumbent_polarity})
            if not decision["accepted"]:
                return incumbent, delay, incumbent_polarity, decision
            local_step = _auto_sub_step_ms(fc) / 4.0
            local_rows = [alt]
            for idx, candidate_delay in enumerate([
                _auto_sub_clamped_delay(delay - 2 * local_step), _auto_sub_clamped_delay(delay - local_step),
                _auto_sub_clamped_delay(delay + local_step), _auto_sub_clamped_delay(delay + 2 * local_step),
            ]):
                local_rows.append(await _measure_auto_sub_candidate(
                    delay_ms=candidate_delay, job=job, candidate_index=idx + 1, total=4,
                    stage=f"{side}_polarity_fine", fc=fc, input_id=input_id, channel=side,
                    mic_input_channel=mic_input_channel, reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref, calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
                    auto_sub_rate=auto_sub_rate, original_level=0.0, original_polarity="normal",
                    original_highpass=True, measure_channel=side,
                    output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO, original_config_snapshot=original_config_snapshot,
                    sub1_alignment_ms=candidate_delay if is_left else best_left,
                    sub2_alignment_ms=best_right if is_left else candidate_delay,
                    active_subs=("sub1",) if is_left else ("sub2",),
                    sub1_polarity=opposite if is_left else selected_left_polarity,
                    sub2_polarity=selected_right_polarity if is_left else opposite,
                ))
            fine_scoring = score_sub_alignment_candidates(local_rows, crossover_hz=fc)
            fine_winner = fine_scoring["winner"]
            selected = _auto_sub_result_for_delay(local_rows, float(fine_winner["delay_ms"])) or alt
            decision.update({"selected": opposite, "fine_scan": {"candidate_count": 4, "winner": fine_winner}})
            return selected, float(fine_winner["delay_ms"]), opposite, decision

        gain_left_winner, best_left, selected_left_polarity, stereo_polarity["left"] = await _check_stereo_polarity(
            "left", gain_left_winner, best_left, selected_left_polarity,
        )
        gain_right_winner, best_right, selected_right_polarity, stereo_polarity["right"] = await _check_stereo_polarity(
            "right", gain_right_winner, best_right, selected_right_polarity,
        )
        polarity_snapshot = _auto_sub_snapshot_copy(original_config_snapshot)
        polarity_snapshot.setdefault("subwoofers", {}).setdefault("sub1", {})["polarity"] = selected_left_polarity
        polarity_snapshot.setdefault("subwoofers", {}).setdefault("sub2", {})["polarity"] = selected_right_polarity
        job["polarity_check"] = stereo_polarity
        job["auto_gain"] = _calculate_auto_sub_gain(
            mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
            target_curve=job.get("target_curve"), anchor=job.get("main_target_anchor"),
            winner_curves={
                "left": gain_left_winner.get("calibrated_points") or [],
                "right": gain_right_winner.get("calibrated_points") or [],
            }, crossover_hz=fc,
        )
        logger.info("AUTOSUB_GAIN mode=2.2_stereo diagnostics=%s", json.dumps(job["auto_gain"], sort_keys=True))
        gain_deltas = _auto_sub_gain_deltas(job["auto_gain"], OUTPUT_MODE_SUBWOOFER_22_STEREO, max_abs_db=6.0)
        _auto_sub_gain_log_line("AUTOGAIN_INIT", {
            "mode": OUTPUT_MODE_SUBWOOFER_22_STEREO, "xo_hz": fc,
            "target": (job.get("target_curve") or {}).get("label"),
            "anchor_hz": (job.get("main_target_anchor") or {}).get("usable_band_hz"),
            "target_offset_db": (job.get("main_target_anchor") or {}).get("target_vertical_offset_db"),
            "gain_before": {"left": float(original_left.get("level_db", 0.0)), "right": float(original_right.get("level_db", 0.0))},
            "winner_delta_left": (job["auto_gain"].get("channels", {}).get("left") or {}).get("target_delta_db"),
            "winner_delta_right": (job["auto_gain"].get("channels", {}).get("right") or {}).get("target_delta_db"),
            "combined_delta_db": None, "first_step_db": gain_deltas,
        })
        gain_snapshot = _auto_sub_22_snapshot_with_gain(
            polarity_snapshot,
            left_delta_db=gain_deltas.get("left", 0.0), right_delta_db=gain_deltas.get("right", 0.0),
        )
        if gain_deltas:
            await asyncio.to_thread(
                set_audio_output_mode,
                OUTPUT_MODE_SUBWOOFER_22_STEREO, _auto_sub_22_global_config(gain_snapshot),
                _auto_sub_22_candidate_subwoofers(
                    gain_snapshot, sub1_alignment_ms=best_left, sub2_alignment_ms=best_right,
                    active_subs=("sub1", "sub2"),
                ),
            )
            if _dsp_runtime() is not None:
                await _dsp_runtime().sync(await asyncio.to_thread(get_audio_output_overview))
        gain_after_left = await _measure_auto_sub_candidate(
            delay_ms=best_left, job=job, candidate_index=1, total=2, stage="gain_after", fc=fc,
            input_id=input_id, channel="left", mic_input_channel=mic_input_channel,
            reference_input_channel=reference_input_channel, calibration_ref=calibration_ref,
            calibration_filename=calibration_filename, calibration_bytes=calibration_bytes,
            auto_sub_sweep_profile=auto_sub_sweep_profile, auto_sub_rate=auto_sub_rate,
            original_level=0.0, original_polarity="normal", original_highpass=True,
            measure_channel="left", output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
            original_config_snapshot=gain_snapshot, sub1_alignment_ms=best_left,
            sub2_alignment_ms=best_right, active_subs=("sub1", "sub2"),
        )
        gain_after_right = await _measure_auto_sub_candidate(
            delay_ms=best_right, job=job, candidate_index=2, total=2, stage="gain_after", fc=fc,
            input_id=input_id, channel="right", mic_input_channel=mic_input_channel,
            reference_input_channel=reference_input_channel, calibration_ref=calibration_ref,
            calibration_filename=calibration_filename, calibration_bytes=calibration_bytes,
            auto_sub_sweep_profile=auto_sub_sweep_profile, auto_sub_rate=auto_sub_rate,
            original_level=0.0, original_polarity="normal", original_highpass=True,
            measure_channel="right", output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
            original_config_snapshot=gain_snapshot, sub1_alignment_ms=best_left,
            sub2_alignment_ms=best_right, active_subs=("sub1", "sub2"),
        )
        gain_after = _calculate_auto_sub_gain(
            mode=OUTPUT_MODE_SUBWOOFER_22_STEREO, target_curve=job.get("target_curve"),
            anchor=job.get("main_target_anchor"), winner_curves={
                "left": gain_after_left.get("calibrated_points") or [],
                "right": gain_after_right.get("calibrated_points") or [],
            }, crossover_hz=fc,
        )
        gain_verdict = _auto_sub_gain_verdict(job["auto_gain"], gain_after, OUTPUT_MODE_SUBWOOFER_22_STEREO)
        accepted_step1_sides = {
            side: bool((gain_verdict.get("channels", {}).get(side) or {}).get("accepted"))
            for side in ("left", "right")
        }
        retained_step1_deltas = {
            side: gain_deltas.get(side, 0.0) if accepted_step1_sides[side] else 0.0
            for side in ("left", "right")
        }
        step1_retained = any(accepted_step1_sides.values())
        final_gain_deltas = retained_step1_deltas
        final_gain_snapshot = _auto_sub_22_snapshot_with_gain(
            polarity_snapshot,
            left_delta_db=retained_step1_deltas["left"],
            right_delta_db=retained_step1_deltas["right"],
        )
        final_gain_left = gain_after_left if accepted_step1_sides["left"] else gain_left_winner
        final_gain_right = gain_after_right if accepted_step1_sides["right"] else gain_right_winner
        correction_deltas: dict[str, float] = {}
        correction_plan = None
        correction_after = None
        correction_verdict = None
        stereo_probe_plan = None
        if not step1_retained:
            await asyncio.to_thread(
                set_audio_output_mode,
                OUTPUT_MODE_SUBWOOFER_22_STEREO, _auto_sub_22_global_config(polarity_snapshot),
                _auto_sub_22_candidate_subwoofers(
                    polarity_snapshot, sub1_alignment_ms=best_left, sub2_alignment_ms=best_right,
                    active_subs=("sub1", "sub2"),
                ),
            )
            if _dsp_runtime() is not None:
                await _dsp_runtime().sync(await asyncio.to_thread(get_audio_output_overview))
        elif not all(accepted_step1_sides.values()):
            # Stereo channels have independent Gain controls.  A regression on
            # one side must not discard a measured improvement on the other.
            await asyncio.to_thread(
                set_audio_output_mode,
                OUTPUT_MODE_SUBWOOFER_22_STEREO, _auto_sub_22_global_config(final_gain_snapshot),
                _auto_sub_22_candidate_subwoofers(
                    final_gain_snapshot, sub1_alignment_ms=best_left, sub2_alignment_ms=best_right,
                    active_subs=("sub1", "sub2"),
                ),
            )
            if _dsp_runtime() is not None:
                await _dsp_runtime().sync(await asyncio.to_thread(get_audio_output_overview))
            correction_verdict = {
                "accepted": False,
                "reason": "Retained improved Stereo side; restored regressed side",
                "channels": gain_verdict.get("channels", {}),
                "step1_retained": True,
            }
        else:
            correction_plan = _auto_sub_gain_response_correction(
                job["auto_gain"], gain_after, gain_deltas, OUTPUT_MODE_SUBWOOFER_22_STEREO,
            )
            correction_deltas = correction_plan.get("deltas_db") or {}
            if not correction_plan.get("available"):
                stereo_probe_plan = _auto_sub_stereo_probe_plan(
                    correction_plan=correction_plan, gain_after=gain_after, gain_deltas=gain_deltas,
                    accepted_step1_sides=accepted_step1_sides,
                    after_points={
                        "left": gain_after_left.get("calibrated_points") or [],
                        "right": gain_after_right.get("calibrated_points") or [],
                    },
                    target_curve=job.get("target_curve"), anchor=job.get("main_target_anchor"),
                    crossover_hz=fc,
                )
                correction_deltas = stereo_probe_plan.get("deltas_db") or {}
                if not stereo_probe_plan.get("available"):
                    correction_verdict = {
                        "accepted": False,
                        "reason": correction_plan.get("reason"),
                        "channels": {},
                        "step1_retained": True,
                    }
            if any(abs(value) > 0.0005 for value in correction_deltas.values()):
                correction_snapshot = _auto_sub_22_snapshot_with_gain(
                    gain_snapshot, left_delta_db=correction_deltas.get("left", 0.0),
                    right_delta_db=correction_deltas.get("right", 0.0),
                )
                await asyncio.to_thread(
                    set_audio_output_mode,
                    OUTPUT_MODE_SUBWOOFER_22_STEREO, _auto_sub_22_global_config(correction_snapshot),
                    _auto_sub_22_candidate_subwoofers(
                        correction_snapshot, sub1_alignment_ms=best_left, sub2_alignment_ms=best_right,
                        active_subs=("sub1", "sub2"),
                    ),
                )
                if _dsp_runtime() is not None:
                    await _dsp_runtime().sync(await asyncio.to_thread(get_audio_output_overview))
                correction_left = await _measure_auto_sub_candidate(
                    delay_ms=best_left, job=job, candidate_index=1, total=2,
                    stage="gain_correction_after", fc=fc, input_id=input_id, channel="left",
                    mic_input_channel=mic_input_channel, reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref, calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
                    auto_sub_rate=auto_sub_rate, original_level=0.0, original_polarity="normal",
                    original_highpass=True, measure_channel="left",
                    output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
                    original_config_snapshot=correction_snapshot, sub1_alignment_ms=best_left,
                    sub2_alignment_ms=best_right, active_subs=("sub1", "sub2"),
                )
                correction_right = await _measure_auto_sub_candidate(
                    delay_ms=best_right, job=job, candidate_index=2, total=2,
                    stage="gain_correction_after", fc=fc, input_id=input_id, channel="right",
                    mic_input_channel=mic_input_channel, reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref, calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
                    auto_sub_rate=auto_sub_rate, original_level=0.0, original_polarity="normal",
                    original_highpass=True, measure_channel="right",
                    output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
                    original_config_snapshot=correction_snapshot, sub1_alignment_ms=best_left,
                    sub2_alignment_ms=best_right, active_subs=("sub1", "sub2"),
                )
                correction_after = _calculate_auto_sub_gain(
                    mode=OUTPUT_MODE_SUBWOOFER_22_STEREO, target_curve=job.get("target_curve"),
                    anchor=job.get("main_target_anchor"), winner_curves={
                        "left": correction_left.get("calibrated_points") or [],
                        "right": correction_right.get("calibrated_points") or [],
                    }, crossover_hz=fc,
                )
                if stereo_probe_plan and stereo_probe_plan.get("available"):
                    probe_channels: dict[str, Any] = {}
                    accepted_probe_sides: dict[str, bool] = {}
                    correction_points = {
                        "left": correction_left.get("calibrated_points") or [],
                        "right": correction_right.get("calibrated_points") or [],
                    }
                    for side in ("left", "right"):
                        planned = side in correction_deltas
                        before_corridor = (((stereo_probe_plan.get("channels") or {}).get(side) or {}).get("corridor_before") or {})
                        after_corridor = _auto_sub_stereo_corridor_violation(
                            points=correction_points[side], target_curve=job.get("target_curve"),
                            anchor=job.get("main_target_anchor"), crossover_hz=fc,
                            direction=correction_deltas.get(side, 0.0),
                        ) if planned else before_corridor
                        before_score = abs(float(gain_after["channels"][side]["target_delta_db"]))
                        after_score = abs(float(correction_after["channels"][side]["target_delta_db"]))
                        score_better = after_score < before_score
                        corridor_better = (
                            after_corridor.get("available") is True
                            and float(after_corridor.get("severity_db", 0.0)) < float(before_corridor.get("severity_db", 0.0))
                        )
                        accepted_probe_sides[side] = bool(planned and score_better and corridor_better)
                        probe_channels[side] = {
                            "planned": planned, "accepted": accepted_probe_sides[side],
                            "score_before": round(before_score, 3), "score_after": round(after_score, 3),
                            "score_better": score_better, "corridor_before": before_corridor,
                            "corridor_after": after_corridor, "corridor_better": corridor_better,
                        }
                    accepted_any_probe = any(accepted_probe_sides.values())
                    final_gain_deltas = {
                        side: gain_deltas.get(side, 0.0) + (
                            correction_deltas.get(side, 0.0) if accepted_probe_sides[side] else 0.0
                        ) for side in ("left", "right")
                    }
                    final_gain_snapshot = _auto_sub_22_snapshot_with_gain(
                        polarity_snapshot, left_delta_db=final_gain_deltas["left"],
                        right_delta_db=final_gain_deltas["right"],
                    )
                    final_gain_left = correction_left if accepted_probe_sides["left"] else gain_after_left
                    final_gain_right = correction_right if accepted_probe_sides["right"] else gain_after_right
                    correction_verdict = {
                        "accepted": accepted_any_probe,
                        "reason": "Stereo corridor probe improved score and 1/3-octave violation" if accepted_any_probe else "Stereo corridor probe rejected; Step 1 retained",
                        "channels": probe_channels, "step1_retained": True, "stereo_probe": True,
                    }
                    if not all(accepted_probe_sides.get(side, False) for side in correction_deltas):
                        await asyncio.to_thread(
                            set_audio_output_mode,
                            OUTPUT_MODE_SUBWOOFER_22_STEREO, _auto_sub_22_global_config(final_gain_snapshot),
                            _auto_sub_22_candidate_subwoofers(
                                final_gain_snapshot, sub1_alignment_ms=best_left, sub2_alignment_ms=best_right,
                                active_subs=("sub1", "sub2"),
                            ),
                        )
                        if _dsp_runtime() is not None:
                            await _dsp_runtime().sync(await asyncio.to_thread(get_audio_output_overview))
                else:
                    correction_verdict = _auto_sub_gain_verdict(
                        gain_after, correction_after, OUTPUT_MODE_SUBWOOFER_22_STEREO,
                    )
                    if correction_verdict["accepted"]:
                        final_gain_deltas = {
                            side: gain_deltas.get(side, 0.0) + correction_deltas.get(side, 0.0)
                            for side in ("left", "right")
                        }
                        final_gain_snapshot = correction_snapshot
                        final_gain_left, final_gain_right = correction_left, correction_right
                    else:
                        await asyncio.to_thread(
                            set_audio_output_mode,
                            OUTPUT_MODE_SUBWOOFER_22_STEREO, _auto_sub_22_global_config(gain_snapshot),
                            _auto_sub_22_candidate_subwoofers(
                                gain_snapshot, sub1_alignment_ms=best_left, sub2_alignment_ms=best_right,
                                active_subs=("sub1", "sub2"),
                            ),
                        )
                        if _dsp_runtime() is not None:
                            await _dsp_runtime().sync(await asyncio.to_thread(get_audio_output_overview))
        _auto_sub_gain_log_line("AUTOGAIN_FEEDBACK", {
            "gain_after_step1": {
                "left": float(_auto_sub_22_sub(gain_snapshot, "sub1").get("level_db", 0.0)),
                "right": float(_auto_sub_22_sub(gain_snapshot, "sub2").get("level_db", 0.0)),
            },
            "score_before": _auto_sub_gain_log_score(job["auto_gain"]),
            "score_after_step1": _auto_sub_gain_log_score(gain_after),
            "response_per_db_left": (((correction_plan or {}).get("channels") or {}).get("left") or {}).get("response_change_per_db"),
            "response_per_db_right": (((correction_plan or {}).get("channels") or {}).get("right") or {}).get("response_change_per_db"),
            "remaining_error_left": (gain_after.get("channels", {}).get("left") or {}).get("target_delta_db"),
            "remaining_error_right": (gain_after.get("channels", {}).get("right") or {}).get("target_delta_db"),
            "raw_correction_db": (correction_plan or {}).get("raw_deltas_db") or None,
            "applied_correction_db": (correction_plan or {}).get("applied_deltas_db") or None,
            "correction_step_db": correction_deltas or None,
        })
        decision = "accepted_step2" if correction_verdict and correction_verdict.get("accepted") else (
            "accepted_step1" if step1_retained else "restored"
        )
        if decision == "accepted_step2":
            if correction_verdict.get("stereo_probe"):
                score_final_source = copy.deepcopy(gain_after)
                for side in ("left", "right"):
                    if ((correction_verdict.get("channels") or {}).get(side) or {}).get("accepted"):
                        score_final_source["channels"][side] = copy.deepcopy(correction_after["channels"][side])
            else:
                score_final_source = correction_after
        elif decision == "accepted_step1":
            score_final_source = copy.deepcopy(job["auto_gain"])
            for side in ("left", "right"):
                if accepted_step1_sides[side]:
                    score_final_source["channels"][side] = copy.deepcopy(gain_after["channels"][side])
        else:
            score_final_source = job["auto_gain"]
        _auto_sub_gain_log_line("AUTOGAIN_RESULT", {
            "gain_final": {
                "left": float(_auto_sub_22_sub(final_gain_snapshot, "sub1").get("level_db", 0.0)),
                "right": float(_auto_sub_22_sub(final_gain_snapshot, "sub2").get("level_db", 0.0)),
            },
            "score_final": _auto_sub_gain_log_score(score_final_source), "decision": decision,
            "reason": ((correction_verdict or gain_verdict) or {}).get("reason"),
            "delay_final": {"left_ms": best_left, "right_ms": best_right},
        })
        job["auto_gain"].update({
            "applied": bool(step1_retained and gain_deltas),
            "reverted": bool(gain_deltas and not step1_retained),
            "accepted_step1_sides": accepted_step1_sides,
            "verification": gain_after, "verification_verdict": gain_verdict,
            "response_correction": correction_plan,
            "stereo_corridor_probe": stereo_probe_plan,
            "correction_deltas_db": correction_deltas,
            "correction_verification": correction_after,
            "correction_verdict": correction_verdict,
            "final_deltas_db": final_gain_deltas,
            "original_levels_db": {
                "sub1": float(original_left.get("level_db", 0.0)), "sub2": float(original_right.get("level_db", 0.0)),
            },
            "final_levels_db": {
                "sub1": float(_auto_sub_22_sub(final_gain_snapshot, "sub1").get("level_db", 0.0)),
                "sub2": float(_auto_sub_22_sub(final_gain_snapshot, "sub2").get("level_db", 0.0)),
            },
            "stage_output_peaks": {
                "left": final_gain_left.get("stage_output_peaks"),
                "right": final_gain_right.get("stage_output_peaks"),
            },
        })
        overall_score = (0.6 * min(left_score, right_score)) + (0.4 * ((left_score + right_score) / 2.0))
        left_xo_score = float(left_winner.get("xo_score", 0.0) or 0.0)
        right_xo_score = float(right_winner.get("xo_score", 0.0) or 0.0)
        left_timing_score = float(left_winner.get("timing_band_score", 0.0) or 0.0)
        right_timing_score = float(right_winner.get("timing_band_score", 0.0) or 0.0)
        left_low_guard_loss = float(left_winner.get("low_guard_loss_db", 0.0) or 0.0)
        right_low_guard_loss = float(right_winner.get("low_guard_loss_db", 0.0) or 0.0)
        left_low_guard_penalty = float(left_winner.get("low_guard_penalty", 0.0) or 0.0)
        right_low_guard_penalty = float(right_winner.get("low_guard_penalty", 0.0) or 0.0)
        overall_low_guard_loss = max(left_low_guard_loss, right_low_guard_loss)
        overall_low_guard_penalty = (
            0.6 * max(left_low_guard_penalty, right_low_guard_penalty)
            + 0.4 * ((left_low_guard_penalty + right_low_guard_penalty) / 2.0)
        )
        left_score_pct = round(left_score * 100.0, 1)
        right_score_pct = round(right_score * 100.0, 1)
        overall_score_pct = round(overall_score * 100.0, 1)
        job["status"] = "completed"
        job["message"] = (
            f"Applied 2.2 Stereo Bass: Left Sub {best_left:.2f} ms / "
            f"Right Sub {best_right:.2f} ms (overall {overall_score_pct:.1f} %)"
        )
        _log_auto_sub_timing_summary(job)

        # Build baseline and confirmation measurements for before/after graph display
        all_left_sweeps = list(left_results) + list(left_fine_results)
        all_right_sweeps = list(right_results) + list(right_fine_results)
        left_baseline = _auto_sub_result_for_delay(all_left_sweeps, original_left_alignment)
        right_baseline = _auto_sub_result_for_delay(all_right_sweeps, original_right_alignment)
        # Prefer the final measured per-side sweep (gain verification or
        # polarity-refined winner) so the confirmation always reflects the
        # applied pair; fall back to the scan sweep at the accepted delay.
        def _points_sweep(sweep: dict[str, Any] | None) -> dict[str, Any] | None:
            return sweep if sweep and _auto_sub_has_points(sweep, "points") else None

        left_confirm = _points_sweep(final_gain_left) or _auto_sub_result_for_delay(all_left_sweeps, best_left)
        right_confirm = _points_sweep(final_gain_right) or _auto_sub_result_for_delay(all_right_sweeps, best_right)

        # One shared vertical offset from baseline L+R bass region so that
        # Before/After and L/R relative level differences are preserved.
        _stereo_offset_db = _auto_sub_shared_bass_offset(
            left_baseline.get("points") if left_baseline else [],
            right_baseline.get("points") if right_baseline else [],
        )

        def _stereo_measurement_from_lr(left_sweep, right_sweep, label, name, offset_db):
            traces = []
            base_id = uuid4().hex[:12]
            if left_sweep:
                pts = left_sweep.get("points") or []
                if isinstance(pts, list) and len(pts) >= 3:
                    points = [[float(p[0]), float(p[1]) - offset_db] for p in pts]
                    traces.append({"kind": "measured", "label": f"{label} L", "role": "left", "points": points})
            if right_sweep:
                pts = right_sweep.get("points") or []
                if isinstance(pts, list) and len(pts) >= 3:
                    points = [[float(p[0]), float(p[1]) - offset_db] for p in pts]
                    traces.append({"kind": "measured", "label": f"{label} R", "role": "right", "points": points})
            return {"id": f"autosub-{base_id}", "name": name, "traces": traces} if traces else None

        baseline_measurement = _stereo_measurement_from_lr(
            left_baseline, right_baseline, "Before",
            f"AutoSub 2.2S Baseline (L {original_left_alignment:.1f} / R {original_right_alignment:.1f} ms)",
            _stereo_offset_db,
        )
        confirmation_measurement = _stereo_measurement_from_lr(
            left_confirm, right_confirm, "After",
            f"AutoSub 2.2S Optimized (L {best_left:.1f} / R {best_right:.1f} ms)",
            _stereo_offset_db,
        )

        job["result"] = {
            "mode": OUTPUT_MODE_SUBWOOFER_22_STEREO,
            "original_sub1_alignment_ms": original_left_alignment,
            "original_sub2_alignment_ms": original_right_alignment,
            "suggested_sub1_alignment_ms": best_left,
            "suggested_sub2_alignment_ms": best_right,
            "applied_sub1_alignment_ms": best_left,
            "applied_sub2_alignment_ms": best_right,
            "applied": True,
            "auto_applied": True,
            "apply_decision": "applied_22_stereo_separate_lr",
            "candidate_ledger": candidate_ledger,
            "crossover_hz": fc,
            "confidence": "left_right_separate",
            "winner": {
                "name": _auto_sub_22_stereo_name(best_left, best_right),
                "score": round(overall_score, 4),
                "score_pct": overall_score_pct,
                "overall_score": round(overall_score, 4),
                "overall_score_pct": overall_score_pct,
                "xo_score": round((left_xo_score + right_xo_score) / 2.0, 4),
                "timing_band_score": round((left_timing_score + right_timing_score) / 2.0, 4),
                "low_guard_loss_db": round(overall_low_guard_loss, 2),
                "low_guard_penalty": round(overall_low_guard_penalty, 4),
                "final_score": round(overall_score, 4),
                "low_guard_loss_L_db": round(left_low_guard_loss, 2),
                "low_guard_loss_R_db": round(right_low_guard_loss, 2),
                "low_guard_penalty_L": round(left_low_guard_penalty, 4),
                "low_guard_penalty_R": round(right_low_guard_penalty, 4),
                "score_L_pct": left_score_pct,
                "score_R_pct": right_score_pct,
            },
            "left_score": round(left_score, 4),
            "right_score": round(right_score, 4),
            "overall_score": round(overall_score, 4),
            "xo_score": round((left_xo_score + right_xo_score) / 2.0, 4),
            "timing_band_score": round((left_timing_score + right_timing_score) / 2.0, 4),
            "low_guard_loss_db": round(overall_low_guard_loss, 2),
            "low_guard_penalty": round(overall_low_guard_penalty, 4),
            "final_score": round(overall_score, 4),
            "low_guard_loss_L_db": round(left_low_guard_loss, 2),
            "low_guard_loss_R_db": round(right_low_guard_loss, 2),
            "low_guard_penalty_L": round(left_low_guard_penalty, 4),
            "low_guard_penalty_R": round(right_low_guard_penalty, 4),
            "left_score_pct": left_score_pct,
            "right_score_pct": right_score_pct,
            "overall_score_pct": overall_score_pct,
            "accepted_winner": {
                "name": _auto_sub_22_stereo_name(best_left, best_right),
                "left_winner": left_winner,
                "right_winner": right_winner,
                "score": round(overall_score, 4),
                "score_pct": overall_score_pct,
            },
            "fine_accepted": bool(left_acceptance["fine_accepted"] or right_acceptance["fine_accepted"]),
            "reject_reason": {
                "left": left_acceptance["reject_reason"],
                "right": right_acceptance["reject_reason"],
            },
            "left_coarse_winner": left_coarse_winner,
            "left_fine_winner": left_fine_winner,
            "right_coarse_winner": right_coarse_winner,
            "right_fine_winner": right_fine_winner,
            "left_winner": left_winner,
            "right_winner": right_winner,
            "left_incumbent_winner": left_incumbent_winner,
            "right_incumbent_winner": right_incumbent_winner,
            "left_incumbent_score": left_acceptance["incumbent_score"],
            "right_incumbent_score": right_acceptance["incumbent_score"],
            "left_accepted_winner": left_winner,
            "right_accepted_winner": right_winner,
            "left_fine_accepted": left_acceptance["fine_accepted"],
            "right_fine_accepted": right_acceptance["fine_accepted"],
            "left_reject_reason": left_acceptance["reject_reason"],
            "right_reject_reason": right_acceptance["reject_reason"],
            "left_coarse_ranking": left_coarse_scoring["results"],
            "left_fine_ranking": left_fine_scoring["results"] if left_fine_scoring else [],
            "right_coarse_ranking": right_coarse_scoring["results"],
            "right_fine_ranking": right_fine_scoring["results"] if right_fine_scoring else [],
            "left_ranking": left_scoring["results"],
            "right_ranking": right_scoring["results"],
            "fine_scan": job["fine_scan"],
            "sweep_count": actual_sweep_total + 2,
            "candidate_count": actual_sweep_total,
            "left_candidate_count": len(left_scan_delays) + len(left_fine_delays),
            "right_candidate_count": len(right_scan_delays) + len(right_fine_delays),
            "left_coarse_candidate_count": len(left_scan_delays),
            "left_fine_candidate_count": len(left_fine_delays),
            "right_coarse_candidate_count": len(right_scan_delays),
            "right_fine_candidate_count": len(right_fine_delays),
            "valid_count": len(left_final_valid) + len(right_final_valid),
            "left_valid_count": len(left_final_valid),
            "right_valid_count": len(right_final_valid),
            "left_coarse_valid_count": len(left_valid),
            "left_fine_valid_count": len(left_fine_valid),
            "right_coarse_valid_count": len(right_valid),
            "right_fine_valid_count": len(right_fine_valid),
            "baseline_measurement": baseline_measurement,
            "confirmation_measurement": confirmation_measurement,
            **derived_delays,
        }
        logger.info(
            "Auto-sub 2.2 Stereo Bass optimize completed: fc=%sHz left %.2f->%.2fms right %.2f->%.2fms "
            "overall_score=%.1f%% score_L=%.1f%% score_R=%.1f%%",
            fc,
            original_left_alignment,
            best_left,
            original_right_alignment,
            best_right,
            overall_score_pct,
            left_score_pct,
            right_score_pct,
        )

    except Exception as exc:
        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return
        logger.exception("Auto-sub 2.2 Stereo Bass optimize failed")
        job["status"] = "failed"
        job["message"] = f"Auto Sub Optimize 2.2 Stereo Bass failed: {exc}"
        job["error"] = {"detail": str(exc)}
        await _restore_original_config()

    finally:
        await _finish_auto_sub_worker(job, job_id)
