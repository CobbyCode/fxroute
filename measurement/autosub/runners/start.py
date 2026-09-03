# SPDX-License-Identifier: AGPL-3.0-only

"""The AutoSub optimize start route."""

from __future__ import annotations

import asyncio
import json
import logging

from audio.samplerate import (
    OUTPUT_MODE_SUBWOOFER_22,
    OUTPUT_MODE_SUBWOOFER_22_MODES,
    OUTPUT_MODE_SUBWOOFER_22_STEREO,
    OUTPUT_MODE_SUBWOOFER_MODES,
    get_audio_output_overview,
)
from datetime import (
    datetime,
    timezone,
)
from dsp.runtime import BassManagementConfig
from fastapi import (
    File,
    Form,
    HTTPException,
    UploadFile,
)
from http_errors import bad_request
from typing import Any
from uploads import (
    UploadTooLargeError,
    read_upload,
)
from uuid import uuid4
from ..candidates import (
    _auto_sub_clamped_delay,
    _auto_sub_snapshot_copy,
    _auto_sub_step_ms,
)
from ..deps import (
    _AUTO_SUB_JOBS,
    _auto_sub_lock,
    _cleanup_stale_autosub_cancelling_jobs,
    _measurement_session,
    _measurement_store,
    _start_auto_sub_worker,
)
from ..jobs import (
    _capture_auto_sub_playback_gain,
    router,
)
from ..scoring import _validate_auto_sub_target_curve_snapshot
from .optimize import _run_auto_sub_optimize
from .optimize_22 import _run_auto_sub_22_optimize
from .optimize_22_stereo import _run_auto_sub_22_stereo_optimize

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

        config = BassManagementConfig.from_overview(await asyncio.to_thread(get_audio_output_overview))
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

        # Read and validate the calibration upload before the job is
        # registered in _AUTO_SUB_JOBS: a rejection here must not leave an
        # orphaned "preparing" job behind (no worker is started and the
        # 600 s cleanup only ever runs from the worker finalizer).
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
