# SPDX-License-Identifier: AGPL-3.0-only

"""AutoSub job API, peak safety, sweep timing, and job finalization."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import datetime, timezone
from typing import Any, Mapping

import numpy as np
import audio.volume_contract as volume_contract
from audio.system_volume import volume_percent_to_linear_gain
from fastapi import APIRouter, HTTPException

from dsp.runtime import BassManagementConfig

from .deps import (
    _AUTO_SUB_CLEANUP_TASKS,
    _AUTO_SUB_JOBS,
    _auto_sub_lock,
    _dsp_manager,
    _measurement_session,
    _measurement_store,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_AUTO_SUB_TIMING_MARKS = [
    "config_set",
    "config_verify",
    "pre_arm",
    "sweep_start",
    "sweep_poll_done",
    "release_start",
    "release_done",
]

# Full-scale of the hardware sink float→integer conversion, the first stage
# that actually clips. The engine and its peak meter are float and never
# clip; between the predicted stage and the DAC only the sink volume applies.
_AUTO_SUB_STAGE_PEAK_LIMIT_DBFS = 0.0
_AUTO_SUB_STAGE_PEAK_MISMATCH_DB = 1.0


def _capture_auto_sub_playback_gain() -> dict[str, Any]:
    """Capture one fixed neutral source gain for an AutoSub optimization job."""
    manager = _dsp_manager()
    loudness_enabled = False
    volume_db = 0.0
    if manager is not None:
        try:
            extras = manager.load_global_extras()
            loudness = extras.get("loudness") if isinstance(extras, dict) else {}
            loudness = loudness if isinstance(loudness, dict) else {}
            get_active = getattr(manager, "get_active_preset", None)
            if callable(get_active):
                loudness_enabled = volume_contract.loudness_in_path(
                    get_active() or "", bool(loudness.get("enabled"))
                )
            else:
                loudness_enabled = bool(loudness.get("enabled"))
            if loudness_enabled:
                volume_db = float(loudness.get("params", {}).get("volumeDb", 0.0))
        except Exception as exc:
            raise RuntimeError("Could not capture the DSP Loudness volume for AutoSub") from exc
    if not math.isfinite(volume_db):
        raise RuntimeError("DSP Loudness volume for AutoSub is not finite")
    playback_gain = 10.0 ** (volume_db / 20.0) if loudness_enabled else 1.0
    if not math.isfinite(playback_gain) or playback_gain < 0.0:
        raise RuntimeError("AutoSub playback gain is not finite")
    return {
        "enabled": loudness_enabled,
        "volume_db": volume_db if loudness_enabled else 0.0,
        "linear": playback_gain,
        "source": "loudness.params.volumeDb" if loudness_enabled else "hardware-sink",
    }

def _auto_sub_job_playback_gain(job: Mapping[str, Any]) -> float:
    """Read the immutable per-job source gain, defaulting old jobs to unity."""
    payload = job.get("playback_gain")
    value = payload.get("linear", 1.0) if isinstance(payload, Mapping) else (1.0 if payload is None else payload)
    try:
        playback_gain = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("AutoSub job playback gain is invalid") from exc
    if not math.isfinite(playback_gain) or playback_gain < 0.0:
        raise RuntimeError("AutoSub job playback gain is invalid")
    return playback_gain

@router.get("/api/measurements/auto-sub-optimize/jobs/{job_id}")
async def get_auto_sub_optimize_job(job_id: str):
    job = _AUTO_SUB_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Auto Sub Optimize job not found")
    return {"status": "ok", "job": job}

@router.post("/api/measurements/auto-sub-optimize/jobs/{job_id}/cancel")
async def cancel_auto_sub_optimize_job(job_id: str):
    measurement_store = _measurement_store()
    job = _AUTO_SUB_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Auto Sub Optimize job not found")
    if str(job.get("status") or "").lower() in {"completed", "failed", "cancelled"}:
        return {"status": "ok", "job": job}

    job["cancel_requested"] = True
    job["cancelled_at"] = datetime.now(timezone.utc).isoformat()
    job["status"] = "cancelling"
    job["message"] = "Auto Sub Optimize cancelling..."
    job["error"] = None
    logger.info("AUTOSUB job=%s cancel requested", job_id)

    current_sweep_id = job.get("current_sweep_id")
    if current_sweep_id and measurement_store:
        try:
            measurement_store.cancel_job(str(current_sweep_id))
        except KeyError:
            pass
        except Exception as exc:
            logger.warning("Auto-sub: failed to cancel current sweep %s: %s", current_sweep_id, exc)

    return {"status": "ok", "job": job}

class AutoSubPeakSafetyError(RuntimeError):
    """Abort the complete AutoSub run after a native-DSP peak safety failure."""

def auto_sub_sink_gain_from_master_percent(percent: int | float, *, clamp_upper: bool = True) -> float:
    """Linear gain the hardware sink applies for a master percent.

    Same PipeWire/Pulse cubic curve as
    :func:`audio.system_volume.volume_percent_to_linear_gain` (verified on
    the .104 UMC204HD sink: 31% -> -30.5 dB, 10% -> -60.0 dB).  The peak
    safety read passes ``clamp_upper=False`` so an externally raised >100%
    master is never under-estimated.
    """
    return volume_percent_to_linear_gain(percent, clamp_upper=clamp_upper)

def _auto_sub_stage_peak_prediction(
    *, sweep_profile: dict[str, Any], sample_rate: int, channel: str,
    config: BassManagementConfig, playback_gain: float = 1.0,
    sink_gain: float = 1.0,
) -> dict[str, Any]:
    """Run the known measurement PCM through the native DSP topology.

    ``sink_gain`` is the linear amplitude gain the hardware sink applies to
    the engine output (0..1) before the float→integer conversion that clips.
    Use :func:`auto_sub_sink_gain_from_master_percent` to convert the master
    percent to this linear gain. The engine chain is linear, so folding it
    into the sweep yields the true DAC-level peaks without touching the
    Mono/Stereo routing.
    """
    rate = int(sample_rate)
    duration = float(sweep_profile["sweep_seconds"])
    count = max(2048, int(round(rate * duration)))
    t = np.arange(count, dtype=np.float64) / rate
    start_hz = float(sweep_profile["sweep_start_hz"])
    end_hz = float(sweep_profile["sweep_end_hz"])
    log_ratio = math.log(end_hz / start_hz)
    phase = 2.0 * math.pi * start_hz * duration / log_ratio * (np.exp(t * log_ratio / duration) - 1.0)
    sweep = np.sin(phase)
    fade_len = min(count // 8, max(64, int(round(rate * 0.01))))
    if fade_len > 1:
        sweep[:fade_len] *= np.linspace(0.0, 1.0, fade_len)
        sweep[-fade_len:] *= np.linspace(1.0, 0.0, fade_len)
    sweep *= 0.8 / max(float(np.max(np.abs(sweep))), 1e-12)
    try:
        source_gain = float(playback_gain)
        sink_linear = float(sink_gain)
    except (TypeError, ValueError) as exc:
        raise ValueError("playback_gain and sink_gain must be finite non-negative numbers") from exc
    if not math.isfinite(source_gain) or source_gain < 0.0:
        raise ValueError("playback_gain must be a finite non-negative number")
    if not math.isfinite(sink_linear) or sink_linear < 0.0:
        raise ValueError("sink_gain must be a finite non-negative number")
    sweep *= source_gain * sink_linear
    zeros = np.zeros_like(sweep)
    left = sweep if channel in ("left", "stereo") else zeros
    right = sweep if channel in ("right", "stereo") else zeros

    def coefficients(kind: str) -> tuple[list[float], list[float]]:
        w0 = 2.0 * math.pi * config.crossover_frequency_hz / rate
        cos_w0, sin_w0 = math.cos(w0), math.sin(w0)
        alpha = sin_w0 / (2.0 * math.sqrt(0.5))
        a0 = 1.0 + alpha
        if kind == "low":
            b = [(1.0 - cos_w0) * 0.5 / a0, (1.0 - cos_w0) / a0,
                 (1.0 - cos_w0) * 0.5 / a0]
        else:
            b = [(1.0 + cos_w0) * 0.5 / a0, -(1.0 + cos_w0) / a0,
                 (1.0 + cos_w0) * 0.5 / a0]
        return b, [1.0, -2.0 * cos_w0 / a0, (1.0 - alpha) / a0]

    def lr24(signal: np.ndarray, kind: str, enabled: bool = True) -> np.ndarray:
        if not enabled:
            return signal.copy()
        b, a = coefficients(kind)
        def one_stage(values: np.ndarray) -> np.ndarray:
            output = np.empty_like(values)
            z1 = 0.0
            z2 = 0.0
            for index, value in enumerate(values):
                filtered = b[0] * value + z1
                z1 = b[1] * value - a[1] * filtered + z2
                z2 = b[2] * value - a[2] * filtered
                output[index] = filtered
            return output
        return one_stage(one_stage(signal))

    def delay(signal: np.ndarray, delay_ms: float) -> np.ndarray:
        samples = int(float(delay_ms) * rate / 1000.0 + 0.5)
        if samples <= 0:
            return signal
        return np.concatenate((np.zeros(samples), signal))[:signal.size]

    main_l = delay(lr24(left, "high", config.main_highpass_enabled), config.derived_main_delay_ms)
    main_r = delay(lr24(right, "high", config.main_highpass_enabled), config.derived_main_delay_ms)
    if config.bass_routing == "stereo":
        sub1_source, sub2_source = left, right
    else:
        sub1_source = sub2_source = (left + right) * 0.5
    sub1 = delay(lr24(sub1_source, "low"), config.derived_sub1_delay_ms)
    sub2 = delay(lr24(sub2_source, "low"), config.derived_sub2_delay_ms)
    sub1 *= (-1.0 if config.sub_polarity == "invert" else 1.0) * 10.0 ** (config.sub_level_db / 20.0)
    sub2 *= (-1.0 if config.sub2_polarity == "invert" else 1.0) * 10.0 ** (config.sub2_level_db / 20.0)
    peaks = {
        f"output_{index + 1}": float(np.max(np.abs(signal))) if signal.size else 0.0
        for index, signal in enumerate((main_l, main_r, sub1, sub2))
    }
    peak_dbfs = {key: round(20.0 * math.log10(max(value, 1e-12)), 3) for key, value in peaks.items()}
    return {
        "linear": peaks,
        "dbfs": peak_dbfs,
        "maximum_dbfs": max(peak_dbfs.values()),
        "limit_dbfs": _AUTO_SUB_STAGE_PEAK_LIMIT_DBFS,
        "safe": max(peak_dbfs.values()) <= _AUTO_SUB_STAGE_PEAK_LIMIT_DBFS,
        "playback_gain": source_gain,
        "sink_gain": sink_linear,
    }

def _auto_sub_stage_peak_comparison(
    predicted: dict[str, Any], measured_linear: dict[str, float],
    *, sink_gain: float = 1.0,
) -> dict[str, Any]:
    try:
        sink_linear = float(sink_gain)
    except (TypeError, ValueError) as exc:
        raise ValueError("sink_gain must be a finite non-negative number") from exc
    if not math.isfinite(sink_linear) or sink_linear < 0.0:
        raise ValueError("sink_gain must be a finite non-negative number")
    # The meter reads the engine output (before the sink volume); fold the
    # sink gain in so predicted and measured are both at the DAC stage.
    folded_linear = {key: float(value) * sink_linear for key, value in measured_linear.items()}
    measured_dbfs = {
        key: round(20.0 * math.log10(max(value, 1e-12)), 3)
        for key, value in folded_linear.items()
    }
    differences = {
        key: round(measured_dbfs[key] - float(predicted["dbfs"][key]), 3)
        for key in measured_dbfs
        if max(measured_dbfs[key], float(predicted["dbfs"][key])) > -90.0
    }
    relevant = any(abs(value) > _AUTO_SUB_STAGE_PEAK_MISMATCH_DB for value in differences.values())
    return {
        "predicted": predicted,
        "measured": {"linear": folded_linear, "dbfs": measured_dbfs},
        "difference_db": differences,
        "tolerance_db": _AUTO_SUB_STAGE_PEAK_MISMATCH_DB,
        "relevant_mismatch": relevant,
        "measured_safe": max(measured_dbfs.values()) <= _AUTO_SUB_STAGE_PEAK_LIMIT_DBFS,
    }

def _auto_sub_timing_durations(marks: dict[str, float]) -> dict[str, float]:
    durations: dict[str, float] = {}
    prev_key = "start"
    for key in _AUTO_SUB_TIMING_MARKS:
        if key in marks and prev_key in marks:
            durations[f"{prev_key}_to_{key}_ms"] = round((marks[key] - marks[prev_key]) * 1000, 1)
        if key in marks:
            prev_key = key
    if "start" in marks and prev_key in marks:
        durations["total_ms"] = round((marks[prev_key] - marks["start"]) * 1000, 1)
    return durations

def _append_auto_sub_sweep_timing(
    job: dict[str, Any],
    *,
    delay_ms: float,
    channel: str,
    candidate_index: int,
    candidate_current: int | None,
    stage: str,
    status: str,
    marks: dict[str, float],
) -> None:
    job.setdefault("_sweep_timings", []).append({
        "delay_ms": delay_ms,
        "channel": channel,
        "stage": stage,
        "durations": _auto_sub_timing_durations(marks),
        "candidate": candidate_current or candidate_index,
        "sweep_index": candidate_index,
        "status": status,
    })

def _log_auto_sub_timing_summary(job: dict[str, Any]) -> None:
    timing_log = job.get("_sweep_timings", [])
    if not timing_log:
        return

    def _sum_phase(phase: str) -> float:
        return sum((t.get("durations", {}) or {}).get(phase, 0) or 0 for t in timing_log)

    total_config = _sum_phase("start_to_config_set_ms")
    total_verify = _sum_phase("config_set_to_config_verify_ms")
    total_prearm = _sum_phase("config_verify_to_pre_arm_ms")
    total_sweep = _sum_phase("pre_arm_to_sweep_start_ms")
    total_poll = _sum_phase("sweep_start_to_sweep_poll_done_ms")
    total_release = _sum_phase("sweep_poll_done_to_release_start_ms")
    total_cleanup = _sum_phase("release_start_to_release_done_ms")
    total_all = _sum_phase("total_ms")

    logger.info(
        "Auto-sub timing summary: count=%d sweeps total=%.1fs "
        "config=%.1fs verify=%.1fs prearm=%.1fs sweep=%.1fs poll=%.1fs release=%.1fs cleanup=%.1fs "
        "idle=%.1fs",
        len(timing_log), total_all / 1000,
        total_config / 1000, total_verify / 1000, total_prearm / 1000,
        total_sweep / 1000, total_poll / 1000, total_release / 1000, total_cleanup / 1000,
        max(0, (total_all - total_config - total_verify - total_prearm - total_sweep - total_poll - total_release - total_cleanup)) / 1000,
    )

    l_timings = [t for t in timing_log if t.get("channel") == "left"]
    r_timings = [t for t in timing_log if t.get("channel") == "right"]
    if l_timings:
        l_avg = sum((t.get("durations", {}) or {}).get("total_ms", 0) or 0 for t in l_timings) / len(l_timings)
        r_avg = (
            sum((t.get("durations", {}) or {}).get("total_ms", 0) or 0 for t in r_timings) / len(r_timings)
            if r_timings
            else 0
        )
        logger.info("Auto-sub timing: L avg=%.1fms R avg=%.1fms", l_avg, r_avg)

def _finalize_autosub_job(job: dict[str, Any] | None, job_id: str) -> None:
    """Transition an AutoSub job to cancelled and log cleanup.

    Cancellation semantics:

    - The cancel endpoint sets cancel_requested only on non-terminal jobs,
      so status="completed" together with cancel_requested=True means the
      cancel request arrived BEFORE the worker committed completion; such a
      job is finalized as cancelled.
    - A genuine worker failure is never relabelled cancelled by the
      finalizer, even when a cancel was requested concurrently.
    - A job already final as cancelled stays cancelled; a job that
      completed without a pending cancel request stays completed.
    """
    if job is None:
        logger.warning("AUTOSUB job=%s worker finished (job missing)", job_id)
        return
    status = str(job.get("status") or "").lower()
    cancel_requested = bool(job.get("cancel_requested"))
    if status in {"failed", "cancelled"}:
        pass
    elif status == "cancelling" or cancel_requested:
        job["status"] = "cancelled"
        job["message"] = "Auto Sub Optimize cancelled."
        job["cancel_requested"] = True
        job["result"] = None
        job["error"] = None
        logger.info(
            "AUTOSUB job=%s worker finished (cancel committed; prior status=%s)",
            job_id, status,
        )
    if isinstance(job.get("result"), dict):
        job["result"]["target_curve"] = json.loads(json.dumps(job.get("target_curve"))) if job.get("target_curve") else None
        job["result"]["auto_gain"] = json.loads(json.dumps(job.get("auto_gain")))
        job["result"]["main_references"] = json.loads(json.dumps(job.get("main_references"))) if job.get("main_references") else None
        job["result"]["main_target_anchor"] = json.loads(json.dumps(job.get("main_target_anchor"))) if job.get("main_target_anchor") else None
        job["result"]["polarity_check"] = json.loads(json.dumps(job.get("polarity_check"))) if job.get("polarity_check") else None
    logger.info("AUTOSUB job=%s cleanup complete state=%s", job_id, job.get("status") or "idle")

async def _finish_auto_sub_worker(job: dict[str, Any] | None, job_id: str) -> None:
    """Release the shared AutoSub lock, finalize the job and schedule its cleanup task.

    Ownership structure: the lock is released unconditionally in the outer
    finally.  Neither a failing sample-rate session unregister nor a failing
    finalize may prevent the release, otherwise every subsequent AutoSub
    operation would block forever with 423.  Cleanup failures are logged and
    never overwrite the worker/job outcome (a failed job stays failed, a
    cancelled job stays cancelled).
    """
    measurement_sr_session = _measurement_session()
    try:
        if measurement_sr_session is not None:
            try:
                await measurement_sr_session.unregister_auto_sub(job_id)
            except Exception:
                logger.exception(
                    "AUTOSUB job=%s measurement sample-rate session unregister failed", job_id
                )
        try:
            _finalize_autosub_job(job, job_id)
        except Exception:
            logger.exception("AUTOSUB job=%s finalize failed", job_id)
    finally:
        try:
            _auto_sub_lock.release()
        except RuntimeError:
            # The lock is not held (already released); the job status is final.
            logger.debug("AUTOSUB job=%s lock release skipped (not held)", job_id)

    async def _cleanup_autosub_job():
        await asyncio.sleep(600)
        _AUTO_SUB_JOBS.pop(job_id, None)

    cleanup_task = asyncio.create_task(_cleanup_autosub_job())
    _AUTO_SUB_CLEANUP_TASKS.add(cleanup_task)
    cleanup_task.add_done_callback(_AUTO_SUB_CLEANUP_TASKS.discard)

