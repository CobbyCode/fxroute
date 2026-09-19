# SPDX-License-Identifier: AGPL-3.0-only

"""The AutoSub optimize start route."""

from __future__ import annotations

import asyncio
import copy
import json
import logging

from audio.output_ports import hardware_playback_port_fallback_from_mode
from audio.samplerate import (
    OUTPUT_MODE_SUBWOOFER_21,
    OUTPUT_MODE_SUBWOOFER_22,
    OUTPUT_MODE_SUBWOOFER_22_STEREO,
    get_audio_output_overview,
)
from datetime import (
    datetime,
    timezone,
)
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
    _auto_sub_step_ms,
)
from ..deps import (
    _AUTO_SUB_JOBS,
    _auto_sub_lock,
    _cleanup_stale_autosub_cancelling_jobs,
    _measurement_session,
    _measurement_store,
    _output_service,
    _start_auto_sub_worker,
    create_candidate_session,
    drop_candidate_owner,
    register_candidate_owner,
)
from ..jobs import (
    _capture_auto_sub_playback_gain,
    router,
)
from ..scoring import _validate_auto_sub_target_curve_snapshot
from ..roles import autosub_topology_from_state, optimizer_path, require_autosub_topology, sub_mute_mask
from .optimize import _run_auto_sub_optimize
from .optimize_22 import _run_auto_sub_22_optimize
from .optimize_22_stereo import _run_auto_sub_22_stereo_optimize

logger = logging.getLogger(__name__)

_AUTO_SUB_MAX_CALIBRATION_BYTES: int = 2 * 1024 * 1024  # 2 MiB
# Service runner IO, compiled peak prediction, prearm, winner commit and
# session release (integration slices C–G) passed their end-to-end gate.
# This internal rollout gate stays explicit: flipping it back to False
# re-locks the HTTP start route (503) without any request/environment bypass.
_AUTO_SUB_SERVICE_INTEGRATION_READY = True


def _auto_sub_output_device(overview: dict) -> tuple[str, int]:
    """Read hardware context only; legacy mode/processing is not authoritative."""
    selected = overview.get("selected_output") or {}
    output = overview.get("output_mode") or {}
    key = str(output.get("effective_output_key") or selected.get("key") or "").strip()
    channels = output.get("effective_output_channels", selected.get("channels"))
    if not key or type(channels) is not int or not 0 < channels <= 32:
        raise ValueError("Auto Sub Optimize requires a selected output with known channel capacity")
    return key, channels


def _auto_sub_runner_snapshot(state: dict, topology) -> tuple[dict, dict]:
    """Project frozen role settings into the existing algorithm's logical slots."""
    path = optimizer_path(topology)
    algorithm_mode = {"single-sub": OUTPUT_MODE_SUBWOOFER_21,
                      "dual-sub": OUTPUT_MODE_SUBWOOFER_22,
                      "stereo-subs": OUTPUT_MODE_SUBWOOFER_22_STEREO}[path]
    role_map = {f"sub{index + 1}": role for index, role in enumerate(topology.sub_roles)}
    active = state["modes"][state["active_mode"]]
    bass = active["bass_management"]
    common = {"crossover_frequency_hz": bass["frequency_hz"],
              "main_highpass_enabled": bass["main_highpass_enabled"]}
    subs = {slot: {field: active["processing"][role][field]
                   for field in ("alignment_ms", "level_db", "polarity")}
            for slot, role in role_map.items()}
    first = subs["sub1"]
    snapshot = {"mode": algorithm_mode, **common, "subwoofer": {
        **common, "sub_alignment_ms": first["alignment_ms"],
        "sub_level_db": first["level_db"], "sub_polarity": first["polarity"]}}
    if len(subs) == 2:
        snapshot["subwoofers"] = subs
    return snapshot, role_map


@router.post("/api/measurements/auto-sub-optimize/start")
async def start_auto_sub_optimize(
    input_id: str = Form(...),
    input_key: str = Form(""),
    channel: str = Form("left"),
    mic_input_channel: str = Form("1"),
    reference_input_channel: str = Form(""),
    reference_input_channel_left: str = Form(""),
    reference_input_channel_right: str = Form(""),
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

    # Reject if any measurement is already running
    if (measurement_store.has_active_measurement_job()
            or (measurement_sr_session is not None and measurement_sr_session.has_active_jobs)):
        raise HTTPException(status_code=409, detail="Another measurement is already running")

    # Clean up stale cancelling jobs before lock acquisition
    _cleanup_stale_autosub_cancelling_jobs()

    # Acquire lock before modifying AutoSub state
    try:
        await asyncio.wait_for(_auto_sub_lock.acquire(), timeout=0.5)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=423, detail="Auto Sub Optimize is already in progress")

    job_id = None
    worker = None
    try:
        service = _output_service()
        start_state = copy.deepcopy(service.load())
        overview = await asyncio.to_thread(get_audio_output_overview)
        try:
            output_key, channels = _auto_sub_output_device(overview)
            topology = require_autosub_topology(autosub_topology_from_state(
                start_state, output_key=output_key, channels=channels))
        except ValueError as exc:
            raise bad_request(exc) from exc
        original_config_snapshot, sub_role_map = _auto_sub_runner_snapshot(start_state, topology)
        output_mode = original_config_snapshot["mode"]  # Algorithm label only.
        auto_sub_playback_gain = _capture_auto_sub_playback_gain()

        config = original_config_snapshot["subwoofer"]
        fc = config["crossover_frequency_hz"]
        current_alignment = config["sub_alignment_ms"]
        current_sub2_alignment = original_config_snapshot.get("subwoofers", {}).get("sub2", {}).get("alignment_ms", 0.0)
        original_polarity = config["sub_polarity"]
        original_level = config["sub_level_db"]
        original_highpass = config["main_highpass_enabled"]

        # Compute scan range
        step_ms = _auto_sub_step_ms(fc)
        coarse_steps = 4
        scan_delays: list[float] = []
        for s in range(-coarse_steps, coarse_steps + 1):
            delay = _auto_sub_clamped_delay(current_alignment + s * step_ms)
            if not scan_delays or abs(delay - scan_delays[-1]) > 0.05:
                scan_delays.append(delay)

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

        if not _AUTO_SUB_SERVICE_INTEGRATION_READY:
            raise HTTPException(status_code=503, detail="Auto Sub Optimize output-state integration is not yet available")
        if measurement_sr_session is None:
            raise HTTPException(status_code=503, detail="Measurement session not available")
        device_context = overview.get("output_mode") or {}
        hardware_ports = (device_context.get("hardware_playback_ports")
                          or hardware_playback_port_fallback_from_mode(device_context))
        try:
            owner = create_candidate_session(
                service=service, start_state=start_state, output_key=output_key, channels=channels,
                hardware_ports=list(hardware_ports))
        except ValueError as exc:
            raise bad_request(exc) from exc
        job_id = f"auto-sub-{uuid4().hex[:12]}"
        job: dict[str, Any] = {
            "id": job_id,
            "status": "preparing",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "message": f"Auto Sub Optimize: {len(scan_delays)} candidates @ {fc} Hz",
            "result": None,
            "error": None,
            "mode": output_mode,
            "output_state_context": {
                "mode": start_state["active_mode"], "revision": start_state["revision"],
                "output_key": output_key, "channels": channels,
                "optimizer_path": optimizer_path(topology), "sub_role_map": sub_role_map,
                "sub_mute_mask": sub_mute_mask(topology),
            },
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
            # Per-side electrical references for the sweeps this job runs. The
            # single candidate funnel reads them so two references never need
            # threading through every Auto-Sub runner signature.
            "reference_channels": {
                "left": reference_input_channel_left,
                "right": reference_input_channel_right,
            },
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
        register_candidate_owner(job_id, owner)
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
            worker = _run_auto_sub_22_stereo_optimize(
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
            worker = _run_auto_sub_22_optimize(
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
        else:
            worker = _run_auto_sub_optimize(
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
        _start_auto_sub_worker(worker)
        return {"status": "ok", "job": job}
    except BaseException:
        # Before successful task scheduling the route still owns both the inert
        # session and the lock. No graph restoration is needed at this boundary.
        if worker is not None:
            worker.close()
        if job_id is not None:
            drop_candidate_owner(job_id)
            _AUTO_SUB_JOBS.pop(job_id, None)
        _auto_sub_lock.release()
        raise
