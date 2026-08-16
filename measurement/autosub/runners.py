# SPDX-License-Identifier: AGPL-3.0-only

"""The three AutoSub optimize runners and the start route."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import File, Form, HTTPException, UploadFile

from http_errors import bad_request
from audio.samplerate import (
    OUTPUT_MODE_SUBWOOFER_21,
    OUTPUT_MODE_SUBWOOFER_22,
    OUTPUT_MODE_SUBWOOFER_22_MODES,
    OUTPUT_MODE_SUBWOOFER_22_STEREO,
    OUTPUT_MODE_SUBWOOFER_MODES,
    get_audio_output_overview,
    set_audio_output_mode,
)
from dsp.runtime import BassManagementConfig
from measurement.store import score_sub_alignment_candidates
from uploads import UploadTooLargeError, read_upload

from .candidates import (
    _auto_sub_22_candidate_subwoofers,
    _auto_sub_22_global_config,
    _auto_sub_22_sub,
    _auto_sub_22_verify_alignment,
    _auto_sub_apply_candidate,
    _auto_sub_clamped_delay,
    _auto_sub_fine_delay_candidates,
    _auto_sub_fine_trigger_reasons,
    _auto_sub_opposite_polarity,
    _auto_sub_polarity_decision,
    _auto_sub_snapshot_copy,
    _auto_sub_step_ms,
    _auto_sub_sweep_profile,
)
from .deps import (
    _AUTO_SUB_JOBS,
    _auto_sub_lock,
    _cleanup_stale_autosub_cancelling_jobs,
    _dsp_runtime,
    _auto_sub_cancel_requested,
    _measurement_session,
    _measurement_store,
    _start_auto_sub_worker,
)
from .jobs import _finish_auto_sub_worker, _log_auto_sub_timing_summary, router
from .measurement import (
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
    _measure_auto_sub_combined_candidate,
)
from .scoring import (
    _auto_sub_best_scan_result,
    _auto_sub_candidate_ledger,
    _auto_sub_delay_key,
    _auto_sub_has_points,
    _auto_sub_measurement_from_sweep,
    _auto_sub_rank_results,
    _auto_sub_result_for_delay,
    _auto_sub_select_accepted_winner,
    _auto_sub_shared_bass_offset,
    _score_auto_sub_combined_candidates,
    _score_auto_sub_matrix_candidates,
)

logger = logging.getLogger(__name__)

_AUTO_SUB_MAX_CALIBRATION_BYTES: int = 2 * 1024 * 1024  # 2 MiB


@router.post("/api/measurements/auto-sub-optimize/start")
async def start_auto_sub_optimize(
    input_id: str = Form(...),
    input_key: str = Form(""),
    channel: str = Form("left"),
    mic_input_channel: str = Form("1"),
    reference_input_channel: str = Form(""),
    calibration_ref: str = Form(""),
    target_curve_snapshot: str = Form(""),
    calibration_file: UploadFile | None = File(None),
):
    measurement_store = _measurement_store()
    measurement_sr_session = _measurement_session()
    global _auto_sub_lock
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    try:
        input_id = measurement_store.resolve_capture_input_id(input_id=input_id, input_key=input_key)
    except ValueError as exc:
        raise bad_request(exc) from exc
    if not _auto_sub_lock:
        _auto_sub_lock = asyncio.Lock()

    # Capture the session entry epoch before any await can interleave a
    # measurement-window close: an entry invalidated by a close must never
    # register as a running Auto-Sub job afterwards.
    entry_epoch = (
        measurement_sr_session.capture_entry_epoch()
        if measurement_sr_session is not None
        else None
    )

    from audio.samplerate import _load_audio_output_mode, set_audio_output_mode

    # Reject if any measurement is already running
    if measurement_store.has_active_measurement_job():
        raise HTTPException(status_code=409, detail="Another measurement is already running")

    # Clean up stale cancelling jobs before lock acquisition
    _cleanup_stale_autosub_cancelling_jobs()

    # Acquire lock before modifying AutoSub state
    try:
        await asyncio.wait_for(_auto_sub_lock.acquire(), timeout=0.5)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=423, detail="Auto Sub Optimize is already in progress")

    try:
        mode_state = _load_audio_output_mode()
        output_mode = mode_state.get("mode")
        if output_mode not in OUTPUT_MODE_SUBWOOFER_MODES:
            raise HTTPException(status_code=400, detail="Auto Sub Optimize requires 2.1 or 2.2 Subwoofer output mode")
        auto_sub_playback_gain = _capture_auto_sub_playback_gain()

        config = BassManagementConfig.from_overview(get_audio_output_overview())
        fc = config.crossover_frequency_hz
        current_alignment = config.sub_alignment_ms
        current_sub2_alignment = config.sub2_alignment_ms if output_mode in OUTPUT_MODE_SUBWOOFER_22_MODES else 0.0
        original_polarity = config.sub_polarity
        original_level = config.sub_level_db
        original_highpass = config.main_highpass_enabled

        # Compute scan range
        step_ms = _auto_sub_step_ms(fc)
        coarse_steps = 4
        scan_delays: list[float] = []
        for s in range(-coarse_steps, coarse_steps + 1):
            delay = _auto_sub_clamped_delay(current_alignment + s * step_ms)
            if not scan_delays or abs(delay - scan_delays[-1]) > 0.05:
                scan_delays.append(delay)

        # Snapshot original config for rollback
        original_config_snapshot = _auto_sub_snapshot_copy(mode_state)
        target_curve, target_curve_error = _validate_auto_sub_target_curve_snapshot(target_curve_snapshot)

        job_id = f"auto-sub-{uuid4().hex[:12]}"
        job: dict[str, Any] = {
            "id": job_id,
            "status": "preparing",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "message": f"Auto Sub Optimize: {len(scan_delays)} candidates @ {fc} Hz",
            "result": None,
            "error": None,
            "mode": output_mode,
            "crossover_hz": fc,
            "scan_delays": scan_delays,
            "step_ms": step_ms,
            "original_alignment_ms": current_alignment,
            "original_sub1_alignment_ms": current_alignment,
            "original_sub2_alignment_ms": current_sub2_alignment,
            "original_config_snapshot": original_config_snapshot,
            "target_curve": target_curve,
            "auto_gain": {
                "available": False,
                "reason": target_curve_error or "Vertical Main/Target level reference has not passed its mandatory gate",
            },
            "playback_gain": auto_sub_playback_gain,
            "current_sweep_id": "",
            "cancel_requested": False,
            "cancelled_at": None,
            "fine_scan": {
                "enabled": False,
                "triggered": False,
                "status": "pending",
                "candidates": [],
            },
        }
        _AUTO_SUB_JOBS[job_id] = job
        logger.info(
            "AUTOSUB job=%s start mode=%s fc=%sHz candidates=%s target_curve=%s auto_gain=%s playback_gain=%s",
            job_id, output_mode, fc, len(scan_delays),
            json.dumps(target_curve, sort_keys=True, separators=(",", ":")) if target_curve else "unavailable",
            json.dumps(job["auto_gain"], sort_keys=True, separators=(",", ":")),
            json.dumps(auto_sub_playback_gain, sort_keys=True, separators=(",", ":")),
        )

        calibration_bytes = None
        calibration_filename = None
        if calibration_file is not None:
            calibration_filename = calibration_file.filename or "calibration.txt"
            content_type = str(calibration_file.content_type or "").lower()
            if "text" not in content_type and "plain" not in content_type and content_type not in ("", "application/octet-stream"):
                raise HTTPException(status_code=400, detail="Calibration file must be a text file")
            try:
                raw_bytes = await read_upload(calibration_file, _AUTO_SUB_MAX_CALIBRATION_BYTES)
            except UploadTooLargeError:
                raise HTTPException(status_code=400, detail=f"Calibration file too large (max {_AUTO_SUB_MAX_CALIBRATION_BYTES // (1024*1024)} MiB)")
            calibration_bytes = raw_bytes

        if output_mode == OUTPUT_MODE_SUBWOOFER_22_STEREO:
            fine_step_ms = step_ms / 4.0
            right_scan_delays: list[float] = []
            for s in range(-coarse_steps, coarse_steps + 1):
                delay = _auto_sub_clamped_delay(current_sub2_alignment + s * step_ms)
                if not right_scan_delays or abs(delay - right_scan_delays[-1]) > 0.05:
                    right_scan_delays.append(delay)
            job["message"] = (
                f"Auto Sub Optimize 2.2 Stereo Bass: Left Sub {len(scan_delays)} coarse, "
                f"Left fine up to 6, Right Sub {len(right_scan_delays)} coarse, Right fine up to 6 @ {fc} Hz"
            )
            job["scan_delays"] = {"left_sub": scan_delays, "right_sub": right_scan_delays}
            job["fine_scan"] = {
                "enabled": True,
                "triggered": False,
                "status": "pending",
                "reason": "2.2 Stereo Bass optimizes Left and Right Sub separately with per-side fine scans",
                "fine_step_ms": fine_step_ms,
                "left": {"status": "pending", "candidates": []},
                "right": {"status": "pending", "candidates": []},
            }
            _start_auto_sub_worker(
                _run_auto_sub_22_stereo_optimize(
                    job_id=job_id,
                    input_id=input_id,
                    mic_input_channel=mic_input_channel,
                    reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref,
                    calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes,
                    left_scan_delays=scan_delays,
                    right_scan_delays=right_scan_delays,
                    fc=fc,
                    original_config_snapshot=original_config_snapshot,
                    entry_epoch=entry_epoch,
                )
            )
        elif output_mode == OUTPUT_MODE_SUBWOOFER_22:
            fine_step_ms = step_ms / 4.0
            sub2_scan_delays: list[float] = []
            for s in range(-coarse_steps, coarse_steps + 1):
                delay = _auto_sub_clamped_delay(current_sub2_alignment + s * step_ms)
                if not sub2_scan_delays or abs(delay - sub2_scan_delays[-1]) > 0.05:
                    sub2_scan_delays.append(delay)
            job["message"] = (
                f"Auto Sub Optimize 2.2: Sub 1 {len(scan_delays)} coarse, "
                f"Sub 2 {len(sub2_scan_delays)} coarse, 3x3 matrix @ {fc} Hz"
            )
            job["scan_delays"] = {"sub1": scan_delays, "sub2": sub2_scan_delays}
            job["combined_matrix"] = {"status": "pending", "fine_step_ms": fine_step_ms, "candidates": []}
            _start_auto_sub_worker(
                _run_auto_sub_22_optimize(
                    job_id=job_id,
                    input_id=input_id,
                    mic_input_channel=mic_input_channel,
                    reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref,
                    calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes,
                    sub1_scan_delays=scan_delays,
                    sub2_scan_delays=sub2_scan_delays,
                    fc=fc,
                    original_config_snapshot=original_config_snapshot,
                    fine_step_ms=fine_step_ms,
                    entry_epoch=entry_epoch,
                )
            )
        else:
            _start_auto_sub_worker(
                _run_auto_sub_optimize(
                    job_id=job_id,
                    input_id=input_id,
                    channel=channel,
                    mic_input_channel=mic_input_channel,
                    reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref,
                    calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes,
                    scan_delays=scan_delays,
                    fc=fc,
                    current_alignment=current_alignment,
                    original_polarity=original_polarity,
                    original_level=original_level,
                    original_highpass=original_highpass,
                    original_config_snapshot=original_config_snapshot,
                    entry_epoch=entry_epoch,
                )
            )
        return {"status": "ok", "job": job}
    except HTTPException:
        _auto_sub_lock.release()
        raise
    except asyncio.CancelledError:
        _auto_sub_lock.release()
        raise
    except Exception:
        _auto_sub_lock.release()
        raise

async def _run_auto_sub_22_optimize(
    job_id: str,
    input_id: str,
    mic_input_channel: str,
    reference_input_channel: str,
    calibration_ref: str,
    calibration_filename: str | None,
    calibration_bytes: bytes | None,
    sub1_scan_delays: list[float],
    sub2_scan_delays: list[float],
    fc: int,
    original_config_snapshot: dict[str, Any],
    fine_step_ms: float,
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

    original_sub1 = _auto_sub_22_sub(original_config_snapshot, "sub1")
    original_sub2 = _auto_sub_22_sub(original_config_snapshot, "sub2")
    original_sub1_alignment = float(original_sub1.get("alignment_ms", 0.0) or 0.0)
    original_sub2_alignment = float(original_sub2.get("alignment_ms", 0.0) or 0.0)

    def _matrix_delays(center: float) -> list[float]:
        return [_auto_sub_clamped_delay(center + offset) for offset in (-fine_step_ms, 0.0, fine_step_ms)]

    def _valid_lr(result: dict[str, Any]) -> bool:
        return _auto_sub_has_points(result, "points_left") or _auto_sub_has_points(result, "points_right")

    def _same_pair(pair: tuple[float, float], sub1_alignment: float, sub2_alignment: float) -> bool:
        return abs(pair[0] - sub1_alignment) <= 0.05 and abs(pair[1] - sub2_alignment) <= 0.05

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
            auto_sub_rate=auto_sub_rate, output_mode=OUTPUT_MODE_SUBWOOFER_22,
            original_config_snapshot=original_config_snapshot,
        )
        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return

        coarse1_results: list[dict[str, Any]] = []
        coarse2_results: list[dict[str, Any]] = []
        matrix_results: list[dict[str, Any]] = []
        sub1_sweep_total = len(sub1_scan_delays) * 2
        sub2_sweep_total = len(sub2_scan_delays) * 2
        matrix_sweep_start = sub1_sweep_total + sub2_sweep_total
        matrix_sweep_total = matrix_sweep_start + 18

        job["stage"] = "sub1_coarse"
        for idx, delay_ms in enumerate(sub1_scan_delays):
            coarse1_results.append(await _measure_auto_sub_combined_candidate(
                delay_ms=delay_ms,
                job=job,
                candidate_index=idx + 1,
                total=len(sub1_scan_delays),
                sweep_index_start=(idx * 2) + 1,
                sweep_total=matrix_sweep_total,
                stage="sub1_coarse",
                fc=fc,
                input_id=input_id,
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
                output_mode=OUTPUT_MODE_SUBWOOFER_22,
                original_config_snapshot=original_config_snapshot,
                sub1_alignment_ms=delay_ms,
                sub2_alignment_ms=original_sub2_alignment,
                active_subs=("sub1",),
            ))
            if _auto_sub_cancel_requested(job):
                job["message"] = "Auto Sub Optimize cancelled."
                await _restore_original_config()
                return

        coarse1_valid = [result for result in coarse1_results if _valid_lr(result)]
        if not coarse1_valid:
            job["status"] = "failed"
            job["message"] = "No valid Sub 1 coarse sweep results to score"
            job["error"] = {"detail": "Sub 1 coarse sweeps failed or produced insufficient data"}
            await _restore_original_config()
            return
        sub1_scoring = _score_auto_sub_combined_candidates(
            coarse1_results,
            crossover_hz=fc,
            low_guard_reference_delay_ms=original_sub1_alignment,
        )
        sub1_winner = sub1_scoring["winner"]
        sub1_winner_delay = _auto_sub_clamped_delay(float(sub1_winner.get("delay_ms", original_sub1_alignment) or original_sub1_alignment))

        job["stage"] = "sub2_coarse"
        for idx, delay_ms in enumerate(sub2_scan_delays):
            coarse2_results.append(await _measure_auto_sub_combined_candidate(
                delay_ms=delay_ms,
                job=job,
                candidate_index=idx + 1,
                total=len(sub2_scan_delays),
                sweep_index_start=sub1_sweep_total + (idx * 2) + 1,
                sweep_total=matrix_sweep_total,
                stage="sub2_coarse",
                fc=fc,
                input_id=input_id,
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
                output_mode=OUTPUT_MODE_SUBWOOFER_22,
                original_config_snapshot=original_config_snapshot,
                sub1_alignment_ms=original_sub1_alignment,
                sub2_alignment_ms=delay_ms,
                active_subs=("sub2",),
            ))
            if _auto_sub_cancel_requested(job):
                job["message"] = "Auto Sub Optimize cancelled."
                await _restore_original_config()
                return

        coarse2_valid = [result for result in coarse2_results if _valid_lr(result)]
        if not coarse2_valid:
            job["status"] = "failed"
            job["message"] = "No valid Sub 2 coarse sweep results to score"
            job["error"] = {"detail": "Sub 2 coarse sweeps failed or produced insufficient data"}
            await _restore_original_config()
            return
        sub2_scoring = _score_auto_sub_combined_candidates(
            coarse2_results,
            crossover_hz=fc,
            low_guard_reference_delay_ms=original_sub2_alignment,
        )
        sub2_winner = sub2_scoring["winner"]
        sub2_winner_delay = _auto_sub_clamped_delay(float(sub2_winner.get("delay_ms", original_sub2_alignment) or original_sub2_alignment))

        sub1_matrix = _matrix_delays(sub1_winner_delay)
        sub2_matrix = _matrix_delays(sub2_winner_delay)
        matrix_pairs = [(sub1_delay, sub2_delay) for sub1_delay in sub1_matrix for sub2_delay in sub2_matrix]
        incumbent_pair = (
            _auto_sub_clamped_delay(original_sub1_alignment),
            _auto_sub_clamped_delay(original_sub2_alignment),
        )
        incumbent_in_matrix = any(_same_pair(pair, incumbent_pair[0], incumbent_pair[1]) for pair in matrix_pairs)
        if not incumbent_in_matrix:
            matrix_pairs.append(incumbent_pair)
        job["combined_matrix"] = {
            "status": "running",
            "fine_step_ms": fine_step_ms,
            "sub1_candidates": sub1_matrix,
            "sub2_candidates": sub2_matrix,
            "incumbent_pair": {"sub1_alignment_ms": incumbent_pair[0], "sub2_alignment_ms": incumbent_pair[1]},
            "incumbent_in_matrix": incumbent_in_matrix,
            "candidates": [
                {
                    "sub1_alignment_ms": a,
                    "sub2_alignment_ms": b,
                    "incumbent_pair": _same_pair((a, b), incumbent_pair[0], incumbent_pair[1]),
                }
                for a, b in matrix_pairs
            ],
        }
        matrix_sweep_total = matrix_sweep_start + (len(matrix_pairs) * 2)

        job["stage"] = "combined_matrix"
        for idx, (sub1_delay, sub2_delay) in enumerate(matrix_pairs):
            matrix_results.append(await _measure_auto_sub_combined_candidate(
                delay_ms=sub1_delay,
                job=job,
                candidate_index=idx + 1,
                total=len(matrix_pairs),
                sweep_index_start=matrix_sweep_start + (idx * 2) + 1,
                sweep_total=matrix_sweep_total,
                stage="combined_matrix",
                fc=fc,
                input_id=input_id,
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
                output_mode=OUTPUT_MODE_SUBWOOFER_22,
                original_config_snapshot=original_config_snapshot,
                sub1_alignment_ms=sub1_delay,
                sub2_alignment_ms=sub2_delay,
                active_subs=("sub1", "sub2"),
            ))
            if _auto_sub_cancel_requested(job):
                job["message"] = "Auto Sub Optimize cancelled."
                await _restore_original_config()
                return

        matrix_valid = [result for result in matrix_results if _valid_lr(result)]
        if not matrix_valid:
            job["status"] = "failed"
            job["message"] = "No valid Combined Matrix sweep results to score"
            job["error"] = {"detail": "Combined Matrix sweeps failed or produced insufficient data"}
            await _restore_original_config()
            return

        matrix_scoring = _score_auto_sub_matrix_candidates(
            matrix_results,
            crossover_hz=fc,
            original_sub1_alignment_ms=original_sub1_alignment,
            original_sub2_alignment_ms=original_sub2_alignment,
        )
        winner = matrix_scoring["accepted_winner"]
        gain_winner = next(
            (candidate for candidate in matrix_results
             if round(float(candidate.get("sub1_alignment_ms", 0.0)), 2) == round(float(winner.get("sub1_alignment_ms", 0.0)), 2)
             and round(float(candidate.get("sub2_alignment_ms", 0.0)), 2) == round(float(winner.get("sub2_alignment_ms", 0.0)), 2)),
            {},
        )
        best_sub1 = _auto_sub_clamped_delay(float(winner.get("sub1_alignment_ms", sub1_winner_delay) or sub1_winner_delay))
        best_sub2 = _auto_sub_clamped_delay(float(winner.get("sub2_alignment_ms", sub2_winner_delay) or sub2_winner_delay))
        incumbent_polarities = (str(original_sub1.get("polarity", "normal")), str(original_sub2.get("polarity", "normal")))
        selected_polarities = incumbent_polarities
        polarity_candidates: list[dict[str, Any]] = [dict(gain_winner, delay_ms=0.0)]
        alternative_polarities = [
            (_auto_sub_opposite_polarity(incumbent_polarities[0]), incumbent_polarities[1]),
            (incumbent_polarities[0], _auto_sub_opposite_polarity(incumbent_polarities[1])),
            (_auto_sub_opposite_polarity(incumbent_polarities[0]), _auto_sub_opposite_polarity(incumbent_polarities[1])),
        ]
        for idx, polarities in enumerate(alternative_polarities, 1):
            measured = await _measure_auto_sub_combined_candidate(
                delay_ms=best_sub1, job=job, candidate_index=idx, total=3,
                sweep_index_start=matrix_sweep_total + (idx - 1) * 2 + 1,
                sweep_total=matrix_sweep_total + 6, stage="polarity_check", fc=fc,
                input_id=input_id, mic_input_channel=mic_input_channel,
                reference_input_channel=reference_input_channel, calibration_ref=calibration_ref,
                calibration_filename=calibration_filename, calibration_bytes=calibration_bytes,
                auto_sub_sweep_profile=auto_sub_sweep_profile, auto_sub_rate=auto_sub_rate,
                original_level=0.0, original_polarity="normal", original_highpass=True,
                output_mode=OUTPUT_MODE_SUBWOOFER_22, original_config_snapshot=original_config_snapshot,
                sub1_alignment_ms=best_sub1, sub2_alignment_ms=best_sub2,
                active_subs=("sub1", "sub2"), sub1_polarity=polarities[0], sub2_polarity=polarities[1],
            )
            polarity_candidates.append(dict(measured, delay_ms=float(idx), tested_polarities=polarities))
        polarity_scoring = _score_auto_sub_combined_candidates(polarity_candidates, crossover_hz=fc, low_guard_reference_delay_ms=0.0)
        polarity_winner = polarity_scoring["winner"]
        incumbent_scored = _auto_sub_result_for_delay(polarity_scoring["results"], 0.0) or {}
        alternative_scored = polarity_winner if float(polarity_winner.get("delay_ms", 0.0)) != 0.0 else {}
        polarity_decision = _auto_sub_polarity_decision(incumbent_scored, alternative_scored) if alternative_scored else {
            "accepted": False, "reason": "incumbent_best", "score_gain": 0.0, "min_score_gain": 0.03,
        }
        if polarity_decision["accepted"]:
            selected_idx = int(round(float(alternative_scored["delay_ms"])))
            selected_polarities = alternative_polarities[selected_idx - 1]
            selected_measurement = polarity_candidates[selected_idx]
            refinement: list[dict[str, Any]] = []
            refinements = [(a, b) for a in _matrix_delays(best_sub1) for b in _matrix_delays(best_sub2)]
            for idx, (delay1, delay2) in enumerate(refinements):
                refinement.append(await _measure_auto_sub_combined_candidate(
                    delay_ms=delay1, job=job, candidate_index=idx + 1, total=9,
                    sweep_index_start=matrix_sweep_total + 7 + idx * 2,
                    sweep_total=matrix_sweep_total + 24, stage="polarity_refine", fc=fc,
                    input_id=input_id, mic_input_channel=mic_input_channel,
                    reference_input_channel=reference_input_channel, calibration_ref=calibration_ref,
                    calibration_filename=calibration_filename, calibration_bytes=calibration_bytes,
                    auto_sub_sweep_profile=auto_sub_sweep_profile, auto_sub_rate=auto_sub_rate,
                    original_level=0.0, original_polarity="normal", original_highpass=True,
                    output_mode=OUTPUT_MODE_SUBWOOFER_22, original_config_snapshot=original_config_snapshot,
                    sub1_alignment_ms=delay1, sub2_alignment_ms=delay2, active_subs=("sub1", "sub2"),
                    sub1_polarity=selected_polarities[0], sub2_polarity=selected_polarities[1],
                ))
            refined_scoring = _score_auto_sub_matrix_candidates(refinement, crossover_hz=fc)
            refined_winner = refined_scoring["winner"]
            best_sub1 = float(refined_winner["sub1_alignment_ms"])
            best_sub2 = float(refined_winner["sub2_alignment_ms"])
            gain_winner = next((row for row in refinement if abs(float(row.get("sub1_alignment_ms", 0))-best_sub1)<0.01 and abs(float(row.get("sub2_alignment_ms", 0))-best_sub2)<0.01), selected_measurement)
            polarity_decision["refinement"] = {"winner": refined_winner, "candidate_count": 9}
        polarity_snapshot = _auto_sub_snapshot_copy(original_config_snapshot)
        polarity_snapshot.setdefault("subwoofers", {}).setdefault("sub1", {})["polarity"] = selected_polarities[0]
        polarity_snapshot.setdefault("subwoofers", {}).setdefault("sub2", {})["polarity"] = selected_polarities[1]
        job["polarity_check"] = {**polarity_decision, "incumbent": incumbent_polarities, "selected": selected_polarities, "alternatives_tested": alternative_polarities}
        job["auto_gain"] = _calculate_auto_sub_gain(
            mode=OUTPUT_MODE_SUBWOOFER_22,
            target_curve=job.get("target_curve"), anchor=job.get("main_target_anchor"),
            winner_curves={
                "left": gain_winner.get("calibrated_points_left") or [],
                "right": gain_winner.get("calibrated_points_right") or [],
            }, crossover_hz=fc,
        )
        logger.info("AUTOSUB_GAIN mode=2.2_mono diagnostics=%s", json.dumps(job["auto_gain"], sort_keys=True))
        gain_deltas = _auto_sub_gain_deltas(job["auto_gain"], OUTPUT_MODE_SUBWOOFER_22, max_abs_db=6.0)
        _auto_sub_gain_log_line("AUTOGAIN_INIT", {
            "mode": OUTPUT_MODE_SUBWOOFER_22, "xo_hz": fc,
            "target": (job.get("target_curve") or {}).get("label"),
            "anchor_hz": (job.get("main_target_anchor") or {}).get("usable_band_hz"),
            "target_offset_db": (job.get("main_target_anchor") or {}).get("target_vertical_offset_db"),
            "gain_before": {"sub1": float(original_sub1.get("level_db", 0.0)), "sub2": float(original_sub2.get("level_db", 0.0))},
            "winner_delta_left": (job["auto_gain"].get("channels", {}).get("left") or {}).get("target_delta_db"),
            "winner_delta_right": (job["auto_gain"].get("channels", {}).get("right") or {}).get("target_delta_db"),
            "combined_delta_db": (job["auto_gain"].get("recommendation") or {}).get("raw_delta_db"),
            "first_step_db": gain_deltas.get("left"),
        })
        gain_snapshot = _auto_sub_22_snapshot_with_gain(
            polarity_snapshot,
            left_delta_db=gain_deltas.get("left", 0.0), right_delta_db=gain_deltas.get("right", 0.0),
        )
        candidate_ledger = (
            _auto_sub_candidate_ledger(
                coarse1_results, sub1_scoring, mode="2.2_mono", phase="sub1_coarse",
                roles={"coarse_winner": sub1_winner},
            )
            + _auto_sub_candidate_ledger(
                coarse2_results, sub2_scoring, mode="2.2_mono", phase="sub2_coarse",
                roles={"coarse_winner": sub2_winner},
            )
            + _auto_sub_candidate_ledger(
                matrix_results, matrix_scoring, mode="2.2_mono", phase="matrix",
                roles={
                    "matrix_winner": matrix_scoring.get("matrix_winner"),
                    "final_accepted_winner": winner,
                },
                requested_incumbent={
                    "sub1_alignment_ms": original_sub1_alignment,
                    "sub2_alignment_ms": original_sub2_alignment,
                },
            )
        )

        sub_config = _auto_sub_22_global_config(gain_snapshot)
        subwoofers_config = _auto_sub_22_candidate_subwoofers(
            gain_snapshot,
            sub1_alignment_ms=best_sub1,
            sub2_alignment_ms=best_sub2,
            active_subs=("sub1", "sub2"),
        )
        apply_ok = await _auto_sub_apply_candidate(
            output_mode=OUTPUT_MODE_SUBWOOFER_22,
            global_config=sub_config,
            subwoofers_config=subwoofers_config,
            verify=lambda overview: _auto_sub_22_verify_alignment(overview, best_sub1, best_sub2),
            load_overview=_load_audio_output_mode,
        )

        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return

        if not apply_ok:
            job["status"] = "failed"
            job["message"] = f"Scoring succeeded but failed to apply winner pair {best_sub1:.2f} / {best_sub2:.2f} ms"
            job["error"] = {"detail": "Winner apply failed - original config restored"}
            await _restore_original_config()
            return

        gain_after_sweep = await _measure_auto_sub_combined_candidate(
            delay_ms=best_sub1, job=job, candidate_index=1, total=1,
            sweep_index_start=matrix_sweep_total + 1, sweep_total=matrix_sweep_total + 2,
            stage="gain_after", fc=fc, input_id=input_id,
            mic_input_channel=mic_input_channel, reference_input_channel=reference_input_channel,
            calibration_ref=calibration_ref, calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
            auto_sub_rate=auto_sub_rate, original_level=0.0, original_polarity="normal",
            original_highpass=bool(_auto_sub_22_global_config(gain_snapshot).get("main_highpass_enabled", True)),
            output_mode=OUTPUT_MODE_SUBWOOFER_22, original_config_snapshot=gain_snapshot,
            sub1_alignment_ms=best_sub1, sub2_alignment_ms=best_sub2, active_subs=("sub1", "sub2"),
        )
        gain_after = _calculate_auto_sub_gain(
            mode=OUTPUT_MODE_SUBWOOFER_22, target_curve=job.get("target_curve"),
            anchor=job.get("main_target_anchor"), winner_curves={
                "left": gain_after_sweep.get("calibrated_points_left") or [],
                "right": gain_after_sweep.get("calibrated_points_right") or [],
            }, crossover_hz=fc,
        )
        gain_verdict = _auto_sub_gain_verdict(job["auto_gain"], gain_after, OUTPUT_MODE_SUBWOOFER_22)
        final_gain_deltas = gain_deltas if gain_verdict["accepted"] else {"left": 0.0, "right": 0.0}
        final_gain_snapshot = gain_snapshot if gain_verdict["accepted"] else polarity_snapshot
        final_gain_sweep = gain_after_sweep if gain_verdict["accepted"] else gain_winner
        correction_deltas: dict[str, float] = {}
        correction_plan = None
        correction_after = None
        correction_verdict = None
        if not gain_verdict["accepted"]:
            rollback_subs = _auto_sub_22_candidate_subwoofers(
                polarity_snapshot, sub1_alignment_ms=best_sub1, sub2_alignment_ms=best_sub2,
                active_subs=("sub1", "sub2"),
            )
            set_audio_output_mode(OUTPUT_MODE_SUBWOOFER_22, _auto_sub_22_global_config(polarity_snapshot), rollback_subs)
            if _dsp_runtime() is not None:
                await _dsp_runtime().sync(get_audio_output_overview())
        else:
            correction_plan = _auto_sub_gain_response_correction(
                job["auto_gain"], gain_after, gain_deltas, OUTPUT_MODE_SUBWOOFER_22,
            )
            correction_deltas = correction_plan.get("deltas_db") or {}
            correction_delta = correction_deltas.get("left", 0.0)
            if not correction_plan.get("available"):
                correction_verdict = {
                    "accepted": False,
                    "reason": correction_plan.get("reason"),
                    "channels": {},
                    "step1_retained": True,
                }
            elif abs(correction_delta) > 0.0005:
                correction_snapshot = _auto_sub_22_snapshot_with_gain(
                    gain_snapshot, left_delta_db=correction_delta, right_delta_db=correction_delta,
                )
                set_audio_output_mode(
                    OUTPUT_MODE_SUBWOOFER_22, _auto_sub_22_global_config(correction_snapshot),
                    _auto_sub_22_candidate_subwoofers(
                        correction_snapshot, sub1_alignment_ms=best_sub1, sub2_alignment_ms=best_sub2,
                        active_subs=("sub1", "sub2"),
                    ),
                )
                if _dsp_runtime() is not None:
                    await _dsp_runtime().sync(get_audio_output_overview())
                correction_sweep = await _measure_auto_sub_combined_candidate(
                    delay_ms=best_sub1, job=job, candidate_index=1, total=1,
                    sweep_index_start=matrix_sweep_total + 3, sweep_total=matrix_sweep_total + 4,
                    stage="gain_correction_after", fc=fc, input_id=input_id,
                    mic_input_channel=mic_input_channel, reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref, calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
                    auto_sub_rate=auto_sub_rate, original_level=0.0, original_polarity="normal",
                    original_highpass=bool(_auto_sub_22_global_config(correction_snapshot).get("main_highpass_enabled", True)),
                    output_mode=OUTPUT_MODE_SUBWOOFER_22, original_config_snapshot=correction_snapshot,
                    sub1_alignment_ms=best_sub1, sub2_alignment_ms=best_sub2, active_subs=("sub1", "sub2"),
                )
                correction_after = _calculate_auto_sub_gain(
                    mode=OUTPUT_MODE_SUBWOOFER_22, target_curve=job.get("target_curve"),
                    anchor=job.get("main_target_anchor"), winner_curves={
                        "left": correction_sweep.get("calibrated_points_left") or [],
                        "right": correction_sweep.get("calibrated_points_right") or [],
                    }, crossover_hz=fc,
                )
                correction_verdict = _auto_sub_gain_verdict(gain_after, correction_after, OUTPUT_MODE_SUBWOOFER_22)
                if correction_verdict["accepted"]:
                    final_gain_deltas = {
                        "left": gain_deltas.get("left", 0.0) + correction_delta,
                        "right": gain_deltas.get("right", 0.0) + correction_delta,
                    }
                    final_gain_snapshot = correction_snapshot
                    final_gain_sweep = correction_sweep
                else:
                    set_audio_output_mode(
                        OUTPUT_MODE_SUBWOOFER_22, _auto_sub_22_global_config(gain_snapshot),
                        _auto_sub_22_candidate_subwoofers(
                            gain_snapshot, sub1_alignment_ms=best_sub1, sub2_alignment_ms=best_sub2,
                            active_subs=("sub1", "sub2"),
                        ),
                    )
                    if _dsp_runtime() is not None:
                        await _dsp_runtime().sync(get_audio_output_overview())
        _auto_sub_gain_log_line("AUTOGAIN_FEEDBACK", {
            "gain_after_step1": {
                "sub1": float(_auto_sub_22_sub(gain_snapshot, "sub1").get("level_db", 0.0)),
                "sub2": float(_auto_sub_22_sub(gain_snapshot, "sub2").get("level_db", 0.0)),
            },
            "score_before": _auto_sub_gain_log_score(job["auto_gain"]),
            "score_after_step1": _auto_sub_gain_log_score(gain_after),
            "response_per_db_left": (((correction_plan or {}).get("channels") or {}).get("left") or {}).get("response_change_per_db"),
            "response_per_db_right": (((correction_plan or {}).get("channels") or {}).get("right") or {}).get("response_change_per_db"),
            "remaining_error_left": (gain_after.get("channels", {}).get("left") or {}).get("target_delta_db"),
            "remaining_error_right": (gain_after.get("channels", {}).get("right") or {}).get("target_delta_db"),
            "raw_correction_db": ((correction_plan or {}).get("raw_deltas_db") or {}).get("left"),
            "applied_correction_db": ((correction_plan or {}).get("applied_deltas_db") or {}).get("left"),
            "correction_step_db": correction_deltas.get("left") if correction_deltas else None,
        })
        decision = "accepted_step2" if correction_verdict and correction_verdict.get("accepted") else (
            "accepted_step1" if gain_verdict.get("accepted") else "restored"
        )
        score_final_source = correction_after if decision == "accepted_step2" else (gain_after if decision == "accepted_step1" else job["auto_gain"])
        _auto_sub_gain_log_line("AUTOGAIN_RESULT", {
            "gain_final": {
                "sub1": float(_auto_sub_22_sub(final_gain_snapshot, "sub1").get("level_db", 0.0)),
                "sub2": float(_auto_sub_22_sub(final_gain_snapshot, "sub2").get("level_db", 0.0)),
            },
            "score_final": _auto_sub_gain_log_score(score_final_source), "decision": decision,
            "reason": ((correction_verdict or gain_verdict) or {}).get("reason"),
            "delay_final": {"sub1_ms": best_sub1, "sub2_ms": best_sub2},
        })
        job["auto_gain"].update({
            "applied": bool(gain_verdict["accepted"] and gain_deltas),
            "reverted": bool(gain_deltas and not gain_verdict["accepted"]),
            "verification": gain_after, "verification_verdict": gain_verdict,
            "response_correction": correction_plan,
            "correction_deltas_db": correction_deltas,
            "correction_verification": correction_after,
            "correction_verdict": correction_verdict,
            "final_deltas_db": final_gain_deltas,
            "original_levels_db": {
                "sub1": float(original_sub1.get("level_db", 0.0)), "sub2": float(original_sub2.get("level_db", 0.0)),
            },
            "final_levels_db": {
                "sub1": float(_auto_sub_22_sub(final_gain_snapshot, "sub1").get("level_db", 0.0)),
                "sub2": float(_auto_sub_22_sub(final_gain_snapshot, "sub2").get("level_db", 0.0)),
            },
            "stage_output_peaks": (final_gain_sweep or {}).get("stage_output_peaks"),
        })

        derived_delays: dict[str, Any] = {}
        try:
            config = BassManagementConfig.from_overview(get_audio_output_overview())
            derived_delays = {
                "derived_main_delay_ms": round(config.derived_main_delay_ms, 2),
                "derived_sub1_delay_ms": round(config.derived_sub1_delay_ms, 2),
                "derived_sub2_delay_ms": round(config.derived_sub2_delay_ms, 2),
            }
        except Exception:
            derived_delays = {}

        job["combined_matrix"].update({
            "status": "completed",
            "winner": winner,
            "matrix_winner": matrix_scoring.get("matrix_winner"),
            "incumbent_winner": matrix_scoring.get("incumbent_winner"),
            "incumbent_score": matrix_scoring.get("incumbent_score"),
            "accepted_winner": matrix_scoring.get("accepted_winner"),
            "incumbent_accepted": matrix_scoring.get("incumbent_accepted"),
            "reject_reason": matrix_scoring.get("reject_reason"),
            "runner_up": matrix_scoring.get("runner_up"),
            "results": matrix_scoring["results"],
            "valid_count": len(matrix_valid),
        })
        _log_auto_sub_timing_summary(job)

        # Build baseline and confirmation measurements for before/after graph display
        all_22_sweeps = list(coarse1_results) + list(coarse2_results) + list(matrix_results)
        baseline_22_sweep = next(
            (r for r in all_22_sweeps
             if round(float(r.get("sub1_alignment_ms", r.get("delay_ms", 0.0))), 2) == round(float(original_sub1_alignment), 2)
             and round(float(r.get("sub2_alignment_ms", 0.0)), 2) == round(float(original_sub2_alignment), 2)
             and (_auto_sub_has_points(r, "points_left") or _auto_sub_has_points(r, "points_right"))),
            None,
        )
        confirm_22_sweep = final_gain_sweep if gain_verdict["accepted"] else next(
            (r for r in all_22_sweeps
             if round(float(r.get("sub1_alignment_ms", r.get("delay_ms", 0.0))), 2) == round(float(best_sub1), 2)
             and round(float(r.get("sub2_alignment_ms", 0.0)), 2) == round(float(best_sub2), 2)
             and (_auto_sub_has_points(r, "points_left") or _auto_sub_has_points(r, "points_right"))),
            None,
        )
        baseline_measurement = None
        confirmation_measurement = None
        _offset_db = _auto_sub_shared_bass_offset(
            baseline_22_sweep.get("points_left") if baseline_22_sweep else [],
            baseline_22_sweep.get("points_right") if baseline_22_sweep else [],
        )
        if baseline_22_sweep:
            baseline_measurement = _auto_sub_measurement_from_sweep(
                baseline_22_sweep, "Before", f"AutoSub 2.2 Baseline (S1 {original_sub1_alignment:.1f} / S2 {original_sub2_alignment:.1f} ms)",
                offset_db=_offset_db,
            )
        if confirm_22_sweep:
            confirmation_measurement = _auto_sub_measurement_from_sweep(
                confirm_22_sweep, "After", f"AutoSub 2.2 Optimized (S1 {best_sub1:.1f} / S2 {best_sub2:.1f} ms)",
                offset_db=_offset_db,
            )

        job["status"] = "completed"
        decision_label = "Kept 2.2 incumbent" if matrix_scoring.get("incumbent_accepted") else "Applied 2.2"
        job["message"] = (
            f"{decision_label}: Sub 1 {best_sub1:.2f} ms / Sub 2 {best_sub2:.2f} ms "
            f"(score {winner['score_pct']:.0f} %, {matrix_scoring.get('reject_reason')})"
        )
        job["result"] = {
            "mode": OUTPUT_MODE_SUBWOOFER_22,
            "original_sub1_alignment_ms": original_sub1_alignment,
            "original_sub2_alignment_ms": original_sub2_alignment,
            "suggested_sub1_alignment_ms": best_sub1,
            "suggested_sub2_alignment_ms": best_sub2,
            "applied_sub1_alignment_ms": best_sub1,
            "applied_sub2_alignment_ms": best_sub2,
            "applied": True,
            "auto_applied": True,
            "apply_decision": (
                "kept_22_incumbent"
                if matrix_scoring.get("incumbent_accepted")
                else "applied_22_combined_matrix"
            ),
            "crossover_hz": fc,
            "confidence": matrix_scoring.get("confidence", "uncertain"),
            "winner": winner,
            "matrix_winner": matrix_scoring.get("matrix_winner"),
            "incumbent_winner": matrix_scoring.get("incumbent_winner"),
            "incumbent_score": matrix_scoring.get("incumbent_score"),
            "accepted_winner": matrix_scoring.get("accepted_winner"),
            "incumbent_accepted": matrix_scoring.get("incumbent_accepted"),
            "reject_reason": matrix_scoring.get("reject_reason"),
            "sub1_coarse_winner": sub1_winner,
            "sub2_coarse_winner": sub2_winner,
            "runner_up": matrix_scoring.get("runner_up"),
            "ranking": matrix_scoring["results"],
            "combined_matrix": job["combined_matrix"],
            "candidate_ledger": candidate_ledger,
            "sweep_count": matrix_sweep_total + 2,
            "candidate_count": len(sub1_scan_delays) + len(sub2_scan_delays) + len(matrix_pairs),
            "sub1_coarse_candidate_count": len(sub1_scan_delays),
            "sub2_coarse_candidate_count": len(sub2_scan_delays),
            "matrix_candidate_count": len(matrix_pairs),
            "valid_count": len(matrix_valid),
            "sub1_coarse_valid_count": len(coarse1_valid),
            "sub2_coarse_valid_count": len(coarse2_valid),
            "baseline_measurement": baseline_measurement,
            "confirmation_measurement": confirmation_measurement,
            **derived_delays,
        }
        logger.info(
            "Auto-sub 2.2 optimize completed: fc=%sHz sub1 %.2f->%.2fms sub2 %.2f->%.2fms "
            "combined_score=%.0f%% score_L=%.1f%% score_R=%.1f%% confidence=%s",
            fc,
            original_sub1_alignment,
            best_sub1,
            original_sub2_alignment,
            best_sub2,
            winner.get("score_pct", 0),
            winner.get("score_L_pct", 0) or 0,
            winner.get("score_R_pct", 0) or 0,
            matrix_scoring.get("confidence", "uncertain"),
        )

    except Exception as exc:
        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return
        logger.exception("Auto-sub 2.2 optimize failed")
        job["status"] = "failed"
        job["message"] = f"Auto Sub Optimize 2.2 failed: {exc}"
        job["error"] = {"detail": str(exc)}
        await _restore_original_config()

    finally:
        await _finish_auto_sub_worker(job, job_id)

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
            config = BassManagementConfig.from_overview(get_audio_output_overview())
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
            set_audio_output_mode(
                OUTPUT_MODE_SUBWOOFER_22_STEREO, _auto_sub_22_global_config(gain_snapshot),
                _auto_sub_22_candidate_subwoofers(
                    gain_snapshot, sub1_alignment_ms=best_left, sub2_alignment_ms=best_right,
                    active_subs=("sub1", "sub2"),
                ),
            )
            if _dsp_runtime() is not None:
                await _dsp_runtime().sync(get_audio_output_overview())
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
            set_audio_output_mode(
                OUTPUT_MODE_SUBWOOFER_22_STEREO, _auto_sub_22_global_config(polarity_snapshot),
                _auto_sub_22_candidate_subwoofers(
                    polarity_snapshot, sub1_alignment_ms=best_left, sub2_alignment_ms=best_right,
                    active_subs=("sub1", "sub2"),
                ),
            )
            if _dsp_runtime() is not None:
                await _dsp_runtime().sync(get_audio_output_overview())
        elif not all(accepted_step1_sides.values()):
            # Stereo channels have independent Gain controls.  A regression on
            # one side must not discard a measured improvement on the other.
            set_audio_output_mode(
                OUTPUT_MODE_SUBWOOFER_22_STEREO, _auto_sub_22_global_config(final_gain_snapshot),
                _auto_sub_22_candidate_subwoofers(
                    final_gain_snapshot, sub1_alignment_ms=best_left, sub2_alignment_ms=best_right,
                    active_subs=("sub1", "sub2"),
                ),
            )
            if _dsp_runtime() is not None:
                await _dsp_runtime().sync(get_audio_output_overview())
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
                set_audio_output_mode(
                    OUTPUT_MODE_SUBWOOFER_22_STEREO, _auto_sub_22_global_config(correction_snapshot),
                    _auto_sub_22_candidate_subwoofers(
                        correction_snapshot, sub1_alignment_ms=best_left, sub2_alignment_ms=best_right,
                        active_subs=("sub1", "sub2"),
                    ),
                )
                if _dsp_runtime() is not None:
                    await _dsp_runtime().sync(get_audio_output_overview())
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
                        set_audio_output_mode(
                            OUTPUT_MODE_SUBWOOFER_22_STEREO, _auto_sub_22_global_config(final_gain_snapshot),
                            _auto_sub_22_candidate_subwoofers(
                                final_gain_snapshot, sub1_alignment_ms=best_left, sub2_alignment_ms=best_right,
                                active_subs=("sub1", "sub2"),
                            ),
                        )
                        if _dsp_runtime() is not None:
                            await _dsp_runtime().sync(get_audio_output_overview())
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
                        set_audio_output_mode(
                            OUTPUT_MODE_SUBWOOFER_22_STEREO, _auto_sub_22_global_config(gain_snapshot),
                            _auto_sub_22_candidate_subwoofers(
                                gain_snapshot, sub1_alignment_ms=best_left, sub2_alignment_ms=best_right,
                                active_subs=("sub1", "sub2"),
                            ),
                        )
                        if _dsp_runtime() is not None:
                            await _dsp_runtime().sync(get_audio_output_overview())
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
        left_confirm = final_gain_left if accepted_step1_sides["left"] else _auto_sub_result_for_delay(all_left_sweeps, best_left)
        right_confirm = final_gain_right if accepted_step1_sides["right"] else _auto_sub_result_for_delay(all_right_sweeps, best_right)

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

async def _run_auto_sub_optimize(
    job_id: str,
    input_id: str,
    channel: str,
    mic_input_channel: str,
    reference_input_channel: str,
    calibration_ref: str,
    calibration_filename: str | None,
    calibration_bytes: bytes | None,
    scan_delays: list[float],
    fc: int,
    current_alignment: float,
    original_polarity: str,
    original_level: float,
    original_highpass: bool,
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

    async def _restore_original_config():
        """Restore subwoofer config from snapshot."""
        await _restore_auto_sub_original_config(original_config_snapshot)

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

        sweep_results: list[dict[str, Any]] = []
        total = len(scan_delays) * 2

        # AutoSub bass-focused sweep settings
        auto_sub_sweep_profile = _auto_sub_sweep_profile(fc)

        # Resolve sample rate once for all sweeps
        auto_sub_rate = _resolve_measurement_start_sample_rate()
        await _capture_auto_sub_main_references(
            job=job, fc=fc, input_id=input_id,
            mic_input_channel=mic_input_channel, reference_input_channel=reference_input_channel,
            calibration_ref=calibration_ref, calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
            auto_sub_rate=auto_sub_rate, output_mode=OUTPUT_MODE_SUBWOOFER_21,
            original_config_snapshot=original_config_snapshot,
        )
        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return

        coarse_total = len(scan_delays)
        coarse_sweep_total = coarse_total * 2
        total = coarse_sweep_total
        for idx, delay_ms in enumerate(scan_delays):
            sweep_results.append(
                await _measure_auto_sub_combined_candidate(
                    delay_ms=delay_ms,
                    job=job,
                    candidate_index=idx + 1,
                    total=coarse_total,
                    sweep_index_start=(idx * 2) + 1,
                    sweep_total=coarse_sweep_total,
                    stage="coarse",
                    fc=fc,
                    input_id=input_id,
                    mic_input_channel=mic_input_channel,
                    reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref,
                    calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes,
                    auto_sub_sweep_profile=auto_sub_sweep_profile,
                    auto_sub_rate=auto_sub_rate,
                    original_level=original_level,
                    original_polarity=original_polarity,
                    original_highpass=original_highpass,
                )
            )
            if _auto_sub_cancel_requested(job):
                job["message"] = "Auto Sub Optimize cancelled."
                await _restore_original_config()
                return

            # Live: push baseline measurement to job for frontend polling display
            if round(float(delay_ms), 2) == round(float(current_alignment), 2):
                last_result = sweep_results[-1]
                if _auto_sub_has_points(last_result, "points_left") or _auto_sub_has_points(last_result, "points_right"):
                    job["baseline_measurement"] = _auto_sub_measurement_from_sweep(
                        last_result, "Before", f"AutoSub Baseline ({current_alignment:.1f} ms)"
                    )

        # Score candidates
        valid = [r for r in sweep_results if _auto_sub_has_points(r, "points_left") or _auto_sub_has_points(r, "points_right")]
        if not valid:
            job["status"] = "failed"
            job["message"] = "No valid sweep results to score"
            job["error"] = {"detail": "All sweeps failed or produced insufficient data"}
            await _restore_original_config()
            return

        step_ms = _auto_sub_step_ms(fc)
        coarse_scoring = _score_auto_sub_combined_candidates(
            sweep_results,
            crossover_hz=fc,
            low_guard_reference_delay_ms=current_alignment,
        )
        valid = list(coarse_scoring.get("scored_candidates") or valid)
        coarse_winner = coarse_scoring["winner"]
        coarse_runner_up = coarse_scoring.get("runner_up")
        fine_trigger_reasons = _auto_sub_fine_trigger_reasons(coarse_scoring, scan_delays)
        fine_delays: list[float] = []
        fine_results: list[dict[str, Any]] = []
        fine_valid: list[dict[str, Any]] = []
        fine_winner: dict[str, Any] | None = None
        fine_scoring: dict[str, Any] | None = None

        fine_scan: dict[str, Any] = {
            "enabled": bool(fine_trigger_reasons),
            "triggered": False,
            "reasons": fine_trigger_reasons,
            "step_ms": step_ms,
            "fine_step_ms": step_ms / 4.0,
            "candidates": [],
            "sweep_count": 0,
            "valid_count": 0,
            "status": "skipped" if not fine_trigger_reasons else "pending",
            "coarse_winner": coarse_winner,
            "coarse_runner_up": coarse_runner_up,
        }
        final_decision_pool = list(sweep_results)

        if fine_trigger_reasons:
            fine_delays = _auto_sub_fine_delay_candidates(coarse_winner, coarse_runner_up, step_ms, {round(float(delay), 2) for delay in scan_delays})
            fine_scan.update({
                "triggered": True,
                "candidates": fine_delays,
                "status": "running" if fine_delays else "skipped",
            })
            job["fine_scan"] = fine_scan
            if fine_delays:
                fine_candidate_total = len(fine_delays)
                fine_sweep_total = fine_candidate_total * 2
                total = coarse_sweep_total + fine_sweep_total
                reason_text = ", ".join(fine_trigger_reasons)
                job["stage"] = "fine_scan"
                job["message"] = f"Fine-Scan triggered ({reason_text}); {len(fine_delays)} candidates"
                job["progress"] = {
                    "current": coarse_sweep_total,
                    "total": total,
                    "sweep_current": coarse_sweep_total,
                    "sweep_total": total,
                    "candidate_current": 0,
                    "candidate_total": fine_candidate_total,
                    "stage": "fine",
                    "reason": reason_text,
                }
                if _auto_sub_cancel_requested(job):
                    job["message"] = "Auto Sub Optimize cancelled."
                    await _restore_original_config()
                    return
                for idx, delay_ms in enumerate(fine_delays):
                    fine_results.append(
                        await _measure_auto_sub_combined_candidate(
                            delay_ms=delay_ms,
                            job=job,
                            candidate_index=idx + 1,
                            total=fine_candidate_total,
                            sweep_index_start=coarse_sweep_total + (idx * 2) + 1,
                            sweep_total=total,
                            stage="fine",
                            fc=fc,
                            input_id=input_id,
                            mic_input_channel=mic_input_channel,
                            reference_input_channel=reference_input_channel,
                            calibration_ref=calibration_ref,
                            calibration_filename=calibration_filename,
                            calibration_bytes=calibration_bytes,
                            auto_sub_sweep_profile=auto_sub_sweep_profile,
                            auto_sub_rate=auto_sub_rate,
                            original_level=original_level,
                            original_polarity=original_polarity,
                            original_highpass=original_highpass,
                        )
                    )
                    if _auto_sub_cancel_requested(job):
                        job["message"] = "Auto Sub Optimize cancelled."
                        await _restore_original_config()
                        return

                fine_valid = [r for r in fine_results if _auto_sub_has_points(r, "points_left") or _auto_sub_has_points(r, "points_right")]
                if fine_valid:
                    fine_scoring = _score_auto_sub_combined_candidates(
                        fine_results,
                        crossover_hz=fc,
                        low_guard_reference_delay_ms=current_alignment,
                    )
                    fine_valid = list(fine_scoring.get("scored_candidates") or fine_valid)
                    fine_winner = fine_scoring["winner"]
                    fine_scan.update({
                        "status": "completed",
                        "candidate_count": len(fine_delays),
                        "sweep_count": len(fine_delays) * 2,
                        "valid_count": len(fine_valid),
                        "winner": fine_winner,
                        "runner_up": fine_scoring.get("runner_up"),
                        "results": fine_scoring["results"],
                    })
                    combined_valid = valid + fine_valid
                    final_decision_pool = list(combined_valid)
                    final_scoring = _score_auto_sub_combined_candidates(
                        combined_valid,
                        crossover_hz=fc,
                        low_guard_reference_delay_ms=current_alignment,
                    )
                    combined_valid = list(final_scoring.get("scored_candidates") or combined_valid)
                else:
                    fine_scan.update({
                        "status": "no_valid_results",
                        "candidate_count": len(fine_delays),
                        "sweep_count": len(fine_delays) * 2,
                        "valid_count": 0,
                        "winner": None,
                        "runner_up": None,
                        "results": fine_results,
                    })
                    combined_valid = valid
                    final_scoring = coarse_scoring
            else:
                fine_scan.update({
                    "status": "skipped",
                    "reason": "no fine candidates generated",
                })
                combined_valid = valid
                final_scoring = coarse_scoring
        else:
            combined_valid = valid
            final_scoring = coarse_scoring

        job["fine_scan"] = fine_scan
        _auto_sub_rank_results(final_scoring["results"])

        # Re-attach scan stage from original measured candidates (scoring creates fresh dicts)
        scan_by_delay: dict[float, str] = {}
        for result in valid:
            delay_key = round(float(result.get("delay_ms", 0.0)), 2)
            scan_by_delay[delay_key] = result.get("scan", "coarse")
        for result in fine_valid:
            delay_key = round(float(result.get("delay_ms", 0.0)), 2)
            scan_by_delay[delay_key] = result.get("scan", "fine")

        coarse_score_by_delay = {
            round(float(result.get("delay_ms", 0.0)), 2): result
            for result in coarse_scoring["results"]
        }
        for result in final_scoring["results"]:
            delay_key = round(float(result.get("delay_ms", 0.0)), 2)
            result["scan"] = scan_by_delay.get(delay_key, "coarse")
            if result["scan"] == "coarse":
                coarse_score = coarse_score_by_delay.get(delay_key)
                if coarse_score:
                    result["coarse_score"] = coarse_score.get("score")
                    result["coarse_score_pct"] = coarse_score.get("score_pct")
                    result["coarse_rank"] = coarse_score.get("rank")

        final_fine_winner = next(
            (result for result in final_scoring["results"] if result.get("scan") == "fine"),
            fine_winner,
        )
        if final_fine_winner is not None and fine_scan.get("status") == "completed":
            fine_scan["final_winner"] = final_fine_winner
        final_coarse_winner = _auto_sub_best_scan_result(final_scoring["results"], "coarse") or coarse_winner
        incumbent_winner = _auto_sub_result_for_delay(final_scoring["results"], current_alignment)
        acceptance = _auto_sub_select_accepted_winner(
            coarse_winner=final_coarse_winner,
            fine_winner=final_fine_winner if fine_scan.get("status") == "completed" else None,
            incumbent_winner=incumbent_winner,
            score_epsilon=0.001,
        )
        fine_scan["coarse_winner"] = final_coarse_winner
        fine_scan["fine_winner"] = final_fine_winner
        fine_scan["incumbent_winner"] = incumbent_winner
        fine_scan["incumbent_score"] = acceptance["incumbent_score"]
        fine_scan["accepted_winner"] = acceptance["accepted_winner"]
        fine_scan["fine_accepted"] = acceptance["fine_accepted"]
        fine_scan["reject_reason"] = acceptance["reject_reason"]

        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return

        winner = acceptance["accepted_winner"]
        best_delay = winner["delay_ms"]
        confidence = str(final_scoring.get("confidence") or "uncertain")
        runner_up = final_scoring.get("runner_up")
        winner_score_pct = float(winner.get("score_pct", 0.0) or 0.0)
        runner_score_pct = float(runner_up.get("score_pct", 0.0) or 0.0) if runner_up else 0.0
        winner_margin_pct = winner_score_pct - runner_score_pct if runner_up else 100.0
        original_score_pct = None
        original_delay_key = round(float(current_alignment), 2)
        for scored_result in final_scoring.get("results", []):
            if round(float(scored_result.get("delay_ms", 0.0)), 2) == original_delay_key:
                original_score_pct = float(scored_result.get("score_pct", 0.0) or 0.0)
                break
        score_gain_pct = winner_score_pct - original_score_pct if original_score_pct is not None else None

        auto_apply = False
        apply_decision = "not_applied_uncertain_confidence"
        if incumbent_winner is not None and round(float(best_delay), 2) == round(float(current_alignment), 2):
            apply_decision = "not_applied_incumbent_better"
        elif confidence == "clear":
            auto_apply = True
            apply_decision = "applied_clear_confidence"
        elif confidence == "close":
            if winner_margin_pct < 2.0:
                apply_decision = "not_applied_close_margin_below_2pp"
            elif score_gain_pct is not None and score_gain_pct < 3.0:
                apply_decision = "not_applied_close_gain_below_3pp"
            else:
                auto_apply = True
                apply_decision = "applied_close_confidence"
        elif (
            confidence == "uncertain"
            and score_gain_pct is not None
            and score_gain_pct >= 10.0
            and winner_score_pct >= 70.0
        ):
            auto_apply = True
            apply_decision = "applied_uncertain_large_gain"

        apply_ok = False
        applied_delay = current_alignment
        if auto_apply:
            try:
                sub_config = {
                    "crossover_frequency_hz": fc,
                    "sub_alignment_ms": best_delay,
                    "sub_level_db": original_level,
                    "sub_polarity": original_polarity,
                    "main_highpass_enabled": original_highpass,
                }
                apply_ok = await _auto_sub_apply_candidate(
                    output_mode=OUTPUT_MODE_SUBWOOFER_21,
                    global_config=sub_config,
                    subwoofers_config=None,
                    verify=lambda overview: float(overview.get("subwoofer", {}).get("sub_alignment_ms", -999)) == best_delay,
                    load_overview=_load_audio_output_mode,
                )
                if apply_ok:
                    applied_delay = best_delay
            except Exception:
                logger.exception("Auto-sub: failed to construct winner delay %.2f ms", best_delay)
        else:
            await _restore_original_config()
            apply_ok = True

        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return

        if not apply_ok:
            job["status"] = "failed"
            job["message"] = f"Scoring succeeded but failed to apply winner delay {best_delay} ms"
            job["error"] = {"detail": "Winner apply failed — original config restored"}
            await _restore_original_config()
            return

        stored_winner = winner if auto_apply else (incumbent_winner or winner)
        gain_winner = _auto_sub_result_for_delay(list(sweep_results) + list(fine_results), stored_winner.get("delay_ms", current_alignment)) or {}
        final_polarity = original_polarity
        polarity_check: dict[str, Any] = {
            "incumbent": original_polarity, "selected": original_polarity,
            "accepted": False, "reason": "incumbent_protected",
        }
        if gain_winner and auto_apply:
            opposite = _auto_sub_opposite_polarity(original_polarity)
            alt = await _measure_auto_sub_combined_candidate(
                delay_ms=applied_delay, job=job, candidate_index=1, total=1,
                sweep_index_start=total + 1, sweep_total=total + 2, stage="polarity_check", fc=fc,
                input_id=input_id, mic_input_channel=mic_input_channel,
                reference_input_channel=reference_input_channel, calibration_ref=calibration_ref,
                calibration_filename=calibration_filename, calibration_bytes=calibration_bytes,
                auto_sub_sweep_profile=auto_sub_sweep_profile, auto_sub_rate=auto_sub_rate,
                original_level=original_level, original_polarity=opposite,
                original_highpass=original_highpass,
            )
            try:
                polarity_scoring = _score_auto_sub_combined_candidates(
                    [dict(gain_winner, delay_ms=0.0), dict(alt, delay_ms=1.0)], crossover_hz=fc,
                    low_guard_reference_delay_ms=0.0,
                )
                scored_incumbent = _auto_sub_result_for_delay(polarity_scoring["results"], 0.0) or {}
                scored_alt = _auto_sub_result_for_delay(polarity_scoring["results"], 1.0) or {}
                polarity_check.update(_auto_sub_polarity_decision(scored_incumbent, scored_alt))
                polarity_check.update({"alternative": opposite, "incumbent_score": scored_incumbent.get("score"), "alternative_score": scored_alt.get("score")})
                if polarity_check["accepted"]:
                    final_polarity = opposite
                    gain_winner = alt
                    polarity_check["selected"] = opposite
                    local_delays = [
                        _auto_sub_clamped_delay(applied_delay + offset)
                        for offset in (-_auto_sub_step_ms(fc) / 2.0, -_auto_sub_step_ms(fc) / 4.0,
                                       _auto_sub_step_ms(fc) / 4.0, _auto_sub_step_ms(fc) / 2.0)
                    ]
                    polarity_fine: list[dict[str, Any]] = [alt]
                    for idx, delay in enumerate(local_delays):
                        polarity_fine.append(await _measure_auto_sub_combined_candidate(
                            delay_ms=delay, job=job, candidate_index=idx + 1, total=len(local_delays),
                            sweep_index_start=total + 3 + idx * 2, sweep_total=total + 2 + len(local_delays) * 2,
                            stage="polarity_fine", fc=fc, input_id=input_id,
                            mic_input_channel=mic_input_channel, reference_input_channel=reference_input_channel,
                            calibration_ref=calibration_ref, calibration_filename=calibration_filename,
                            calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
                            auto_sub_rate=auto_sub_rate, original_level=original_level,
                            original_polarity=opposite, original_highpass=original_highpass,
                        ))
                    fine_scored = _score_auto_sub_combined_candidates(polarity_fine, crossover_hz=fc)
                    fine_best = fine_scored["winner"]
                    fine_measured = _auto_sub_result_for_delay(polarity_fine, float(fine_best.get("delay_ms", applied_delay)))
                    if fine_measured:
                        applied_delay = float(fine_best["delay_ms"])
                        gain_winner = fine_measured
                    polarity_check["fine_scan"] = {"candidates": local_delays, "winner": fine_best}
            except Exception as exc:
                logger.warning("Auto-sub polarity check unavailable; restoring incumbent: %s", exc)
                final_polarity = original_polarity
                polarity_check.update({"accepted": False, "selected": original_polarity, "reason": "measurement_or_scoring_failed"})

            set_audio_output_mode(OUTPUT_MODE_SUBWOOFER_21, {
                "crossover_frequency_hz": fc, "sub_alignment_ms": applied_delay,
                "sub_level_db": original_level, "sub_polarity": final_polarity,
                "main_highpass_enabled": original_highpass,
            })
            if _dsp_runtime() is not None:
                await _dsp_runtime().sync(get_audio_output_overview())
        job["polarity_check"] = polarity_check
        job["auto_gain"] = _calculate_auto_sub_gain(
            mode=OUTPUT_MODE_SUBWOOFER_21,
            target_curve=job.get("target_curve"), anchor=job.get("main_target_anchor"),
            winner_curves={
                "left": gain_winner.get("calibrated_points_left") or [],
                "right": gain_winner.get("calibrated_points_right") or [],
            }, crossover_hz=fc,
        )
        logger.info("AUTOSUB_GAIN mode=2.1 diagnostics=%s", json.dumps(job["auto_gain"], sort_keys=True))
        gain_deltas = _auto_sub_gain_deltas(job["auto_gain"], OUTPUT_MODE_SUBWOOFER_21, max_abs_db=6.0)
        applied_gain_delta = gain_deltas.get("left", 0.0)
        _auto_sub_gain_log_line("AUTOGAIN_INIT", {
            "mode": OUTPUT_MODE_SUBWOOFER_21, "xo_hz": fc,
            "target": (job.get("target_curve") or {}).get("label"),
            "anchor_hz": (job.get("main_target_anchor") or {}).get("usable_band_hz"),
            "target_offset_db": (job.get("main_target_anchor") or {}).get("target_vertical_offset_db"),
            "gain_before": original_level,
            "winner_delta_left": (job["auto_gain"].get("channels", {}).get("left") or {}).get("target_delta_db"),
            "winner_delta_right": (job["auto_gain"].get("channels", {}).get("right") or {}).get("target_delta_db"),
            "combined_delta_db": (job["auto_gain"].get("recommendation") or {}).get("raw_delta_db"),
            "first_step_db": applied_gain_delta,
        })
        gained_level = max(-24.0, min(12.0, original_level + applied_gain_delta))
        if gain_deltas:
            set_audio_output_mode(OUTPUT_MODE_SUBWOOFER_21, {
                "crossover_frequency_hz": fc, "sub_alignment_ms": applied_delay,
                "sub_level_db": gained_level, "sub_polarity": final_polarity,
                "main_highpass_enabled": original_highpass,
            })
            if _dsp_runtime() is not None:
                await _dsp_runtime().sync(get_audio_output_overview())
        gain_after_sweep = await _measure_auto_sub_combined_candidate(
            delay_ms=applied_delay, job=job, candidate_index=1, total=1,
            sweep_index_start=total + 1, sweep_total=total + 2, stage="gain_after", fc=fc,
            input_id=input_id, mic_input_channel=mic_input_channel,
            reference_input_channel=reference_input_channel, calibration_ref=calibration_ref,
            calibration_filename=calibration_filename, calibration_bytes=calibration_bytes,
            auto_sub_sweep_profile=auto_sub_sweep_profile, auto_sub_rate=auto_sub_rate,
            original_level=gained_level, original_polarity=final_polarity,
            original_highpass=original_highpass, output_mode=OUTPUT_MODE_SUBWOOFER_21,
            original_config_snapshot=original_config_snapshot,
        )
        gain_after = _calculate_auto_sub_gain(
            mode=OUTPUT_MODE_SUBWOOFER_21, target_curve=job.get("target_curve"),
            anchor=job.get("main_target_anchor"), winner_curves={
                "left": gain_after_sweep.get("calibrated_points_left") or [],
                "right": gain_after_sweep.get("calibrated_points_right") or [],
            }, crossover_hz=fc,
        )
        gain_verdict = _auto_sub_gain_verdict(job["auto_gain"], gain_after, OUTPUT_MODE_SUBWOOFER_21)
        final_gain_deltas = gain_deltas if gain_verdict["accepted"] else {"left": 0.0, "right": 0.0}
        final_gain_level = gained_level if gain_verdict["accepted"] else original_level
        final_gain_sweep = gain_after_sweep if gain_verdict["accepted"] else gain_winner
        correction_deltas: dict[str, float] = {}
        correction_plan = None
        correction_after = None
        correction_verdict = None
        if not gain_verdict["accepted"]:
            set_audio_output_mode(OUTPUT_MODE_SUBWOOFER_21, {
                "crossover_frequency_hz": fc, "sub_alignment_ms": applied_delay,
                "sub_level_db": original_level, "sub_polarity": final_polarity,
                "main_highpass_enabled": original_highpass,
            })
            if _dsp_runtime() is not None:
                await _dsp_runtime().sync(get_audio_output_overview())
        else:
            correction_plan = _auto_sub_gain_response_correction(
                job["auto_gain"], gain_after, gain_deltas, OUTPUT_MODE_SUBWOOFER_21,
            )
            correction_deltas = correction_plan.get("deltas_db") or {}
            correction_delta = correction_deltas.get("left", 0.0)
            corrected_level = max(-24.0, min(12.0, gained_level + correction_delta))
            if not correction_plan.get("available"):
                correction_verdict = {
                    "accepted": False,
                    "reason": correction_plan.get("reason"),
                    "channels": {},
                    "step1_retained": True,
                }
            elif abs(correction_delta) > 0.0005:
                set_audio_output_mode(OUTPUT_MODE_SUBWOOFER_21, {
                    "crossover_frequency_hz": fc, "sub_alignment_ms": applied_delay,
                    "sub_level_db": corrected_level, "sub_polarity": final_polarity,
                    "main_highpass_enabled": original_highpass,
                })
                if _dsp_runtime() is not None:
                    await _dsp_runtime().sync(get_audio_output_overview())
                correction_sweep = await _measure_auto_sub_combined_candidate(
                    delay_ms=applied_delay, job=job, candidate_index=1, total=1,
                    sweep_index_start=total + 3, sweep_total=total + 4, stage="gain_correction_after", fc=fc,
                    input_id=input_id, mic_input_channel=mic_input_channel,
                    reference_input_channel=reference_input_channel, calibration_ref=calibration_ref,
                    calibration_filename=calibration_filename, calibration_bytes=calibration_bytes,
                    auto_sub_sweep_profile=auto_sub_sweep_profile, auto_sub_rate=auto_sub_rate,
                    original_level=corrected_level, original_polarity=final_polarity,
                    original_highpass=original_highpass, output_mode=OUTPUT_MODE_SUBWOOFER_21,
                    original_config_snapshot=original_config_snapshot,
                )
                correction_after = _calculate_auto_sub_gain(
                    mode=OUTPUT_MODE_SUBWOOFER_21, target_curve=job.get("target_curve"),
                    anchor=job.get("main_target_anchor"), winner_curves={
                        "left": correction_sweep.get("calibrated_points_left") or [],
                        "right": correction_sweep.get("calibrated_points_right") or [],
                    }, crossover_hz=fc,
                )
                correction_verdict = _auto_sub_gain_verdict(gain_after, correction_after, OUTPUT_MODE_SUBWOOFER_21)
                if correction_verdict["accepted"]:
                    final_gain_deltas = {
                        "left": applied_gain_delta + correction_delta,
                        "right": applied_gain_delta + correction_delta,
                    }
                    final_gain_level = corrected_level
                    final_gain_sweep = correction_sweep
                else:
                    set_audio_output_mode(OUTPUT_MODE_SUBWOOFER_21, {
                        "crossover_frequency_hz": fc, "sub_alignment_ms": applied_delay,
                        "sub_level_db": gained_level, "sub_polarity": final_polarity,
                        "main_highpass_enabled": original_highpass,
                    })
                    if _dsp_runtime() is not None:
                        await _dsp_runtime().sync(get_audio_output_overview())
        _auto_sub_gain_log_line("AUTOGAIN_FEEDBACK", {
            "gain_after_step1": gained_level,
            "score_before": _auto_sub_gain_log_score(job["auto_gain"]),
            "score_after_step1": _auto_sub_gain_log_score(gain_after),
            "response_per_db_left": (((correction_plan or {}).get("channels") or {}).get("left") or {}).get("response_change_per_db"),
            "response_per_db_right": (((correction_plan or {}).get("channels") or {}).get("right") or {}).get("response_change_per_db"),
            "remaining_error_left": (gain_after.get("channels", {}).get("left") or {}).get("target_delta_db"),
            "remaining_error_right": (gain_after.get("channels", {}).get("right") or {}).get("target_delta_db"),
            "raw_correction_db": ((correction_plan or {}).get("raw_deltas_db") or {}).get("left"),
            "applied_correction_db": ((correction_plan or {}).get("applied_deltas_db") or {}).get("left"),
            "correction_step_db": correction_deltas.get("left") if correction_deltas else None,
        })
        decision = "accepted_step2" if correction_verdict and correction_verdict.get("accepted") else (
            "accepted_step1" if gain_verdict.get("accepted") else "restored"
        )
        score_final_source = correction_after if decision == "accepted_step2" else (gain_after if decision == "accepted_step1" else job["auto_gain"])
        _auto_sub_gain_log_line("AUTOGAIN_RESULT", {
            "gain_final": final_gain_level, "score_final": _auto_sub_gain_log_score(score_final_source),
            "decision": decision, "reason": ((correction_verdict or gain_verdict) or {}).get("reason"),
            "delay_final": applied_delay,
        })
        job["auto_gain"].update({
            "applied": bool(gain_verdict["accepted"] and gain_deltas),
            "reverted": bool(gain_deltas and not gain_verdict["accepted"]),
            "verification": gain_after, "verification_verdict": gain_verdict,
            "response_correction": correction_plan,
            "correction_deltas_db": correction_deltas,
            "correction_verification": correction_after,
            "correction_verdict": correction_verdict,
            "final_deltas_db": final_gain_deltas,
            "original_level_db": original_level,
            "final_level_db": final_gain_level,
            "stage_output_peaks": (final_gain_sweep or {}).get("stage_output_peaks"),
        })
        stored_fine_accepted = bool(acceptance["fine_accepted"] and auto_apply)
        stored_reject_reason = acceptance["reject_reason"]
        if not auto_apply and stored_winner is incumbent_winner and round(float(best_delay), 2) != round(float(current_alignment), 2):
            stored_reject_reason = apply_decision
        fine_scan["accepted_winner"] = stored_winner
        fine_scan["fine_accepted"] = stored_fine_accepted
        fine_scan["reject_reason"] = stored_reject_reason
        candidate_ledger = (
            _auto_sub_candidate_ledger(
                sweep_results, final_scoring, mode="2.1", phase="coarse",
                roles={
                    "coarse_winner": final_coarse_winner,
                    "final_accepted_winner": stored_winner,
                },
                decision_pool=final_decision_pool,
                requested_incumbent={"delay_ms": current_alignment},
            )
            + _auto_sub_candidate_ledger(
                fine_results, final_scoring, mode="2.1", phase="fine",
                roles={"fine_winner": final_fine_winner, "final_accepted_winner": stored_winner},
                decision_pool=final_decision_pool,
                requested_incumbent={"delay_ms": current_alignment},
            )
        )

        job["status"] = "completed"
        job["message"] = (
            f"Applied: {best_delay} ms (score {winner['score_pct']:.0f} %)"
            if auto_apply
            else f"Suggested: {best_delay} ms (not applied: {confidence})"
        )
        _log_auto_sub_timing_summary(job)

        # Build baseline and confirmation measurements for before/after graph display
        baseline_measurement = None
        confirmation_measurement = None
        all_sweep_results = list(sweep_results) + list(fine_results)
        baseline_sweep = _auto_sub_result_for_delay(all_sweep_results, current_alignment)
        _offset_db = _auto_sub_shared_bass_offset(
            baseline_sweep.get("points_left") if baseline_sweep else [],
            baseline_sweep.get("points_right") if baseline_sweep else [],
        )
        if baseline_sweep and (_auto_sub_has_points(baseline_sweep, "points_left") or _auto_sub_has_points(baseline_sweep, "points_right")):
            baseline_measurement = _auto_sub_measurement_from_sweep(
                baseline_sweep, "Before", f"AutoSub Baseline ({current_alignment:.1f} ms)",
                offset_db=_offset_db,
            )
        confirm_delay = best_delay if auto_apply else current_alignment
        confirmation_sweep = final_gain_sweep if gain_verdict["accepted"] else _auto_sub_result_for_delay(all_sweep_results, confirm_delay)
        if confirmation_sweep and (_auto_sub_has_points(confirmation_sweep, "points_left") or _auto_sub_has_points(confirmation_sweep, "points_right")):
            confirm_label = "After" if auto_apply else "Current"
            confirmation_measurement = _auto_sub_measurement_from_sweep(
                confirmation_sweep, confirm_label, f"AutoSub {confirm_label} ({confirm_delay:.1f} ms)",
                offset_db=_offset_db,
            )

        job["result"] = {
            "original_alignment_ms": current_alignment,
            "suggested_alignment_ms": best_delay,
            "applied_alignment_ms": applied_delay,
            "applied_sub_alignment_ms": applied_delay,
            "applied": auto_apply,
            "auto_applied": auto_apply,
            "apply_decision": apply_decision,
            "winner_margin_pct": round(winner_margin_pct, 1),
            "score_gain_pct": round(score_gain_pct, 1) if score_gain_pct is not None else None,
            "original_score_pct": round(original_score_pct, 1) if original_score_pct is not None else None,
            "crossover_hz": fc,
            "confidence": confidence,
            "winner": winner,
            "coarse_winner": final_coarse_winner,
            "coarse_runner_up": coarse_runner_up,
            "fine_winner": final_fine_winner,
            "incumbent_winner": incumbent_winner,
            "incumbent_score": acceptance["incumbent_score"],
            "accepted_winner": stored_winner,
            "fine_accepted": stored_fine_accepted,
            "reject_reason": stored_reject_reason,
            "runner_up": final_scoring.get("runner_up"),
            "ranking": final_scoring["results"],
            "candidate_ledger": candidate_ledger,
            "sweep_count": total + 2,
            "candidate_count": coarse_total + len(fine_delays),
            "coarse_candidate_count": coarse_total,
            "fine_candidate_count": len(fine_delays),
            "coarse_sweep_count": coarse_sweep_total,
            "fine_sweep_count": len(fine_delays) * 2,
            "valid_count": len(combined_valid),
            "coarse_valid_count": len(valid),
            "fine_valid_count": len(fine_valid),
            "fine_scan": fine_scan,
            "baseline_measurement": baseline_measurement,
            "confirmation_measurement": confirmation_measurement,
        }

        logger.info(
            "Auto-sub optimize completed: fc=%sHz suggested=%.2fms applied=%s applied_delay=%.2fms combined_score=%.0f%% "
            "score_L=%.1f%% score_R=%.1f%% confidence=%s decision=%s fine_scan=%s",
            fc,
            best_delay,
            auto_apply,
            applied_delay,
            winner.get("score_pct", 0),
            winner.get("score_L_pct", 0) or 0,
            winner.get("score_R_pct", 0) or 0,
            confidence,
            apply_decision,
            fine_scan.get("status"),
        )

    except Exception as exc:
        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return
        logger.exception("Auto-sub optimize failed")
        job["status"] = "failed"
        job["message"] = f"Auto Sub Optimize failed: {exc}"
        job["error"] = {"detail": str(exc)}
        await _restore_original_config()

    finally:
        await _finish_auto_sub_worker(job, job_id)

