# SPDX-License-Identifier: AGPL-3.0-only

"""Candidate measurement, target analysis, smoothing, and gain analysis."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import statistics
import time
from typing import Any

from audio.samplerate import (
    OUTPUT_MODE_SUBWOOFER_21,
    OUTPUT_MODE_SUBWOOFER_22,
    OUTPUT_MODE_SUBWOOFER_22_MODES,
    OUTPUT_MODE_SUBWOOFER_22_STEREO,
    get_audio_output_overview,
    set_audio_output_mode,
)
from audio.system_volume import get_output_volume_unclamped
from dsp.runtime import BassManagementConfig

from measurement.analyzer import measurement_level_reference_db
from measurement.constants import LEVEL_REFERENCE_MAX_HZ, LEVEL_REFERENCE_MIN_HZ
from measurement.store import auto_sub_chain_anchor_db, default_measurement_sweep_profile

from .candidates import (
    _auto_sub_22_candidate_subwoofers,
    _auto_sub_22_global_config,
    _auto_sub_22_name,
    _auto_sub_22_sub,
    _auto_sub_22_verify_alignment,
    _auto_sub_cancelled_candidate,
    _auto_sub_clamped_delay,
    _auto_sub_snapshot_copy,
    _auto_sub_sync_dsp_runtime,
)
from .deps import _auto_sub_cancel_requested, _dsp_runtime, _measurement_store
from .jobs import (
    _AUTO_SUB_STAGE_PEAK_LIMIT_DBFS,
    _append_auto_sub_sweep_timing,
    _auto_sub_job_playback_gain,
    _auto_sub_stage_peak_comparison,
    _auto_sub_stage_peak_prediction,
    auto_sub_sink_gain_from_master_percent,
    AutoSubPeakSafetyError,
    AutoSubChainHealthError,
)

logger = logging.getLogger(__name__)


async def _auto_sub_fresh_master_percent() -> int:
    """Live, unclamped sink master percent for the pre-sweep peak safety.

    The non-blocking status cache is not authoritative for a safety
    decision: a stale low value would under-estimate the DAC gain and could
    admit a sweep that clips.  A failed live read conservatively assumes
    100% (the largest gain FXRoute itself can apply), so a stale or
    unreadable low master never releases a potentially dangerous sweep.
    """
    try:
        return await asyncio.to_thread(get_output_volume_unclamped)
    except Exception as exc:
        logger.warning(
            "Auto-sub: live master volume read failed; assuming 100%% for peak safety: %s",
            exc,
        )
        return 100


async def _measure_auto_sub_candidate(
    *,
    delay_ms: float,
    job: dict[str, Any],
    candidate_index: int,
    total: int,
    stage: str,
    fc: int,
    input_id: str,
    channel: str,
    mic_input_channel: str,
    reference_input_channel: str,
    calibration_ref: str,
    calibration_filename: str | None,
    calibration_bytes: bytes | None,
    auto_sub_sweep_profile: dict[str, Any],
    auto_sub_rate: int,
    original_level: float,
    original_polarity: str,
    original_highpass: bool,
    measurement_label: str | None = None,
    candidate_current: int | None = None,
    candidate_total: int | None = None,
    measure_channel: str | None = None,
    output_mode: str = OUTPUT_MODE_SUBWOOFER_21,
    original_config_snapshot: dict[str, Any] | None = None,
    sub1_alignment_ms: float | None = None,
    sub2_alignment_ms: float | None = None,
    active_subs: tuple[str, ...] = ("sub1",),
    sub1_polarity: str | None = None,
    sub2_polarity: str | None = None,
    exact_sub_mute: bool = False,
) -> dict[str, Any]:
    """Measure one AutoSub delay candidate with the standard safety checks."""
    measurement_store = _measurement_store()
    from measurement.session import _sync_dsp_runtime_for_measurement_sweep
    from audio.samplerate import _load_audio_output_mode

    _marks = {"start": time.monotonic()}
    _timing_written = False

    def _return_candidate(result: dict[str, Any]) -> dict[str, Any]:
        nonlocal _timing_written
        if not _timing_written:
            _marks.setdefault("release_done", time.monotonic())
            _append_auto_sub_sweep_timing(
                job,
                delay_ms=delay_ms,
                channel=measure_channel or channel,
                candidate_index=candidate_index,
                candidate_current=candidate_current,
                stage=stage,
                status=str(result.get("status") or "unknown"),
                marks=_marks,
            )
            _timing_written = True
        return result

    if _auto_sub_cancel_requested(job):
        return _return_candidate(_auto_sub_cancelled_candidate(delay_ms, stage))

    label = "Fine-Scan" if stage == "fine" else "Coarse scan"
    job["status"] = "running"
    job["message"] = measurement_label or f"{label}: sweep {candidate_index}/{total} @ sub_alignment_ms={delay_ms:.2f} ms"
    job["progress"] = {
        "current": candidate_index,
        "total": total,
        "delay_ms": delay_ms,
        "stage": stage,
        "sweep_current": candidate_index,
        "sweep_total": total,
    }
    if candidate_current is not None and candidate_total is not None:
        job["progress"]["candidate_current"] = candidate_current
        job["progress"]["candidate_total"] = candidate_total
    if measure_channel:
        job["progress"]["channel"] = measure_channel

    config_success = False
    try:
        if output_mode in OUTPUT_MODE_SUBWOOFER_22_MODES:
            snapshot = original_config_snapshot or {}
            sub1_delay = _auto_sub_clamped_delay(sub1_alignment_ms if sub1_alignment_ms is not None else delay_ms)
            sub2_delay = _auto_sub_clamped_delay(sub2_alignment_ms if sub2_alignment_ms is not None else _auto_sub_22_sub(snapshot, "sub2").get("alignment_ms", 0.0))
            sub_config = _auto_sub_22_global_config(snapshot)
            subwoofers_config = _auto_sub_22_candidate_subwoofers(
                snapshot,
                sub1_alignment_ms=sub1_delay,
                sub2_alignment_ms=sub2_delay,
                active_subs=active_subs,
                sub1_polarity=sub1_polarity,
                sub2_polarity=sub2_polarity,
            )
            persisted_overview = await asyncio.to_thread(
                set_audio_output_mode, output_mode, sub_config, subwoofers_config,
            )
        else:
            sub_config = {
                "crossover_frequency_hz": fc,
                "sub_alignment_ms": delay_ms,
                "sub_level_db": original_level,
                "sub_polarity": original_polarity,
                "main_highpass_enabled": original_highpass,
            }
            persisted_overview = await asyncio.to_thread(
                set_audio_output_mode, OUTPUT_MODE_SUBWOOFER_21, sub_config,
            )
        if _dsp_runtime() is not None:
            await _auto_sub_sync_dsp_runtime(
                output_mode=output_mode,
                persisted_overview=persisted_overview,
            )
        _marks["config_set"] = time.monotonic()
        await asyncio.sleep(0.5)
        if _auto_sub_cancel_requested(job):
            return _return_candidate(_auto_sub_cancelled_candidate(delay_ms, stage))
        verify = _load_audio_output_mode()
        if output_mode in OUTPUT_MODE_SUBWOOFER_22_MODES:
            config_success = _auto_sub_22_verify_alignment(verify, sub1_delay, sub2_delay)
        else:
            config_success = float(verify.get("subwoofer", {}).get("sub_alignment_ms", -999)) == delay_ms
        if not config_success:
            await asyncio.sleep(0.15)
            if _auto_sub_cancel_requested(job):
                return _return_candidate(_auto_sub_cancelled_candidate(delay_ms, stage))
            verify = _load_audio_output_mode()
            if output_mode in OUTPUT_MODE_SUBWOOFER_22_MODES:
                config_success = _auto_sub_22_verify_alignment(verify, sub1_delay, sub2_delay)
            else:
                config_success = float(verify.get("subwoofer", {}).get("sub_alignment_ms", -999)) == delay_ms
            if not config_success:
                await asyncio.sleep(0.5)
                if _auto_sub_cancel_requested(job):
                    return _return_candidate(_auto_sub_cancelled_candidate(delay_ms, stage))
                verify = _load_audio_output_mode()
                if output_mode in OUTPUT_MODE_SUBWOOFER_22_MODES:
                    config_success = _auto_sub_22_verify_alignment(verify, sub1_delay, sub2_delay)
                else:
                    config_success = float(verify.get("subwoofer", {}).get("sub_alignment_ms", -999)) == delay_ms
        _marks["config_verify"] = time.monotonic()
    except Exception as exc:
        logger.warning("Auto-sub: failed to configure delay %.2f ms: %s", delay_ms, exc)

    if not config_success:
        logger.warning("Auto-sub: skipping candidate %.2f ms — config sync failed", delay_ms)
        return _return_candidate({
            "delay_ms": delay_ms,
            "name": str(delay_ms),
            "points": [],
            "sweep_id": "",
            "status": "config_failed",
            "error": "Subwoofer config sync failed",
            "scan": stage,
        })

    try:
        await _sync_dsp_runtime_for_measurement_sweep(auto_sub_rate)
        _marks["pre_arm"] = time.monotonic()
        if _auto_sub_cancel_requested(job):
            return _return_candidate(_auto_sub_cancelled_candidate(delay_ms, stage))
    except Exception as exc:
        logger.exception("Auto-sub: pre-arm failed for delay %.2f ms", delay_ms)
        return _return_candidate({
            "delay_ms": delay_ms,
            "name": str(delay_ms),
            "points": [],
            "sweep_id": "",
            "status": "pre_arm_failed",
            "error": str(exc),
            "scan": stage,
        })
    playback_gain = _auto_sub_job_playback_gain(job)
    master_percent = await _auto_sub_fresh_master_percent()
    # The sink applies the master volume as a float-domain gain before the
    # float→integer conversion; its transfer curve is the measured PA cubic
    # (percent/100)**3, so the prediction folds in the resulting linear gain.
    # clamp_upper=False keeps an externally raised >100% master from being
    # under-estimated by the safety check.
    sink_gain = auto_sub_sink_gain_from_master_percent(master_percent, clamp_upper=False)
    stage_peak_prediction = _auto_sub_stage_peak_prediction(
        sweep_profile=auto_sub_sweep_profile,
        sample_rate=auto_sub_rate,
        channel=channel,
        config=BassManagementConfig.from_overview(
            await asyncio.to_thread(get_audio_output_overview),
        ),
        playback_gain=playback_gain,
        sink_gain=sink_gain,
    )
    if exact_sub_mute:
        for key in ("output_3", "output_4"):
            stage_peak_prediction["linear"][key] = 0.0
            stage_peak_prediction["dbfs"][key] = -240.0
        stage_peak_prediction["maximum_dbfs"] = max(stage_peak_prediction["dbfs"].values())
        stage_peak_prediction["safe"] = stage_peak_prediction["maximum_dbfs"] <= _AUTO_SUB_STAGE_PEAK_LIMIT_DBFS
    if not stage_peak_prediction["safe"]:
        logger.error(
            "Auto-sub: blocked unsafe sweep candidate stage=%s delay=%.2f predicted=%s",
            stage, delay_ms, stage_peak_prediction["dbfs"],
        )
        job["message"] = (
            f"AutoGain candidate blocked before sweep: predicted DAC peak "
            f"{stage_peak_prediction['maximum_dbfs']:.2f} dBFS exceeds 0 dBFS "
            f"(master volume {master_percent}%)"
        )
        peak_failure = {"predicted": stage_peak_prediction, "status": "headroom_blocked"}
        job.setdefault("auto_gain", {})["stage_output_peaks"] = peak_failure
        raise AutoSubPeakSafetyError(job["message"])
    sweep_id = ""
    stage_peak_comparison: dict[str, Any] | None = None
    previous_exact_sub_mute = False
    exact_sub_mute_enabled = False
    try:
        if exact_sub_mute:
            if _dsp_runtime() is None:
                raise RuntimeError("Subwoofer runtime unavailable; exact digital mute cannot be enabled")
            previous_exact_sub_mute = await _dsp_runtime().set_exact_sub_mute(True)
            exact_sub_mute_enabled = True
            if not _dsp_runtime().snapshot().get("exact_sub_mute"):
                raise RuntimeError("Subwoofer helper did not retain exact digital mute state")
        if _dsp_runtime() is None:
            raise RuntimeError("Native DSP output peak capture unavailable")
        await _dsp_runtime().reset_output_peaks()
        sweep_job = await measurement_store.start_measurement(
            input_id=input_id,
            channel=channel,
            mic_input_channel=mic_input_channel,
            reference_input_channel=reference_input_channel,
            calibration_ref=calibration_ref,
            calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes,
            sweep_profile=auto_sub_sweep_profile,
            measurement_scope="raw_helper",
            playback_gain=playback_gain,
        )
        sweep_id = sweep_job["id"]
        job["current_sweep_id"] = sweep_id
        _marks["sweep_start"] = time.monotonic()

        if _auto_sub_cancel_requested(job):
            try:
                measurement_store.cancel_job(sweep_id)
            except Exception:
                pass
            job["current_sweep_id"] = ""
            return _return_candidate(_auto_sub_cancelled_candidate(delay_ms, stage))

        sweep_ok = False
        for _poll in range(120):
            if _auto_sub_cancel_requested(job):
                try:
                    measurement_store.cancel_job(sweep_id)
                except Exception:
                    pass
                if job.get("current_sweep_id") == sweep_id:
                    job["current_sweep_id"] = ""
                return _return_candidate(_auto_sub_cancelled_candidate(delay_ms, stage))
            await asyncio.sleep(0.5)
            try:
                current = measurement_store.get_job(sweep_id)
            except KeyError:
                sweep_ok = True
                break
            if current.get("status") in ("completed", "failed", "cancelled"):
                sweep_ok = True
                break

        if not sweep_ok:
            logger.warning("Auto-sub: sweep %s timed out (delay %.2f ms), cancelling", sweep_id, delay_ms)
            try:
                measurement_store.cancel_job(sweep_id)
            except Exception:
                pass
            await asyncio.sleep(0.5)

        _marks["sweep_poll_done"] = time.monotonic()
        measured_stage_peaks = await _dsp_runtime().read_output_peaks()
        stage_peak_comparison = _auto_sub_stage_peak_comparison(
            stage_peak_prediction, measured_stage_peaks, sink_gain=sink_gain,
        )
        if stage_peak_comparison["relevant_mismatch"] or not stage_peak_comparison["measured_safe"]:
            reason = (
                "Native DSP peak mismatch"
                if stage_peak_comparison["relevant_mismatch"]
                else "Native DSP measured peak exceeded 0 dBFS at the DAC"
            )
            logger.error("Auto-sub stopped: %s diagnostics=%s", reason, json.dumps(stage_peak_comparison, sort_keys=True))
            job["message"] = f"Auto Sub stopped: {reason}"
            job.setdefault("auto_gain", {})["stage_output_peaks"] = stage_peak_comparison
            if job.get("current_sweep_id") == sweep_id:
                job["current_sweep_id"] = ""
            raise AutoSubPeakSafetyError(reason)
        _marks["release_start"] = time.monotonic()
        _marks["release_done"] = time.monotonic()

        try:
            final = measurement_store.get_job(sweep_id)
        except KeyError:
            if job.get("current_sweep_id") == sweep_id:
                job["current_sweep_id"] = ""
            logger.warning("Auto-sub: sweep job disappeared after completion polling: %s", sweep_id)
            return _return_candidate({
                "delay_ms": delay_ms,
                "name": str(delay_ms),
                "points": [],
                "sweep_id": sweep_id,
                "status": "cancelled",
                "error": "Sweep job disappeared",
                "scan": stage,
            })
        if final.get("status") == "completed" and final.get("result"):
            result = final["result"]
            measurement = result.get("measurement") or {}
            points = []
            for t in (measurement.get("traces") or []):
                if t.get("kind") == "sweep-response":
                    points = t.get("points") or []
                    break
            if not points:
                for t in (measurement.get("review_traces") or []):
                    points = t.get("points") or []
                    if points:
                        break
            if not points:
                logger.warning("Auto-sub: no points in sweep result for delay %.2f ms", delay_ms)
            if job.get("current_sweep_id") == sweep_id:
                job["current_sweep_id"] = ""
            analysis = measurement.get("analysis") if isinstance(measurement.get("analysis"), dict) else {}
            normalized_by_db = analysis.get("normalized_by_db")
            calibrated_points = None
            if normalized_by_db is not None:
                calibrated_points = _auto_sub_reconstruct_calibrated_points(points, normalized_by_db)
            chain_health = _auto_sub_chain_health_check(
                job, analysis.get("alignment_samples"), analysis.get("sample_rate"),
            )
            if chain_health:
                logger.error(
                    "AUTOSUB_CHAIN_HEALTH job=%s channel=%s delay=%.2f %s",
                    job.get("id") or "", channel, delay_ms, json.dumps(chain_health, sort_keys=True),
                )
                raise AutoSubChainHealthError(
                    "AutoSub stopped: the capture chain arrival shifted by "
                    f"{chain_health['arrival_shift_samples'] / 1000:.1f} ms against the run baseline "
                    "(audio device state degraded) — reset the audio stack (or reboot) and retry",
                )
            return _return_candidate({
                "delay_ms": delay_ms,
                "name": str(delay_ms),
                "points": points,
                "sweep_id": sweep_id,
                "status": "completed",
                "scan": stage,
                "normalized_by_db": normalized_by_db,
                "calibrated_points": calibrated_points,
                "exact_sub_mute": bool(exact_sub_mute),
                "alignment_samples": analysis.get("alignment_samples"),
                "measurement_channel": str(measurement.get("channel") or channel),
                "sample_rate": analysis.get("sample_rate"),
                "stage_output_peaks": stage_peak_comparison,
            })

        error_msg = final.get("error", {}).get("detail") if isinstance(final.get("error"), dict) else str(final.get("error") or "timeout")
        logger.warning("Auto-sub: sweep failed for delay %.2f ms: %s", delay_ms, error_msg)
        if job.get("current_sweep_id") == sweep_id:
            job["current_sweep_id"] = ""
        return _return_candidate({
            "delay_ms": delay_ms,
            "name": str(delay_ms),
            "points": [],
            "sweep_id": sweep_id,
            "status": "failed",
            "error": error_msg,
            "scan": stage,
            "stage_output_peaks": stage_peak_comparison or {"predicted": stage_peak_prediction},
        })
    except AutoSubPeakSafetyError:
        raise
    except AutoSubChainHealthError:
        raise
    except Exception as exc:
        logger.exception("Auto-sub: sweep error for delay %.2f ms", delay_ms)
        if job.get("current_sweep_id") == sweep_id:
            job["current_sweep_id"] = ""
        return _return_candidate({
            "delay_ms": delay_ms,
            "name": str(delay_ms),
            "points": [],
            "sweep_id": sweep_id,
            "status": "error",
            "error": str(exc),
            "scan": stage,
        })
    finally:
        if exact_sub_mute_enabled and _dsp_runtime() is not None:
            try:
                await _dsp_runtime().set_exact_sub_mute(previous_exact_sub_mute)
            except Exception as exc:
                logger.exception("Auto-sub: failed to restore exact sub mute after %s reference", channel)
                job["auto_gain"] = {
                    "available": False,
                    "reason": f"Exact sub mute restore failed after Main-only {channel}: {exc}",
                }
                raise RuntimeError(
                    f"AutoSub stopped: exact sub mute restoration was not acknowledged after Main-only {channel}"
                ) from exc

def _auto_sub_reconstruct_calibrated_points(
    normalized_points: list[list[float]], normalized_by_db: Any,
) -> list[list[float]]:
    """Undo MeasurementStore normalization: normalized = raw - normalized_by."""
    offset = float(normalized_by_db)
    if not math.isfinite(offset):
        raise ValueError("normalized_by_db must be finite")
    reconstructed: list[list[float]] = []
    for point in normalized_points:
        frequency_hz, normalized_db = float(point[0]), float(point[1])
        if not math.isfinite(frequency_hz) or frequency_hz <= 0 or not math.isfinite(normalized_db):
            raise ValueError("Measurement point must contain finite positive frequency and finite dB")
        reconstructed.append([frequency_hz, round(normalized_db + offset, 3)])
    return reconstructed

def _auto_sub_log_interpolate_points(
    points: list[list[float]], frequencies_hz: list[float],
) -> list[list[float]]:
    """Interpolate dB values linearly in log-frequency, without extrapolation."""
    source = [(float(point[0]), float(point[1])) for point in points]
    if len(source) < 2:
        raise ValueError("At least two interpolation points are required")
    if any(not math.isfinite(frequency) or frequency <= 0 or not math.isfinite(db) for frequency, db in source):
        raise ValueError("Interpolation points must contain finite positive frequencies and finite dB")
    if any(source[index][0] >= source[index + 1][0] for index in range(len(source) - 1)):
        raise ValueError("Interpolation frequencies must be strictly increasing")
    result: list[list[float]] = []
    source_index = 0
    for requested in frequencies_hz:
        frequency = float(requested)
        if not math.isfinite(frequency) or frequency <= 0:
            raise ValueError("Requested interpolation frequency must be finite and positive")
        if frequency < source[0][0] or frequency > source[-1][0]:
            continue
        while source_index + 1 < len(source) and source[source_index + 1][0] < frequency:
            source_index += 1
        if source[source_index][0] == frequency:
            value = source[source_index][1]
        elif source_index + 1 < len(source) and source[source_index + 1][0] == frequency:
            value = source[source_index + 1][1]
        else:
            low_frequency, low_db = source[source_index]
            high_frequency, high_db = source[source_index + 1]
            fraction = math.log(frequency / low_frequency) / math.log(high_frequency / low_frequency)
            value = low_db + fraction * (high_db - low_db)
        result.append([frequency, round(value, 6)])
    return result

def _analyze_auto_sub_main_target_anchor(
    *, target_curve: dict[str, Any] | None, main_references: dict[str, Any] | None,
    crossover_hz: int, main_highpass_enabled: bool,
) -> dict[str, Any]:
    """Build immutable Main/Target alignment diagnostics; never calculate Gain."""
    diagnostics: dict[str, Any] = {
        "status": "unavailable",
        "reason": None,
        "method": "calibrated Main-only points with log-frequency Target interpolation",
        "gain_calculated": False,
        "crossover_frequency_hz": int(crossover_hz),
        "main_highpass_enabled": bool(main_highpass_enabled),
        "reference_band_hz": [LEVEL_REFERENCE_MIN_HZ, LEVEL_REFERENCE_MAX_HZ],
        "criteria": {
            "level_reference": "normal measurement median from 120-8000 Hz, applied to calibrated Main minus relative Target",
            "lower_bound_rule": "maximum of common support and normal measurement level-reference minimum",
            "upper_bound_rule": "minimum of common support and normal measurement level-reference maximum",
            "minimum_points_per_side": 8,
            "minimum_log_span_octaves": 1.0,
            "support_qc_scope": "structural sampling adequacy only; it does not assert acoustic correctness",
            "target_extrapolation": False,
            "both_sides_required": True,
        },
        "sides": {},
    }
    if not isinstance(target_curve, dict) or len(target_curve.get("points") or []) < 2:
        diagnostics["reason"] = "Target snapshot unavailable"
        return diagnostics
    if not isinstance(main_references, dict) or main_references.get("status") != "completed":
        diagnostics["reason"] = "Completed Main-only L/R snapshots unavailable"
        return diagnostics
    target_points = target_curve.get("points") or []
    try:
        target_min = float(target_points[0][0])
        target_max = float(target_points[-1][0])
        side_points: dict[str, list[list[float]]] = {}
        sample_rates: dict[str, float] = {}
        for side in ("left", "right"):
            reference = main_references.get(side)
            points = reference.get("points") if isinstance(reference, dict) else None
            if not isinstance(reference, dict) or reference.get("status") != "completed":
                raise ValueError(f"Main-only {side} snapshot is not completed")
            if reference.get("exact_sub_mute") is not True:
                raise ValueError(f"Main-only {side} exact-sub-mute confirmation is missing")
            normalized_by_db = float(reference.get("normalized_by_db"))
            if not math.isfinite(normalized_by_db):
                raise ValueError(f"Main-only {side} normalization metadata is invalid")
            if int(reference.get("crossover_frequency_hz")) != int(crossover_hz):
                raise ValueError(f"Main-only {side} crossover does not match the AutoSub job")
            if bool(reference.get("main_highpass_enabled")) != bool(main_highpass_enabled):
                raise ValueError(f"Main-only {side} Main-HP state does not match the AutoSub job")
            sample_rate = float(reference.get("sample_rate"))
            if not math.isfinite(sample_rate) or sample_rate <= 2.0 * float(crossover_hz):
                raise ValueError(f"Main-only {side} sample-rate metadata is invalid")
            sample_rates[side] = sample_rate
            if not isinstance(points, list) or len(points) < 2:
                raise ValueError(f"Main-only {side} calibrated points unavailable")
            parsed = [[float(point[0]), float(point[1])] for point in points]
            if any(not math.isfinite(point[0]) or point[0] <= 0 or not math.isfinite(point[1]) for point in parsed):
                raise ValueError(f"Main-only {side} contains invalid points")
            if any(parsed[index][0] >= parsed[index + 1][0] for index in range(len(parsed) - 1)):
                raise ValueError(f"Main-only {side} frequencies are not strictly increasing")
            side_points[side] = parsed
    except (TypeError, ValueError, IndexError) as exc:
        diagnostics["reason"] = str(exc)
        return diagnostics

    common_low = max(target_min, side_points["left"][0][0], side_points["right"][0][0])
    common_high = min(target_max, side_points["left"][-1][0], side_points["right"][-1][0])
    if sample_rates["left"] != sample_rates["right"]:
        diagnostics["reason"] = "Main-only L/R sample rates do not match"
        return diagnostics
    usable_low = max(common_low, LEVEL_REFERENCE_MIN_HZ)
    usable_high = min(common_high, LEVEL_REFERENCE_MAX_HZ)
    diagnostics["common_support_hz"] = [round(common_low, 6), round(common_high, 6)]
    diagnostics["usable_band_hz"] = [round(usable_low, 6), round(usable_high, 6)]
    if usable_high <= usable_low:
        diagnostics["reason"] = "No common Target/Main support remains in the broadband level-reference band"
        return diagnostics
    span_octaves = math.log2(usable_high / usable_low)
    diagnostics["usable_span_octaves"] = round(span_octaves, 6)
    failures: list[str] = []
    for side in ("left", "right"):
        usable_main = [point for point in side_points[side] if usable_low <= point[0] <= usable_high]
        target_on_main = _auto_sub_log_interpolate_points(target_points, [point[0] for point in usable_main])
        aligned = [
            [main_point[0], main_point[1], target_point[1]]
            for main_point, target_point in zip(usable_main, target_on_main)
        ]
        side_ok = len(aligned) >= 8 and span_octaves >= 1.0
        diagnostics["sides"][side] = {
            "status": "ready" if side_ok else "insufficient_support",
            "point_count": len(aligned),
            "frequency_support_hz": [aligned[0][0], aligned[-1][0]] if aligned else None,
            "aligned_points": aligned,
            "point_format": ["frequency_hz", "calibrated_main_db", "target_db"],
            "source": {
                "sweep_id": main_references[side].get("sweep_id"),
                "measurement_channel": main_references[side].get("measurement_channel"),
                "sample_rate": main_references[side].get("sample_rate"),
                "normalized_by_db": main_references[side].get("normalized_by_db"),
                "exact_sub_mute": True,
            },
        }
        if len(aligned) < 8:
            failures.append(f"{side} has {len(aligned)} usable points; 8 required")
    if span_octaves < 1.0:
        failures.append(f"usable span is {span_octaves:.3f} octaves; 1.0 required")
    if failures:
        diagnostics["reason"] = "; ".join(failures)
        return diagnostics
    anchor_offset_points = [
        [point[0], point[1] - point[2]]
        for side in ("left", "right")
        for point in diagnostics["sides"][side]["aligned_points"]
    ]
    target_vertical_offset_db = measurement_level_reference_db(anchor_offset_points)
    if target_vertical_offset_db is None:
        diagnostics["reason"] = "Broadband Main/Target level reference is unavailable"
        return diagnostics
    diagnostics["target_vertical_offset_db"] = round(target_vertical_offset_db, 6)
    diagnostics["target_anchor_statistic"] = "normal measurement 120-8000 Hz median(calibrated_main_db - relative_target_db), pooled L/R"
    diagnostics["status"] = "ready"
    diagnostics["reason"] = "Anchor inputs passed"
    diagnostics["target"] = {
        "key": target_curve.get("key"),
        "label": target_curve.get("label"),
        "provenance": target_curve.get("provenance"),
    }
    return json.loads(json.dumps(diagnostics))

def _auto_sub_one_octave_smooth(points: list[list[float]]) -> list[list[float]]:
    """Robust fixed 1/1-octave smoothing using a moving median in log frequency."""
    parsed = [[float(point[0]), float(point[1])] for point in points]
    if len(parsed) < 3 or any(
        not math.isfinite(frequency) or frequency <= 0 or not math.isfinite(db)
        for frequency, db in parsed
    ):
        raise ValueError("Winner curve requires at least three finite points")
    if any(parsed[index][0] >= parsed[index + 1][0] for index in range(len(parsed) - 1)):
        raise ValueError("Winner frequencies must be strictly increasing")
    half_octave = math.sqrt(2.0)
    return [
        [frequency, round(statistics.median(
            value for neighbour_frequency, value in parsed
            if frequency / half_octave <= neighbour_frequency <= frequency * half_octave
        ), 6)]
        for frequency, _value in parsed
    ]

def _auto_sub_third_octave_smooth(points: list[list[float]]) -> list[list[float]]:
    """Robust fixed 1/3-octave smoothing for Stereo corridor checks only."""
    parsed = [[float(point[0]), float(point[1])] for point in points]
    if len(parsed) < 3 or any(
        not math.isfinite(frequency) or frequency <= 0 or not math.isfinite(db)
        for frequency, db in parsed
    ):
        raise ValueError("Corridor curve requires at least three finite points")
    if any(parsed[index][0] >= parsed[index + 1][0] for index in range(len(parsed) - 1)):
        raise ValueError("Corridor frequencies must be strictly increasing")
    half_window = 2.0 ** (1.0 / 6.0)
    return [
        [frequency, round(statistics.median(
            value for neighbour_frequency, value in parsed
            if frequency / half_window <= neighbour_frequency <= frequency * half_window
        ), 6)]
        for frequency, _value in parsed
    ]

def _auto_sub_stereo_corridor_violation(
    *, points: list[list[float]], target_curve: dict[str, Any] | None,
    anchor: dict[str, Any] | None, crossover_hz: int, direction: float,
) -> dict[str, Any]:
    """Measure broad 1/3-octave Target -6/+9 dB violations in one Gain direction."""
    result: dict[str, Any] = {
        "available": False, "relevant": False, "smoothing_octaves": 1.0 / 3.0,
        "corridor_db": [-6.0, 9.0], "direction": "lower" if direction < 0 else "raise",
        "severity_db": 0.0, "reason": None,
    }
    try:
        if not isinstance(anchor, dict) or anchor.get("status") != "ready":
            raise ValueError("Main/Target anchor is unavailable")
        offset = float(anchor.get("target_vertical_offset_db"))
        target_points = (target_curve or {}).get("points")
        if not math.isfinite(offset) or not isinstance(target_points, list) or len(target_points) < 2:
            raise ValueError("Anchored Target is unavailable")
        low, high = max(20.0, 0.5 * float(crossover_hz)), 2.0 * float(crossover_hz)
        smoothed = [point for point in _auto_sub_third_octave_smooth(points) if low <= point[0] <= high]
        target = _auto_sub_log_interpolate_points(target_points, [point[0] for point in smoothed])
        if len(smoothed) < 8 or len(target) != len(smoothed):
            raise ValueError("Stereo corridor has fewer than 8 common points")
        target = _auto_sub_third_octave_smooth(target)
        rows = []
        for measured, target_point in zip(smoothed, target):
            delta = measured[1] - (target_point[1] + offset)
            excess = max(0.0, delta - 9.0) if direction < 0 else max(0.0, -6.0 - delta)
            rows.append((measured[0], excess))
        groups: list[list[tuple[float, float]]] = []
        previous_index = -2
        for index, row in enumerate(rows):
            if row[1] > 0.0:
                if index != previous_index + 1:
                    groups.append([])
                groups[-1].append(row)
                previous_index = index
        relevant_groups = [
            group for group in groups
            if len(group) >= 3 and math.log2(group[-1][0] / group[0][0]) >= (1.0 / 6.0)
        ]
        severity = max((statistics.median(value for _frequency, value in group) for group in relevant_groups), default=0.0)
        result.update({
            "available": True, "relevant": bool(relevant_groups), "severity_db": round(severity, 3),
            "point_count": len(smoothed), "frequency_range_hz": [round(smoothed[0][0], 3), round(smoothed[-1][0], 3)],
            "broad_group_count": len(relevant_groups),
            "reason": "Broad 1/3-octave corridor violation" if relevant_groups else "No broad 1/3-octave corridor violation",
        })
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        result["reason"] = str(exc)
    return result

def _auto_sub_stereo_probe_plan(
    *, correction_plan: dict[str, Any], gain_after: dict[str, Any],
    gain_deltas: dict[str, float], accepted_step1_sides: dict[str, bool],
    after_points: dict[str, list[list[float]]], target_curve: dict[str, Any] | None,
    anchor: dict[str, Any] | None, crossover_hz: int,
) -> dict[str, Any]:
    """Plan one bounded Stereo-only probe when only the >6 dB correction limit failed."""
    result: dict[str, Any] = {"available": False, "deltas_db": {}, "channels": {}, "reason": None}
    if correction_plan.get("reason") != "Measured final Gain correction is implausible":
        result["reason"] = "Stereo probe requires only the >6 dB correction limit to have failed"
        return result
    for side in ("left", "right"):
        channel = ((correction_plan.get("channels") or {}).get(side) or {})
        try:
            response = float(channel.get("response_change_per_db"))
            remaining = float(((gain_after.get("channels") or {}).get(side) or {}).get("target_delta_db"))
            first_step = float(gain_deltas.get(side, 0.0))
            raw_correction = remaining / response
            direction_clear = (
                accepted_step1_sides.get(side) is True
                and 0.2 <= response <= 2.0
                and abs(raw_correction) > 6.0
                and raw_correction * first_step > 0.0
                and remaining * first_step > 0.0
            )
            corridor = _auto_sub_stereo_corridor_violation(
                points=after_points.get(side) or [], target_curve=target_curve, anchor=anchor,
                crossover_hz=crossover_hz, direction=raw_correction,
            )
            eligible = bool(direction_clear and corridor.get("available") and corridor.get("relevant"))
            result["channels"][side] = {
                "eligible": eligible, "response_change_per_db": round(response, 4),
                "remaining_error_db": round(remaining, 3), "raw_correction_db": round(raw_correction, 3),
                "direction_clear": direction_clear, "corridor_before": corridor,
            }
            if eligible:
                result["deltas_db"][side] = -1.0 if raw_correction < 0.0 else 1.0
        except (TypeError, ValueError, ZeroDivisionError):
            result["channels"][side] = {"eligible": False, "reason": "Incomplete Stereo probe inputs"}
    result["available"] = bool(result["deltas_db"])
    result["reason"] = "Bounded Stereo corridor probe planned" if result["available"] else "No Stereo side qualified for a bounded corridor probe"
    return result

def _auto_sub_target_residual_raw_db(
    points: list[list[float]] | None, target_curve: dict[str, Any] | None,
    anchor: dict[str, Any] | None, crossover_hz: int,
) -> tuple[float, float, list[list[float]]]:
    """Unbounded anchored-Target residual (median Target-minus-curve) of one curve.

    Shared definition for the Gain diagnostics and the balance-trim
    configuration transfer. Returns ``(raw_residual_db, mad_db, usable_points)``
    with ``usable_points`` the 1/1-octave-smoothed curve restricted to the
    decision band. Raises ValueError when curve/Target/anchor support is
    insufficient.
    """
    if not isinstance(anchor, dict) or anchor.get("status") != "ready":
        raise ValueError("Main/Target anchor is unavailable")
    vertical_offset = float(anchor.get("target_vertical_offset_db"))
    if not math.isfinite(vertical_offset):
        raise ValueError("Target vertical offset is invalid")
    target_points = (target_curve or {}).get("points")
    if not isinstance(target_points, list) or len(target_points) < 2:
        raise ValueError("Target snapshot is unavailable")
    requested_low = max(20.0, float(crossover_hz) * 0.5)
    requested_high = float(crossover_hz) * 2.0
    smoothed = _auto_sub_one_octave_smooth(points)
    usable = [point for point in smoothed if requested_low <= point[0] <= requested_high]
    target_on_winner = _auto_sub_log_interpolate_points(target_points, [point[0] for point in usable])
    if len(target_on_winner) != len(usable) or len(usable) < 8:
        raise ValueError("curve/Target support has fewer than 8 common points")
    smoothed_target = _auto_sub_one_octave_smooth(target_on_winner)
    deviations = [
        (target_point[1] + vertical_offset) - curve_point[1]
        for curve_point, target_point in zip(usable, smoothed_target)
    ]
    target_delta = statistics.median(deviations)
    mad = statistics.median(abs(value - target_delta) for value in deviations)
    return target_delta, mad, usable


def _calculate_auto_sub_gain(
    *, mode: str, target_curve: dict[str, Any] | None, anchor: dict[str, Any] | None,
    winner_curves: dict[str, list[list[float]]], crossover_hz: int,
) -> dict[str, Any]:
    """Calculate bounded diagnostic Gain from calibrated accepted-winner curves."""
    result: dict[str, Any] = {
        "available": False, "gain_calculated": False, "applied": False,
        "method": "fixed 1/1-octave moving-median smoothing of Winner and anchored Target; median Target-minus-Winner deviation",
        "smoothing_octaves": 1.0,
        "bounds_db": [-6.0, 6.0], "reason": None, "channels": {},
    }
    try:
        for channel, points in winner_curves.items():
            raw_gain, mad, usable = _auto_sub_target_residual_raw_db(points, target_curve, anchor, crossover_hz)
            bounded = min(6.0, max(-6.0, raw_gain))
            coverage_octaves = math.log2(usable[-1][0] / usable[0][0])
            confidence = "high" if len(usable) >= 24 and coverage_octaves >= 1.5 and mad <= 1.5 else (
                "medium" if len(usable) >= 12 and coverage_octaves >= 1.0 and mad <= 3.0 else "low"
            )
            result["channels"][channel] = {
                "frequency_range_hz": [round(usable[0][0], 3), round(usable[-1][0], 3)],
                "point_count": len(usable), "coverage_octaves": round(coverage_octaves, 3),
                "target_delta_db": round(raw_gain, 3),
                "raw_recommendation_db": round(raw_gain, 3), "recommendation_db": round(bounded, 3),
                "clamped": bounded != raw_gain, "median_absolute_deviation_db": round(mad, 3),
                "confidence": confidence,
                "chain_anchor_db": (
                    round(anchor_value, 3) if (anchor_value := auto_sub_chain_anchor_db(points)) is not None else None
                ),
                "reason": f"{len(usable)} points across {coverage_octaves:.2f} octaves; MAD {mad:.2f} dB",
            }
        if mode in (OUTPUT_MODE_SUBWOOFER_21, OUTPUT_MODE_SUBWOOFER_22):
            channel_values = [entry["raw_recommendation_db"] for entry in result["channels"].values()]
            if not channel_values:
                raise ValueError("No accepted Winner channels are available")
            raw_common = statistics.median(channel_values)
            bounded_common = min(6.0, max(-6.0, raw_common))
            result["recommendation"] = {
                "type": "common", "raw_delta_db": round(raw_common, 3),
                "delta_db": round(bounded_common, 3), "clamped": bounded_common != raw_common,
                "preserves_relative_sub_gain": mode == OUTPUT_MODE_SUBWOOFER_22,
            }
        else:
            result["recommendation"] = {
                "type": "per_channel",
                "left_delta_db": result["channels"]["left"]["recommendation_db"],
                "right_delta_db": result["channels"]["right"]["recommendation_db"],
            }
        confidences = [entry["confidence"] for entry in result["channels"].values()]
        result["confidence"] = "low" if "low" in confidences else ("medium" if "medium" in confidences else "high")
        result["available"] = True
        result["gain_calculated"] = True
        result["reason"] = "Diagnostic recommendation calculated; no audio state changed"
    except (TypeError, ValueError, IndexError, KeyError) as exc:
        result["reason"] = str(exc)
    return json.loads(json.dumps(result))

def _auto_sub_gain_deltas(
    auto_gain: dict[str, Any], mode: str, *, max_abs_db: float = 6.0,
) -> dict[str, float]:
    """Use the residual only as direction/magnitude input for one bounded feedback step."""
    if not isinstance(auto_gain, dict) or not auto_gain.get("gain_calculated"):
        return {}
    limit = abs(float(max_abs_db))
    if not math.isfinite(limit) or limit <= 0:
        return {}
    bounded = lambda value: min(limit, max(-limit, float(value)))
    recommendation = auto_gain.get("recommendation") or {}
    if mode == OUTPUT_MODE_SUBWOOFER_22_STEREO:
        return {
            "left": bounded(recommendation["left_delta_db"]),
            "right": bounded(recommendation["right_delta_db"]),
        }
    delta = bounded(recommendation["delta_db"])
    return {"left": delta, "right": delta}


# A side counts as alignment-changed when the accepted delay differs from the
# delay the balance trim was measured for by more than this tolerance (same
# tolerance the scan-edge and neighbour checks use).
_AUTO_SUB_ALIGNMENT_CHANGE_TOLERANCE_MS: float = 0.05


def _auto_sub_chain_health_check(
    job: dict[str, Any],
    alignment_samples: float | int | None,
    sample_rate: float | int | None,
) -> dict[str, Any] | None:
    """Detect a persistent reference-vs-mic arrival displacement mid-run.

    The analyzer locks each capture's arrival independently, so healthy
    sweeps jitter only by capture-quantum steps (1024 samples at 48 kHz)
    around a constant baseline. A constant displacement far beyond that —
    25600 samples in the 799f3bd5d1ab run, left by the output device's
    resync pre-filling its buffer — corrupts every subsequent capture until
    the audio stack is reset, so the run must abort instead of grinding
    through disturbed sweeps. Returns the evidence dict when degraded, else
    None.
    """
    if not isinstance(alignment_samples, (int, float)) or not math.isfinite(float(alignment_samples)):
        return None
    history = job.setdefault("chain_alignment_samples", [])
    history.append(float(alignment_samples))
    if len(history) < 3:
        return None
    window = history[-8:]
    baseline = statistics.median(window)
    shift = float(alignment_samples) - baseline
    bound = max(4096.0, float(sample_rate or 48000) / 12.0)
    if abs(shift) <= bound:
        return None
    return {
        "arrival_shift_samples": round(shift, 1),
        "arrival_bound_samples": round(bound, 1),
        "baseline_samples": round(baseline, 1),
        "current_samples": round(float(alignment_samples), 1),
    }


def _auto_sub_balance_transfer_deltas(
    *,
    balance_deltas_db: dict[str, float],
    winner_residuals_db: dict[str, float | None],
    incumbent_residuals_db: dict[str, float | None],
    alignment_changed: dict[str, bool],
    max_abs_db: float = 6.0,
) -> dict[str, Any]:
    """Final sub trim per channel after the alignment decision.

    The balance stage measures the anchored-Target residual of the incumbent
    configuration at the original sub level and applies it as a trim. That
    residual describes the incumbent configuration only: after the
    alignment changed, the post-alignment residual measured at the balanced
    level still contains the part of the balance trim the first application
    did not realise (band-median response is below 1 dB per dB of sub
    trim). Adding that full residual on top of the old trim re-closes the
    same room excess a second time and produced the documented over-damping.

    Transfer rule per channel whose alignment changed::

        applied delta = winner residual - incumbent residual
        implied total = balance trim + (winner residual - incumbent residual)

    where both residuals are measured at the same balanced level. The implied
    total is the single-stage residual the end configuration would have
    received; the returned ``deltas_db`` is the increment to apply on top of
    the still-applied balance trim. Channels whose alignment did not change
    keep the existing fine-trim behaviour (the residual of the same
    configuration is applied on top). When the incumbent residual is
    unavailable for a changed channel, the balance trim stands alone
    (increment 0) instead of guessing or accumulating.
    """
    sides = ("left", "right")
    result: dict[str, Any] = {
        "available": False, "deltas_db": {}, "channels": {}, "reason": None,
        "alignment_changed": {side: bool(alignment_changed.get(side)) for side in sides},
    }
    try:
        limit = abs(float(max_abs_db))
        if not math.isfinite(limit) or limit <= 0:
            raise ValueError("Transfer bound is invalid")
        any_changed = any(result["alignment_changed"].values())
        if not any_changed:
            result["reason"] = "Accepted alignment matches the balance configuration; transfer not applicable"
            return result
        for side in sides:
            balance_delta = float(balance_deltas_db.get(side, 0.0) or 0.0)
            winner_residual = winner_residuals_db.get(side)
            incumbent_residual = incumbent_residuals_db.get(side)
            if not result["alignment_changed"][side]:
                if winner_residual is None:
                    raise ValueError(f"{side} winner residual is unavailable")
                increment = float(winner_residual)
                mode = "fine_trim_same_configuration"
            elif winner_residual is None:
                raise ValueError(f"{side} winner residual is unavailable")
            elif incumbent_residual is None:
                # Without the incumbent's balanced-level residual the
                # level-consistent delta is unknown; keep the measured
                # balance trim instead of guessing or accumulating.
                increment = 0.0
                mode = "fallback_balance_only"
            else:
                increment = float(winner_residual) - float(incumbent_residual)
                mode = "configuration_transfer"
            bounded = min(limit, max(-limit, increment))
            result["deltas_db"][side] = bounded
            result["channels"][side] = {
                "balance_delta_db": round(balance_delta, 3),
                "winner_residual_db": round(float(winner_residual), 3) if winner_residual is not None else None,
                "incumbent_residual_db": round(float(incumbent_residual), 3) if incumbent_residual is not None else None,
                "delta_db": round(bounded, 3),
                "implied_total_db": round(balance_delta + bounded, 3),
                "mode": mode if result["alignment_changed"][side] else "fine_trim_same_configuration",
                "clamped": bounded != increment,
            }
        result["available"] = True
        result["reason"] = "Balance trim transferred to the accepted configuration"
    except (TypeError, ValueError, KeyError) as exc:
        result["reason"] = str(exc)
    return result

# Verdict tolerance for the Target-residual comparison: must exceed the
# residual metric's run-to-run spread (~0.5 dB) while staying far below the
# multi-dB shift a harmful Gain step would produce.
_AUTO_SUB_GAIN_VERDICT_TOLERANCE_DB: float = 1.0

# Confirmation-gate tolerance for the deepest local dip (curve minus
# 1/1-octave moving-median surround, 0.5*fc..2*fc). Derived from real runs:
# same-state repeats move the metric by <=0.47 dB, a balance-only level
# change by <=0.69 dB, the accepted real outcomes stayed <=+0.7 dB versus
# Before, while the rejected real 2.2-stereo run showed +3.17 dB at the new
# 74 Hz notch. 2.5 dB sits ~5x above the noise floor and clearly below the
# observed failure signature.
_AUTO_SUB_LOCAL_DIP_TOLERANCE_DB: float = 2.5

def _auto_sub_local_dip_db(points: list, low_hz: float, high_hz: float) -> float | None:
    """Depth of the deepest local dip versus the 1/1-octave median surround.

    Unlike mean-based shape metrics this is immune to broadband level or
    balance changes: a sub level trim moves the local median along, while a
    new interference notch keeps its depth. Returns positive dB, or None
    when the band support is too small.
    """
    if not isinstance(points, list) or len(points) < 8:
        return None
    try:
        smoothed = _auto_sub_one_octave_smooth(points)
    except (ValueError, TypeError):
        return None
    usable = [p for p in smoothed if low_hz <= float(p[0]) <= high_hz]
    if len(usable) < 8:
        return None
    curve = _auto_sub_log_interpolate_points(points, [float(p[0]) for p in usable])
    deviations = [float(c[1]) - float(s[1]) for c, s in zip(curve, usable)]
    return round(-min(deviations), 2)

def _auto_sub_local_dip_gate_sides(
    before_dips: dict[str, float | None], final_dips: dict[str, float | None], tolerance_db: float,
) -> list[str]:
    """Sides whose final local dip exceeds the Before state by the tolerance."""
    return [
        side for side in ("left", "right")
        if final_dips.get(side) is not None and before_dips.get(side) is not None
        and final_dips[side] > before_dips[side] + tolerance_db
    ]

def _auto_sub_local_dip_recheck_decision(
    before_dips: dict[str, float | None],
    final_dips: dict[str, float | None],
    recheck_dips: dict[str, float | None],
    tolerance_db: float,
) -> dict[str, Any]:
    """Confirm a triggered regression against the freshly measured incumbent."""
    failed_sides = _auto_sub_local_dip_gate_sides(before_dips, final_dips, tolerance_db)
    evidence_available = all(recheck_dips.get(side) is not None for side in failed_sides)
    confirmed_failed_sides = [
        side for side in failed_sides
        if recheck_dips.get(side) is not None
        and final_dips[side] > recheck_dips[side] + tolerance_db
    ]
    incumbent_evidence_available = all(
        before_dips.get(side) is not None and recheck_dips.get(side) is not None
        for side in ("left", "right")
    )
    incumbent_passed = incumbent_evidence_available and all(
        recheck_dips[side] <= before_dips[side] + tolerance_db
        for side in ("left", "right")
    )
    if not evidence_available:
        outcome = "original_restored"
    elif not confirmed_failed_sides:
        outcome = "final_kept"
    elif incumbent_passed:
        outcome = "incumbent_kept"
    else:
        outcome = "original_restored"
    return {
        "failed_sides": failed_sides,
        "confirmed_failed_sides": confirmed_failed_sides,
        "evidence_available": evidence_available,
        "incumbent_evidence_available": incumbent_evidence_available,
        "incumbent_passed": incumbent_passed,
        "outcome": outcome,
    }

def _auto_sub_gain_verdict(before: dict[str, Any], after: dict[str, Any], mode: str) -> dict[str, Any]:
    """Accept one Gain attempt unless its residual Target error grows notably.

    Both diagnostics carry the main-only 200-600 Hz chain anchor of their
    input curves. A chain gain excursion between the two measurements shifts
    the measured residual without any real response change, so the after
    residual is anchor-corrected before the comparison whenever both anchors
    are available.

    The acceptance tolerance must exceed the metric's run-to-run spread
    (~0.5 dB from room/chain variation); a genuinely harmful bounded Gain
    step moves the residual by several dB. The former 0.25 dB threshold sat
    below the noise floor and produced noise-driven rejections.
    """
    tolerance_db = _AUTO_SUB_GAIN_VERDICT_TOLERANCE_DB
    verdict: dict[str, Any] = {"accepted": False, "reason": None, "channels": {}, "tolerance_db": tolerance_db}
    if not before.get("gain_calculated") or not after.get("gain_calculated"):
        verdict["reason"] = "Gain verification inputs unavailable"
        return verdict
    names = ("left", "right")
    accepted = True
    for name in names:
        before_channel = before["channels"][name]
        after_channel = after["channels"][name]
        before_error = abs(float(before_channel["raw_recommendation_db"]))
        after_signed = float(after_channel["raw_recommendation_db"])
        after_error = abs(after_signed)
        anchor_adjustment_db = None
        before_anchor = before_channel.get("chain_anchor_db")
        after_anchor = after_channel.get("chain_anchor_db")
        if before_anchor is not None and after_anchor is not None:
            anchor_adjustment_db = round(float(after_anchor) - float(before_anchor), 3)
            after_error = abs(after_signed + anchor_adjustment_db)
        channel_ok = after_error <= before_error + tolerance_db
        accepted = accepted and channel_ok
        verdict["channels"][name] = {
            "before_absolute_residual_db": round(before_error, 3),
            "after_absolute_residual_db": round(abs(after_signed), 3),
            "adjusted_after_absolute_residual_db": round(after_error, 3),
            "anchor_adjustment_db": anchor_adjustment_db,
            "accepted": channel_ok,
        }
    verdict["accepted"] = accepted
    verdict["reason"] = "Gain verification passed" if accepted else "After residual exceeded pre-Gain residual by more than 1.0 dB"
    return verdict

def _auto_sub_gain_response_correction(
    before: dict[str, Any], after: dict[str, Any], applied_step: dict[str, float], mode: str,
) -> dict[str, Any]:
    """Estimate one final correction from the measured broad-band response per applied dB."""
    result: dict[str, Any] = {
        "available": False, "deltas_db": {}, "raw_deltas_db": {}, "applied_deltas_db": {},
        "channels": {}, "reason": None,
    }
    if not before.get("gain_calculated") or not after.get("gain_calculated"):
        result["reason"] = "Gain response inputs unavailable"
        return result
    try:
        for side in ("left", "right"):
            step = float(applied_step[side])
            if abs(step) < 0.05:
                raise ValueError(f"{side} first Gain step is too small to measure sensitivity")
            before_channel = before["channels"][side]
            after_channel = after["channels"][side]
            before_delta = float(before_channel["target_delta_db"])
            after_delta = float(after_channel["target_delta_db"])
            anchor_adjusted = False
            before_anchor = before_channel.get("chain_anchor_db")
            after_anchor = after_channel.get("chain_anchor_db")
            if before_anchor is not None and after_anchor is not None:
                # Remove a chain gain excursion between the two measurements
                # so the sensitivity reflects the response, not the capture.
                after_delta = after_delta + (float(after_anchor) - float(before_anchor))
                anchor_adjusted = True
            response_change = before_delta - after_delta
            sensitivity = response_change / step
            plausible = math.isfinite(sensitivity) and 0.2 <= sensitivity <= 2.0
            result["channels"][side] = {
                "before_target_delta_db": round(before_delta, 3),
                "after_target_delta_db": round(float(after_channel["target_delta_db"]), 3),
                "anchor_adjusted_after_target_delta_db": round(after_delta, 3) if anchor_adjusted else None,
                "anchor_adjusted": anchor_adjusted,
                "applied_step_db": round(step, 3),
                "response_change_db": round(response_change, 3),
                "response_change_per_db": round(sensitivity, 4),
                "plausible": plausible,
            }
            if not plausible:
                raise ValueError(f"{side} measured response per dB is implausible ({sensitivity:.3f})")
        if mode in (OUTPUT_MODE_SUBWOOFER_21, OUTPUT_MODE_SUBWOOFER_22):
            sensitivity = statistics.median(
                result["channels"][side]["response_change_per_db"] for side in ("left", "right")
            )
            remaining = statistics.median(
                float(after["channels"][side]["target_delta_db"]) for side in ("left", "right")
            )
            correction = remaining / sensitivity
            deltas = {"left": correction, "right": correction}
        else:
            deltas = {
                side: float(after["channels"][side]["target_delta_db"])
                / float(result["channels"][side]["response_change_per_db"])
                for side in ("left", "right")
            }
        result["raw_deltas_db"] = {side: round(value, 3) for side, value in deltas.items()}
        if any(not math.isfinite(value) or abs(value) > 6.0 for value in deltas.values()):
            raise ValueError("Measured final Gain correction is implausible")
        applied_deltas = {
            side: max(-6.0 - float(applied_step[side]), min(6.0 - float(applied_step[side]), value))
            for side, value in deltas.items()
        }
        result["applied_deltas_db"] = {side: round(value, 3) for side, value in applied_deltas.items()}
        result["deltas_db"] = dict(result["applied_deltas_db"])
        result["available"] = True
        result["reason"] = "Final correction derived from measured Before/After response"
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        result["reason"] = str(exc)
    return result

def _auto_sub_gain_log_score(diagnostics: dict[str, Any] | None) -> float | None:
    channels = (diagnostics or {}).get("channels") or {}
    raw_values = [(channels.get(side) or {}).get("target_delta_db") for side in ("left", "right")]
    if any(value is None for value in raw_values):
        return None
    values = [abs(float(value)) for value in raw_values]
    return round(statistics.median(values), 3) if len(values) == 2 and all(math.isfinite(v) for v in values) else None

def _auto_sub_gain_log_line(event: str, payload: dict[str, Any]) -> None:
    logger.info("%s %s", event, json.dumps(payload, sort_keys=True, separators=(",", ":")))

def _auto_sub_22_snapshot_with_gain(
    snapshot: dict[str, Any], *, left_delta_db: float, right_delta_db: float,
) -> dict[str, Any]:
    updated = _auto_sub_snapshot_copy(snapshot)
    subs = updated.setdefault("subwoofers", {})
    for key, delta in (("sub1", left_delta_db), ("sub2", right_delta_db)):
        sub = subs.setdefault(key, {})
        sub["level_db"] = max(-80.0, min(12.0, float(sub.get("level_db", 0.0) or 0.0) + float(delta)))
    return updated

async def _capture_auto_sub_main_references(
    *,
    job: dict[str, Any],
    fc: int,
    input_id: str,
    mic_input_channel: str,
    reference_input_channel: str,
    calibration_ref: str,
    calibration_filename: str | None,
    calibration_bytes: bytes | None,
    auto_sub_rate: int,
    output_mode: str,
    original_config_snapshot: dict[str, Any],
) -> None:
    """Capture exactly one L and one R Main-only reference before candidate scans."""
    subwoofer = original_config_snapshot.get("subwoofer") if isinstance(original_config_snapshot.get("subwoofer"), dict) else {}
    sub1 = _auto_sub_22_sub(original_config_snapshot, "sub1")
    sub2 = _auto_sub_22_sub(original_config_snapshot, "sub2")
    is_22 = output_mode in OUTPUT_MODE_SUBWOOFER_22_MODES
    main_highpass_enabled = bool(
        _auto_sub_22_global_config(original_config_snapshot).get("main_highpass_enabled", True)
        if is_22 else subwoofer.get("main_highpass_enabled", True)
    )
    left_delay = float(sub1.get("alignment_ms", 0.0) or 0.0) if is_22 else float(subwoofer.get("sub_alignment_ms", 0.0) or 0.0)
    right_delay = float(sub2.get("alignment_ms", 0.0) or 0.0) if is_22 else left_delay
    job["stage"] = "main_reference"
    job["main_references"] = {
        "status": "running",
        "exact_sub_mute": True,
        "crossover_frequency_hz": int(fc),
        "main_highpass_enabled": main_highpass_enabled,
        "left": {"status": "pending"},
        "right": {"status": "pending"},
    }
    main_reference_sweep_profile = default_measurement_sweep_profile()
    results: dict[str, dict[str, Any]] = {}
    for index, side in enumerate(("left", "right"), start=1):
        if _auto_sub_cancel_requested(job):
            job["main_references"][side] = {"status": "cancelled"}
            break
        delay = left_delay if side == "left" else right_delay
        result = await _measure_auto_sub_candidate(
            delay_ms=delay,
            job=job,
            candidate_index=index,
            total=2,
            stage="main_reference",
            fc=fc,
            input_id=input_id,
            channel=side,
            mic_input_channel=mic_input_channel,
            reference_input_channel=reference_input_channel,
            calibration_ref=calibration_ref,
            calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes,
            auto_sub_sweep_profile=main_reference_sweep_profile,
            auto_sub_rate=auto_sub_rate,
            original_level=float(subwoofer.get("sub_level_db", 0.0) or 0.0),
            original_polarity=str(subwoofer.get("sub_polarity") or "normal"),
            original_highpass=main_highpass_enabled,
            measurement_label=f"Main-only reference: {side.title()} ({index}/2)",
            measure_channel=side,
            output_mode=output_mode,
            original_config_snapshot=original_config_snapshot,
            sub1_alignment_ms=left_delay,
            sub2_alignment_ms=right_delay,
            active_subs=("sub1", "sub2"),
            exact_sub_mute=True,
        )
        results[side] = result
        job["main_references"][side] = {
            "status": result.get("status"),
            "points": json.loads(json.dumps(result.get("calibrated_points"))) if result.get("calibrated_points") else [],
            "normalized_by_db": result.get("normalized_by_db"),
            "sweep_id": result.get("sweep_id"),
            "channel": side,
            "measurement_channel": result.get("measurement_channel"),
            "sample_rate": result.get("sample_rate"),
            "crossover_frequency_hz": int(fc),
            "main_highpass_enabled": main_highpass_enabled,
            "exact_sub_mute": bool(result.get("exact_sub_mute")),
        }
        if result.get("error"):
            job["main_references"][side]["error"] = str(result.get("error"))
    complete = len(results) == 2 and all(
        result.get("status") == "completed" and len(result.get("calibrated_points") or []) >= 2
        for result in results.values()
    )
    job["main_references"]["status"] = "completed" if complete else "unavailable"
    if not complete:
        failures = [f"{side}: {result.get('status')} ({result.get('error') or 'no calibrated points'})" for side, result in results.items() if result.get("status") != "completed" or not result.get("calibrated_points")]
        if len(results) < 2:
            failures.append("reference capture cancelled before both sides completed")
        job["auto_gain"] = {"available": False, "reason": "Main-only reference unavailable: " + "; ".join(failures)}
    job["main_target_anchor"] = _analyze_auto_sub_main_target_anchor(
        target_curve=job.get("target_curve"),
        main_references=job.get("main_references"),
        crossover_hz=fc,
        main_highpass_enabled=main_highpass_enabled,
    )
    if job["main_target_anchor"].get("status") == "ready":
        job["auto_gain"] = {
            "available": False,
            "reason": "Main/Target anchor ready; Gain calculation is not implemented",
        }
    elif complete:
        job["auto_gain"] = {
            "available": False,
            "reason": "Main/Target anchor unavailable: " + str(job["main_target_anchor"].get("reason") or "unknown reason"),
        }

async def _measure_auto_sub_combined_candidate(
    *,
    delay_ms: float,
    job: dict[str, Any],
    candidate_index: int,
    total: int,
    sweep_index_start: int,
    sweep_total: int,
    stage: str,
    fc: int,
    input_id: str,
    mic_input_channel: str,
    reference_input_channel: str,
    calibration_ref: str,
    calibration_filename: str | None,
    calibration_bytes: bytes | None,
    auto_sub_sweep_profile: dict[str, Any],
    auto_sub_rate: int,
    original_level: float,
    original_polarity: str,
    original_highpass: bool,
    output_mode: str = OUTPUT_MODE_SUBWOOFER_21,
    original_config_snapshot: dict[str, Any] | None = None,
    sub1_alignment_ms: float | None = None,
    sub2_alignment_ms: float | None = None,
    active_subs: tuple[str, ...] = ("sub1",),
    sub1_polarity: str | None = None,
    sub2_polarity: str | None = None,
) -> dict[str, Any]:
    """Measure both L and R for one AutoSub delay candidate."""
    _combined_start = time.monotonic()

    def _last_sweep_timing(channel_name: str) -> dict[str, Any] | None:
        for timing in reversed(job.get("_sweep_timings", [])):
            if (
                timing.get("channel") == channel_name
                and timing.get("stage") == stage
                and round(float(timing.get("delay_ms", -9999)), 2) == round(float(delay_ms), 2)
            ):
                return timing
        return None

    def _append_combined_timing(status: str, left_result: dict[str, Any] | None = None, right_result: dict[str, Any] | None = None) -> None:
        left_timing = _last_sweep_timing("left")
        right_timing = _last_sweep_timing("right")
        job.setdefault("_combined_candidate_timings", []).append({
            "delay_ms": delay_ms,
            "stage": stage,
            "candidate": candidate_index,
            "status": status,
            "left_status": (left_result or {}).get("status"),
            "right_status": (right_result or {}).get("status"),
            "left_total_ms": ((left_timing or {}).get("durations", {}) or {}).get("total_ms"),
            "right_total_ms": ((right_timing or {}).get("durations", {}) or {}).get("total_ms"),
            "total_ms": round((time.monotonic() - _combined_start) * 1000, 1),
        })

    if _auto_sub_cancel_requested(job):
        _append_combined_timing("cancelled")
        return _auto_sub_cancelled_candidate(delay_ms, stage)

    if stage == "sub1_coarse":
        label = "Optimizing Sub 1"
    elif stage == "sub2_coarse":
        label = "Optimizing Sub 2"
    elif stage == "combined_matrix":
        label = "Combined Matrix"
    else:
        label = "Fine-Scan" if stage == "fine" else "Coarse scan"
    pair_suffix = ""
    if output_mode in OUTPUT_MODE_SUBWOOFER_22_MODES:
        s1 = _auto_sub_clamped_delay(sub1_alignment_ms if sub1_alignment_ms is not None else delay_ms)
        s2 = _auto_sub_clamped_delay(sub2_alignment_ms if sub2_alignment_ms is not None else 0.0)
        pair_suffix = f" (S1 {s1:.2f} ms / S2 {s2:.2f} ms)"
    left_result = await _measure_auto_sub_candidate(
        delay_ms=delay_ms,
        job=job,
        candidate_index=sweep_index_start,
        total=sweep_total,
        stage=stage,
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
        original_level=original_level,
        original_polarity=original_polarity,
        original_highpass=original_highpass,
        measurement_label=f"{label}: L meas {candidate_index}/{total} @ {delay_ms:.2f} ms{pair_suffix}",
        candidate_current=candidate_index,
        candidate_total=total,
        measure_channel="left",
        output_mode=output_mode,
        original_config_snapshot=original_config_snapshot,
        sub1_alignment_ms=sub1_alignment_ms,
        sub2_alignment_ms=sub2_alignment_ms,
        active_subs=active_subs,
        sub1_polarity=sub1_polarity,
        sub2_polarity=sub2_polarity,
    )
    if _auto_sub_cancel_requested(job):
        _append_combined_timing("cancelled", left_result=left_result)
        return _auto_sub_cancelled_candidate(delay_ms, stage)

    right_result = await _measure_auto_sub_candidate(
        delay_ms=delay_ms,
        job=job,
        candidate_index=sweep_index_start + 1,
        total=sweep_total,
        stage=stage,
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
        original_level=original_level,
        original_polarity=original_polarity,
        original_highpass=original_highpass,
        measurement_label=f"{label}: R meas {candidate_index}/{total} @ {delay_ms:.2f} ms{pair_suffix}",
        candidate_current=candidate_index,
        candidate_total=total,
        measure_channel="right",
        output_mode=output_mode,
        original_config_snapshot=original_config_snapshot,
        sub1_alignment_ms=sub1_alignment_ms,
        sub2_alignment_ms=sub2_alignment_ms,
        active_subs=active_subs,
        sub1_polarity=sub1_polarity,
        sub2_polarity=sub2_polarity,
    )
    if _auto_sub_cancel_requested(job):
        _append_combined_timing("cancelled", left_result=left_result, right_result=right_result)
        return _auto_sub_cancelled_candidate(delay_ms, stage)

    left_points = left_result.get("points") or []
    right_points = right_result.get("points") or []
    points = left_points if len(left_points) >= 3 else right_points
    status = "completed" if (len(left_points) >= 3 or len(right_points) >= 3) else "failed"
    _append_combined_timing(status, left_result=left_result, right_result=right_result)

    candidate = {
        "delay_ms": delay_ms,
        "name": str(delay_ms),
        "points": points,
        "points_left": left_points,
        "points_right": right_points,
        "calibrated_points_left": left_result.get("calibrated_points") or [],
        "calibrated_points_right": right_result.get("calibrated_points") or [],
        "normalized_by_db_left": left_result.get("normalized_by_db"),
        "normalized_by_db_right": right_result.get("normalized_by_db"),
        "sweep_id": left_result.get("sweep_id", ""),
        "sweep_id_left": left_result.get("sweep_id", ""),
        "sweep_id_right": right_result.get("sweep_id", ""),
        "status": status,
        "scan": stage,
        "status_left": left_result.get("status"),
        "status_right": right_result.get("status"),
        "stage_output_peaks": {
            "left": left_result.get("stage_output_peaks"),
            "right": right_result.get("stage_output_peaks"),
        },
        "combined_candidate": True,
    }
    if output_mode in OUTPUT_MODE_SUBWOOFER_22_MODES:
        sub1_delay = _auto_sub_clamped_delay(sub1_alignment_ms if sub1_alignment_ms is not None else delay_ms)
        sub2_delay = _auto_sub_clamped_delay(sub2_alignment_ms if sub2_alignment_ms is not None else 0.0)
        candidate.update({
            "sub1_alignment_ms": sub1_delay,
            "sub2_alignment_ms": sub2_delay,
            "name": _auto_sub_22_name(sub1_delay, sub2_delay),
            "active_subs": list(active_subs),
            "sub1_polarity": sub1_polarity,
            "sub2_polarity": sub2_polarity,
        })
    return candidate
