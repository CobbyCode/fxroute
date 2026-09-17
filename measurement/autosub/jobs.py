# SPDX-License-Identifier: AGPL-3.0-only

"""AutoSub job API, peak safety, sweep timing, and job finalization."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import audio.volume_contract as volume_contract
from audio.system_volume import volume_percent_to_linear_gain
from fastapi import APIRouter, HTTPException

from dsp.manager import ensure_kernel_supported_ir, parse_wav_frames
from dsp.runtime import BassManagementConfig

from .candidates import _restore_original_config_or_fail_job
from .deps import (
    _AUTO_SUB_CLEANUP_TASKS,
    _AUTO_SUB_JOBS,
    _auto_sub_lock,
    _dsp_manager,
    _measurement_session,
    _measurement_store,
    drop_candidate_owner,
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

# Persisted autosub-job-<id>.json snapshots kept in the measurements jobs dir.
_AUTO_SUB_SNAPSHOT_KEEP = 40

# Single-slot peak-prediction cache. The prediction is expensive (pure-Python
# cascaded biquads over the full sweep) and runs once per sweep; consecutive
# sweeps of a scan share the DSP config, sample rate and sink volume, so the
# last result is reused when every input that affects the DAC-level peaks is
# unchanged. Any change of those inputs (alignment, level, polarity, sweep
# profile, rate, channel, source/sink gain) produces a new key and recomputes.
# Stored and returned values are deep copies: callers mutate the returned
# dict (exact-sub-mute peak zeroing) without poisoning the cached entry.
_AUTO_SUB_PEAK_PREDICTION_CACHE_KEY: tuple | None = None
_AUTO_SUB_PEAK_PREDICTION_CACHE_RESULT: dict[str, Any] | None = None

# Same single-slot pattern for the compiled-layout model below. The key
# carries the model tag, the plan fingerprint, the canonical layout
# signature, the IR content identity of every convolver, the rate, the sweep,
# the channel and every gain, so any audible change recomputes.
_AUTO_SUB_PLAN_PEAK_PREDICTION_CACHE_KEY: tuple | None = None
_AUTO_SUB_PLAN_PEAK_PREDICTION_CACHE_RESULT: dict[str, Any] | None = None

# Native per-output PEQ types (native_dsp/dsp.c design()), mirrored by the
# layout predictor so its biquads match the engine coefficients.
_AUTO_SUB_PLAN_PEAK_FILTER_TYPES = frozenset({
    "bell", "notch", "lowpass", "highpass", "lowshelf", "highshelf",
})


def _auto_sub_peak_prediction_cache_key(
    *,
    sweep_profile: dict[str, Any],
    sample_rate: int,
    channel: str,
    config: BassManagementConfig,
    playback_gain: float,
    sink_gain: float,
) -> tuple:
    """Immutable key over every input the prediction reads."""
    return (
        round(float(sweep_profile["sweep_start_hz"]), 6),
        round(float(sweep_profile["sweep_end_hz"]), 6),
        round(float(sweep_profile["sweep_seconds"]), 6),
        int(sample_rate),
        str(channel),
        int(config.crossover_frequency_hz),
        bool(config.main_highpass_enabled),
        round(float(config.derived_main_delay_ms), 4),
        str(config.bass_routing),
        round(float(config.derived_sub1_delay_ms), 4),
        round(float(config.derived_sub2_delay_ms), 4),
        round(float(config.sub_level_db), 4),
        round(float(config.sub2_level_db), 4),
        str(config.sub_polarity),
        str(config.sub2_polarity),
        round(float(playback_gain), 8),
        round(float(sink_gain), 8),
    )


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

class AutoSubChainHealthError(RuntimeError):
    """Abort the complete AutoSub run after the capture chain degraded.

    Raised when a sweep's direct-arrival alignment shifts far beyond capture
    quantum jitter against the run's own baseline: the audio device state
    (resync-pre-filled output buffer, 799f3bd5d1ab) then corrupts every
    subsequent capture, and continuing would run disturbed audio and score
    garbage instead of stopping with a clear remediation message.
    """


def auto_sub_sink_gain_from_master_percent(percent: int | float, *, clamp_upper: bool = True) -> float:
    """Linear gain the hardware sink applies for a master percent.

    Same PipeWire/Pulse cubic curve as
    :func:`audio.system_volume.volume_percent_to_linear_gain` (verified on
    the .104 UMC204HD sink: 31% -> -30.5 dB, 10% -> -60.0 dB).  The peak
    safety read passes ``clamp_upper=False`` so an externally raised >100%
    master is never under-estimated.
    """
    return volume_percent_to_linear_gain(percent, clamp_upper=clamp_upper)

def _auto_sub_sweep_input_pcm(
    sweep_profile: dict[str, Any], sample_rate: int,
) -> np.ndarray:
    """Synthesize the unit measurement sweep both predictor models share.

    Log chirp with edge fades, normalized to 0.8 peak. Callers scale it by
    their source gain and assign it to the driven input channels.
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
    return sweep


def _auto_sub_native_peq_coefficients(
    filter_type: str, frequency_hz: float, sample_rate: int,
    q: float, gain_db: float,
) -> tuple[float, float, float, float, float]:
    """Mirror the native engine's PEQ biquad design (RBJ cookbook, S=1 shelves).

    Returns normalized ``(b0, b1, b2, a1, a2)`` exactly as native_dsp/dsp.c
    ``design()`` computes them, so the predictor and the engine agree on
    every bank PEQ response.
    """
    frequency = float(frequency_hz)
    rate = float(int(sample_rate))
    quality = float(q)
    gain = float(gain_db)
    if not quality > 0.0:
        raise ValueError("PEQ q must be positive")
    a = 10.0 ** (gain / 40.0)
    w = 2.0 * math.pi * frequency / rate
    cs, sn = math.cos(w), math.sin(w)
    alpha = sn / (2.0 * quality)
    beta = 2.0 * math.sqrt(a) * alpha
    if filter_type == "bell":
        b0, b1, b2, a0, a1, a2 = (1 + alpha * a, -2 * cs, 1 - alpha * a,
                                  1 + alpha / a, -2 * cs, 1 - alpha / a)
    elif filter_type == "notch":
        b0, b1, b2, a0, a1, a2 = (1, -2 * cs, 1, 1 + alpha, -2 * cs, 1 - alpha)
    elif filter_type == "lowpass":
        b0, b1, b2, a0, a1, a2 = ((1 - cs) / 2, 1 - cs, (1 - cs) / 2,
                                  1 + alpha, -2 * cs, 1 - alpha)
    elif filter_type == "highpass":
        b0, b1, b2, a0, a1, a2 = ((1 + cs) / 2, -(1 + cs), (1 + cs) / 2,
                                  1 + alpha, -2 * cs, 1 - alpha)
    elif filter_type == "lowshelf":
        b0, b1, b2, a0, a1, a2 = (a * ((a + 1) - (a - 1) * cs + beta),
                                  2 * a * ((a - 1) - (a + 1) * cs),
                                  a * ((a + 1) - (a - 1) * cs - beta),
                                  (a + 1) + (a - 1) * cs + beta,
                                  -2 * ((a - 1) + (a + 1) * cs),
                                  (a + 1) + (a - 1) * cs - beta)
    elif filter_type == "highshelf":
        b0, b1, b2, a0, a1, a2 = (a * ((a + 1) + (a - 1) * cs + beta),
                                  -2 * a * ((a - 1) + (a + 1) * cs),
                                  a * ((a + 1) + (a - 1) * cs - beta),
                                  (a + 1) - (a - 1) * cs + beta,
                                  2 * ((a - 1) - (a + 1) * cs),
                                  (a + 1) - (a - 1) * cs - beta)
    else:
        raise ValueError(f"Unsupported PEQ filter type: {filter_type}")
    return b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0


def _auto_sub_run_plan_biquad(
    values: np.ndarray, coefficients: tuple[float, float, float, float, float],
) -> np.ndarray:
    """Run one biquad with the native engine's state-variable structure."""
    b0, b1, b2, a1, a2 = coefficients
    output = np.empty_like(values)
    z1 = 0.0
    z2 = 0.0
    for index, value in enumerate(values):
        filtered = b0 * value + z1
        z1 = b1 * value - a1 * filtered + z2
        z2 = b2 * value - a2 * filtered
        output[index] = filtered
    return output


def _auto_sub_read_mono_ir(path: str, channel: int) -> np.ndarray:
    """Read one IR channel as float64 samples, failing closed on bad files.

    Uses the same WAV parsing and kernel-support gate as the manager, so the
    predictor only ever models IR files the native convolver would load.
    """
    if type(channel) is not int or channel < 0:
        raise ValueError("IR channel must be a non-negative integer")
    name = str(path) or ""
    try:
        params = parse_wav_frames(Path(name))
    except (OSError, ValueError) as exc:
        raise ValueError(f"IR file is not readable: {name or path}") from exc
    try:
        ensure_kernel_supported_ir(params, Path(name).name)
    except ValueError as exc:
        raise ValueError(f"IR file has an unsupported encoding: {name}") from exc
    if channel >= params["channels"]:
        raise ValueError(
            f"IR channel {channel} exceeds the {params['channels']} channels of {name}")
    data = params["data"]
    if params["format"] == 3:
        samples = np.frombuffer(data, dtype="<f4").astype(np.float64)
    elif params["bits"] == 16:
        samples = np.frombuffer(data, dtype="<i2").astype(np.float64) / 32768.0
    elif params["bits"] == 32:
        samples = np.frombuffer(data, dtype="<i4").astype(np.float64) / 2147483648.0
    else:
        raw = np.frombuffer(data, dtype=np.uint8).astype(np.int32).reshape(-1, 3)
        signed = (raw[:, 0] | (raw[:, 1] << 8) | (raw[:, 2] << 16)).astype(np.float64)
        signed -= 16777216.0 * (signed >= 8388608.0)
        samples = signed / 8388608.0
    frames = params["frames"]
    if samples.size != frames * params["channels"]:
        raise ValueError(f"IR file has truncated frames: {name}")
    return np.ascontiguousarray(samples.reshape(frames, params["channels"])[:, channel])


def _auto_sub_convolve_mono(signal: np.ndarray, taps: np.ndarray) -> np.ndarray:
    """Causal FIR convolution of one block from zero state, truncated to it."""
    size = 1
    while size < signal.size + taps.size - 1:
        size *= 2
    full = np.fft.irfft(
        np.fft.rfft(signal, size) * np.fft.rfft(taps, size), size)
    return np.ascontiguousarray(full[:signal.size])


def _auto_sub_plan_peak_cache_key(
    *, sweep_profile: dict[str, Any], sample_rate: int, channel: str,
    layout_signature: str, plan_fingerprint: str, ir_identity: tuple,
    output_gain_db: float, playback_gain: float, sink_gain: float,
) -> tuple:
    """Immutable key over every input the layout prediction reads."""
    return (
        "compiled-layout-v1",
        str(plan_fingerprint),
        str(layout_signature),
        tuple(ir_identity),
        round(float(sweep_profile["sweep_start_hz"]), 6),
        round(float(sweep_profile["sweep_end_hz"]), 6),
        round(float(sweep_profile["sweep_seconds"]), 6),
        int(sample_rate),
        str(channel),
        round(float(output_gain_db), 6),
        round(float(playback_gain), 8),
        round(float(sink_gain), 8),
    )


def _auto_sub_validated_layout_output(
    entry: dict[str, Any], index: int, sample_rate: int,
) -> dict[str, Any]:
    """Normalize one compiled layout output, failing closed on garbage."""
    prefix = f"layout output {index + 1}"
    if not isinstance(entry, dict):
        raise ValueError(f"{prefix} must be an object")
    routes = entry.get("routes")
    if not isinstance(routes, list) or not routes:
        raise ValueError(f"{prefix} requires a non-empty routes array")
    normalized_routes = []
    for route in routes:
        source = route.get("input") if isinstance(route, dict) else None
        gain = route.get("gain", 1.0) if isinstance(route, dict) else None
        if type(source) is not int or source not in (0, 1):
            raise ValueError(f"{prefix} routes must address stereo input 0 or 1")
        try:
            gain_value = float(gain)
        except (TypeError, ValueError):
            raise ValueError(f"{prefix} route gain must be numeric") from None
        if not math.isfinite(gain_value):
            raise ValueError(f"{prefix} route gain must be finite")
        normalized_routes.append((source, gain_value))
    try:
        gain_db = float(entry.get("gain_db", 0.0))
        delay_ms = float(entry.get("delay_ms", 0.0))
    except (TypeError, ValueError):
        raise ValueError(f"{prefix} gain_db/delay_ms must be numeric") from None
    if not math.isfinite(gain_db) or not -80.0 <= gain_db <= 24.0:
        raise ValueError(f"{prefix} gain_db must be between -80 and 24")
    if not math.isfinite(delay_ms) or not 0.0 <= delay_ms <= 500.0:
        raise ValueError(f"{prefix} delay_ms must be between 0 and 500")
    filters = entry.get("filters", [])
    if not isinstance(filters, list):
        raise ValueError(f"{prefix} filters must be an array")
    biquads: list[tuple[float, float, float, float, float]] = []
    for position, raw in enumerate(filters):
        if not isinstance(raw, dict) or raw.get("type") not in _AUTO_SUB_PLAN_PEAK_FILTER_TYPES:
            raise ValueError(f"{prefix} filters[{position}] has an unsupported type")
        try:
            frequency = float(raw["frequency_hz"])
            quality = float(raw.get("q", 0.70710678))
            band_gain = float(raw.get("gain_db", 0.0))
            stages_number = float(raw.get("stages", 1))
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"{prefix} filters[{position}] has non-numeric parameters") from None
        if not math.isfinite(frequency) or not 20.0 <= frequency <= 20000.0:
            raise ValueError(f"{prefix} filters[{position}] frequency is out of range")
        if not frequency < sample_rate / 2:
            raise ValueError(f"{prefix} filters[{position}] frequency must be below Nyquist")
        if not math.isfinite(quality) or not 0.1 <= quality <= 20.0:
            raise ValueError(f"{prefix} filters[{position}] q is out of range")
        if not math.isfinite(band_gain) or not -24.0 <= band_gain <= 24.0:
            raise ValueError(f"{prefix} filters[{position}] gain is out of range")
        if (not math.isfinite(stages_number) or not stages_number.is_integer()
                or not 1 <= int(stages_number) <= 32):
            raise ValueError(f"{prefix} filters[{position}] stages must be a whole number 1..32")
        stages = int(stages_number)
        coefficients = _auto_sub_native_peq_coefficients(
            raw["type"], frequency, sample_rate, quality, band_gain)
        biquads.extend([coefficients] * stages)
    sos = entry.get("sos", [])
    if not isinstance(sos, list):
        raise ValueError(f"{prefix} sos must be an array")
    for position, section in enumerate(sos):
        if (not isinstance(section, (list, tuple)) or len(section) != 5
                or not all(type(value) in (int, float) and math.isfinite(value)
                           for value in section)):
            raise ValueError(f"{prefix} sos[{position}] must be five finite numbers")
        _, _, _, a1, a2 = (float(value) for value in section)
        if not (abs(a2) < 1.0 and abs(a1) < 1.0 + a2):
            raise ValueError(f"{prefix} sos[{position}] has poles outside the unit circle")
        biquads.append(tuple(float(value) for value in section))
    oconv = entry.get("oconv")
    convolver = None
    if oconv is not None:
        if not isinstance(oconv, dict) or set(oconv) != {
                "path", "channel", "wet_db", "dry_db",
                "input_gain_db", "output_gain_db"}:
            raise ValueError(f"{prefix} oconv requires path, channel and four gains")
        try:
            gains = {key: float(oconv[key]) for key in (
                "wet_db", "dry_db", "input_gain_db", "output_gain_db")}
        except (TypeError, ValueError):
            raise ValueError(f"{prefix} oconv gains must be numeric") from None
        if not all(math.isfinite(value) for value in gains.values()):
            raise ValueError(f"{prefix} oconv gains must be finite")
        taps = _auto_sub_read_mono_ir(str(oconv["path"] or ""), oconv["channel"])
        convolver = {"taps": taps, **gains}
    return {"routes": normalized_routes, "gain_db": gain_db, "delay_ms": delay_ms,
            "invert": bool(entry.get("invert", False)),
            "biquads": biquads, "convolver": convolver}


def _auto_sub_layout_peak_prediction(
    *, sweep_profile: dict[str, Any], sample_rate: int, channel: str,
    layout: list[dict[str, Any]], plan_fingerprint: str | None,
    output_gain_db: float, playback_gain: float = 1.0,
    sink_gain: float = 1.0,
) -> dict[str, Any]:
    """Run the known measurement PCM through one compiled output layout.

    Per output, in native engine order: route sums → PEQ/crossover biquads →
    mono convolver → delay/trim/polarity → runtime output gain → sink gain.
    Only the per-output chain is modeled; the global bank chain holds
    arbitrary LV2 plugins the predictor cannot evaluate, so callers must run
    this against a neutral global path (as staged AutoSub candidates do).
    Peaks are keyed ``output_1..N`` in layout order, matching the native
    peak meter.
    """
    rate = int(sample_rate)
    if rate <= 0:
        raise ValueError("sample_rate must be a positive integer")
    if channel not in ("left", "right", "stereo"):
        raise ValueError("channel must be left, right or stereo")
    if not isinstance(plan_fingerprint, str) or not plan_fingerprint:
        raise ValueError("Layout peak prediction requires the compiled plan fingerprint")
    try:
        runtime_gain = float(output_gain_db)
        source_gain = float(playback_gain)
        sink_linear = float(sink_gain)
    except (TypeError, ValueError) as exc:
        raise ValueError("output_gain_db, playback_gain and sink_gain must be numeric") from exc
    for value, label in ((runtime_gain, "output_gain_db"), (source_gain, "playback_gain"),
                         (sink_linear, "sink_gain")):
        if not math.isfinite(value):
            raise ValueError(f"{label} must be finite")
    if not -80.0 <= runtime_gain <= 0.0:
        raise ValueError("output_gain_db must be between -80 and 0")
    if source_gain < 0.0 or sink_linear < 0.0:
        raise ValueError("playback_gain and sink_gain must be non-negative")
    if not isinstance(layout, list) or not layout:
        raise ValueError("Layout peak prediction requires a non-empty layout")
    validated = [_auto_sub_validated_layout_output(entry, index, rate)
                 for index, entry in enumerate(layout)]
    try:
        layout_signature = json.dumps(layout, sort_keys=True, separators=(",", ":"),
                                      allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Layout peak prediction requires a JSON-serializable layout") from exc
    ir_identity = []
    for entry, stage in zip(layout, validated):
        convolver = stage["convolver"]
        if convolver is None:
            ir_identity.append(None)
            continue
        path = str((entry.get("oconv") or {}).get("path") or "")
        try:
            stat = os.stat(path)
        except OSError as exc:
            raise ValueError(f"IR file is not readable: {path}") from exc
        ir_identity.append((path, stat.st_size, stat.st_mtime_ns))
    global _AUTO_SUB_PLAN_PEAK_PREDICTION_CACHE_KEY, _AUTO_SUB_PLAN_PEAK_PREDICTION_CACHE_RESULT
    cache_key = _auto_sub_plan_peak_cache_key(
        sweep_profile=sweep_profile, sample_rate=rate, channel=channel,
        layout_signature=layout_signature, plan_fingerprint=plan_fingerprint,
        ir_identity=tuple(ir_identity), output_gain_db=runtime_gain,
        playback_gain=source_gain, sink_gain=sink_linear,
    )
    if (_AUTO_SUB_PLAN_PEAK_PREDICTION_CACHE_KEY == cache_key
            and _AUTO_SUB_PLAN_PEAK_PREDICTION_CACHE_RESULT is not None):
        return copy.deepcopy(_AUTO_SUB_PLAN_PEAK_PREDICTION_CACHE_RESULT)
    sweep = _auto_sub_sweep_input_pcm(sweep_profile, rate) * source_gain
    zeros = np.zeros_like(sweep)
    inputs = (sweep if channel in ("left", "stereo") else zeros,
              sweep if channel in ("right", "stereo") else zeros)
    runtime_linear = 10.0 ** (runtime_gain / 20.0)

    def shifted(signal: np.ndarray, delay_ms: float) -> np.ndarray:
        samples = int(float(delay_ms) * rate / 1000.0 + 0.5)
        if samples <= 0:
            return signal
        return np.concatenate((np.zeros(samples), signal))[:signal.size]

    peaks = {}
    for position, stage in enumerate(validated):
        signal = sum(gain * inputs[source] for source, gain in stage["routes"])
        for coefficients in stage["biquads"]:
            signal = _auto_sub_run_plan_biquad(np.ascontiguousarray(signal), coefficients)
        convolver = stage["convolver"]
        if convolver is not None:
            driven = signal * 10.0 ** (convolver["input_gain_db"] / 20.0)
            wet = _auto_sub_convolve_mono(driven, convolver["taps"])
            signal = (10.0 ** (convolver["output_gain_db"] / 20.0)
                      * (10.0 ** (convolver["dry_db"] / 20.0) * driven
                         + 10.0 ** (convolver["wet_db"] / 20.0) * wet))
        signal = shifted(np.ascontiguousarray(signal), stage["delay_ms"])
        signal = signal * 10.0 ** (stage["gain_db"] / 20.0)
        if stage["invert"]:
            signal = -signal
        signal = signal * runtime_linear * sink_linear
        peaks[f"output_{position + 1}"] = float(np.max(np.abs(signal))) if signal.size else 0.0
    peak_dbfs = {key: round(20.0 * math.log10(max(value, 1e-12)), 3) for key, value in peaks.items()}
    result = {
        "model": "compiled-layout-v1",
        "plan_fingerprint": plan_fingerprint,
        "linear": peaks,
        "dbfs": peak_dbfs,
        "maximum_dbfs": max(peak_dbfs.values()),
        "limit_dbfs": _AUTO_SUB_STAGE_PEAK_LIMIT_DBFS,
        "safe": max(peak_dbfs.values()) <= _AUTO_SUB_STAGE_PEAK_LIMIT_DBFS,
        "playback_gain": source_gain,
        "sink_gain": sink_linear,
        "output_gain_db": runtime_gain,
    }
    _AUTO_SUB_PLAN_PEAK_PREDICTION_CACHE_KEY = cache_key
    _AUTO_SUB_PLAN_PEAK_PREDICTION_CACHE_RESULT = copy.deepcopy(result)
    return result


def _auto_sub_stage_peak_prediction(
    *, sweep_profile: dict[str, Any], sample_rate: int, channel: str,
    config: BassManagementConfig | None = None,
    layout: list[dict[str, Any]] | None = None,
    plan_fingerprint: str | None = None,
    output_gain_db: float = 0.0,
    playback_gain: float = 1.0,
    sink_gain: float = 1.0,
) -> dict[str, Any]:
    """Run the known measurement PCM through the native DSP topology.

    ``sink_gain`` is the linear amplitude gain the hardware sink applies to
    the engine output (0..1) before the float→integer conversion that clips.
    Use :func:`auto_sub_sink_gain_from_master_percent` to convert the master
    percent to this linear gain. The engine chain is linear, so folding it
    into the sweep yields the true DAC-level peaks without touching the
    Mono/Stereo routing.

    Exactly one of ``config`` (legacy four-output bass management) or
    ``layout`` (compiled multichannel output layout, with ``plan_fingerprint``
    and the runtime ``output_gain_db`` in [-80, 0]) selects the model.
    """
    if (config is None) == (layout is None):
        raise ValueError("Peak prediction requires exactly one of config or layout")
    if layout is not None:
        return _auto_sub_layout_peak_prediction(
            sweep_profile=sweep_profile, sample_rate=sample_rate, channel=channel,
            layout=layout, plan_fingerprint=plan_fingerprint,
            output_gain_db=output_gain_db,
            playback_gain=playback_gain, sink_gain=sink_gain,
        )
    rate = int(sample_rate)
    duration = float(sweep_profile["sweep_seconds"])
    try:
        source_gain = float(playback_gain)
        sink_linear = float(sink_gain)
    except (TypeError, ValueError) as exc:
        raise ValueError("playback_gain and sink_gain must be finite non-negative numbers") from exc
    if not math.isfinite(source_gain) or source_gain < 0.0:
        raise ValueError("playback_gain must be a finite non-negative number")
    if not math.isfinite(sink_linear) or sink_linear < 0.0:
        raise ValueError("sink_gain must be a finite non-negative number")
    global _AUTO_SUB_PEAK_PREDICTION_CACHE_KEY, _AUTO_SUB_PEAK_PREDICTION_CACHE_RESULT
    cache_key = _auto_sub_peak_prediction_cache_key(
        sweep_profile=sweep_profile,
        sample_rate=sample_rate,
        channel=channel,
        config=config,
        playback_gain=source_gain,
        sink_gain=sink_linear,
    )
    if _AUTO_SUB_PEAK_PREDICTION_CACHE_KEY == cache_key and _AUTO_SUB_PEAK_PREDICTION_CACHE_RESULT is not None:
        return copy.deepcopy(_AUTO_SUB_PEAK_PREDICTION_CACHE_RESULT)
    # Only a cache miss pays for synthesizing the sweep PCM and filtering it.
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
    result = {
        "linear": peaks,
        "dbfs": peak_dbfs,
        "maximum_dbfs": max(peak_dbfs.values()),
        "limit_dbfs": _AUTO_SUB_STAGE_PEAK_LIMIT_DBFS,
        "safe": max(peak_dbfs.values()) <= _AUTO_SUB_STAGE_PEAK_LIMIT_DBFS,
        "playback_gain": source_gain,
        "sink_gain": sink_linear,
    }
    _AUTO_SUB_PEAK_PREDICTION_CACHE_KEY = cache_key
    _AUTO_SUB_PEAK_PREDICTION_CACHE_RESULT = copy.deepcopy(result)
    return result

async def _predict_auto_sub_stage_peaks(
    *,
    sweep_profile: dict[str, Any],
    sample_rate: int,
    channel: str,
    config: BassManagementConfig | None = None,
    layout: list[dict[str, Any]] | None = None,
    plan_fingerprint: str | None = None,
    output_gain_db: float = 0.0,
    playback_gain: float = 1.0,
    sink_gain: float = 1.0,
) -> dict[str, Any]:
    """Run the peak prediction off the event loop.

    The prediction filters the full sweep PCM through four cascaded Python
    biquads (seconds of CPU per call); on the event loop it would freeze every
    HTTP handler (job poll, cancel, heartbeat) and the background measurement
    jobs for the duration. It runs in the default executor instead; the
    single-slot cache inside the sync function still skips repeated sweeps
    whose DSP state, sample rate and sink volume are unchanged.
    """
    return await asyncio.to_thread(
        _auto_sub_stage_peak_prediction,
        sweep_profile=sweep_profile,
        sample_rate=sample_rate,
        channel=channel,
        config=config,
        layout=layout,
        plan_fingerprint=plan_fingerprint,
        output_gain_db=output_gain_db,
        playback_gain=playback_gain,
        sink_gain=sink_gain,
    )

def _auto_sub_zero_sub_peaks(
    prediction: dict[str, Any], sub_indices: tuple[int, ...],
) -> dict[str, Any]:
    """Fold a Main-only capture's muted subs into the stage peak prediction.

    Returns a deep copy with every sub engine output's predicted peak zeroed,
    exactly as the exact sub mute will silence them during the sweep, then
    recomputes the maximum and the safety verdict. The input prediction is
    not mutated. Outputs the model does not expose fail closed instead of
    silently extending the prediction.
    """
    for index in sub_indices:
        if type(index) is not int or index < 0:
            raise ValueError("Sub output indices must be non-negative integers")
        key = f"output_{index + 1}"
        if key not in prediction["linear"] or key not in prediction["dbfs"]:
            raise ValueError(f"Exact sub mute output {key} is absent from the peak prediction")
    result = copy.deepcopy(prediction)
    for index in sub_indices:
        result["linear"][f"output_{index + 1}"] = 0.0
        result["dbfs"][f"output_{index + 1}"] = -240.0
    result["maximum_dbfs"] = max(result["dbfs"].values())
    result["safe"] = result["maximum_dbfs"] <= _AUTO_SUB_STAGE_PEAK_LIMIT_DBFS
    return result


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
    predicted_keys = set((predicted.get("dbfs") or {}))
    measured_keys = set(measured_linear or {})
    if predicted_keys != measured_keys:
        raise ValueError(
            "Peak comparison output sets differ: "
            f"predicted={sorted(predicted_keys)} measured={sorted(measured_keys)}")
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

def _auto_sub_executed_sweep_count(job: Mapping[str, Any]) -> int:
    """Physical sweep measurements the job actually ran, per side.

    Every measured candidate (each side of a combined candidate, main
    references, polarity, gain and confirmation sweeps included) appends one
    entry to the job's ``_sweep_timings`` ledger, so this count reflects the
    sweeps that really happened instead of a static plan that misses the
    late polarity/gain/confirmation stages or over-counts gated-out ones.
    """
    return len(job.get("_sweep_timings") or [])

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

def _persist_auto_sub_job_snapshot(job: dict[str, Any], job_id: str) -> None:
    """Write the final job state next to the persisted sweep records.

    AutoSub job state lives only in memory and is cleaned up after 600 s.
    The snapshot keeps the candidate ledger, rankings, polarity evidence,
    deep-bass check, gain verdicts and display measurements available for
    later analysis. Failures are logged and never affect the job outcome.
    """
    try:
        store = _measurement_store()
        jobs_dir = getattr(store, "jobs_dir", None) if store is not None else None
        if not jobs_dir:
            return
        target_dir = Path(jobs_dir) / "autosub"
        target_dir.mkdir(parents=True, exist_ok=True)
        snapshot = {
            "job_id": job_id,
            "mode": job.get("mode"),
            "status": job.get("status"),
            "message": job.get("message"),
            "crossover_hz": job.get("crossover_hz"),
            "step_ms": job.get("step_ms"),
            "original_alignment_ms": job.get("original_alignment_ms"),
            "original_sub1_alignment_ms": job.get("original_sub1_alignment_ms"),
            "original_sub2_alignment_ms": job.get("original_sub2_alignment_ms"),
            "result": job.get("result"),
            "auto_gain": job.get("auto_gain"),
            "polarity_check": job.get("polarity_check"),
            "balance_check": job.get("balance_check"),
            "confirmation_gate": job.get("confirmation_gate"),
            "deep_bass_check": job.get("deep_bass_check"),
            "fine_scan": job.get("fine_scan"),
            "sweep_timings": job.get("_sweep_timings"),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        target = target_dir / f"autosub-job-{job_id}.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snapshot, default=str), encoding="utf-8")
        tmp.replace(target)
        _prune_auto_sub_snapshots(target_dir)
        logger.info("AUTOSUB job=%s result snapshot persisted: %s", job_id, target)
    except Exception:
        logger.exception("AUTOSUB job=%s result snapshot persistence failed", job_id)

def _prune_auto_sub_snapshots(target_dir: Path) -> None:
    """Keep only the newest AutoSub job snapshots."""
    try:
        snapshots = sorted(target_dir.glob("autosub-job-*.json"), key=lambda path: path.stat().st_mtime)
        for stale in snapshots[:max(0, len(snapshots) - _AUTO_SUB_SNAPSHOT_KEEP)]:
            try:
                stale.unlink()
            except OSError:
                pass
    except OSError:
        pass

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
    if str(job.get("status") or "").lower() in {"completed", "failed"}:
        _persist_auto_sub_job_snapshot(job, job_id)
    logger.info("AUTOSUB job=%s cleanup complete state=%s", job_id, job.get("status") or "idle")

async def _finish_auto_sub_worker(job: dict[str, Any] | None, job_id: str) -> None:
    """Drain service-owner cleanup even if the worker is cancelled repeatedly."""
    if job is None or "output_state_context" not in job:
        await _finish_auto_sub_worker_cleanup(job, job_id)
        return
    cleanup = asyncio.create_task(_finish_auto_sub_worker_cleanup(job, job_id))
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    cleanup.result()
    if cancelled:
        raise asyncio.CancelledError


async def _register_autosub_release_adapter(job: dict[str, Any]) -> None:
    """Register the committed-plan release adapter before session unregister.

    Committed service jobs only: the adapter renders the current committed
    output plan at the restore rate so the session release rebuilds it
    instead of the stale legacy overview.  Anything unconfigured (no
    services, no factory, no session) or uncommitted skips quietly and the
    release keeps the legacy path; registration failure never fails the job
    or blocks the unregister (see the cleanup contract below).
    """
    context = job.get("output_state_context") or {}
    if "committed_revision" not in context:
        return
    try:
        from measurement.session import _measurement_services
        services = _measurement_services()
    except RuntimeError:
        logger.warning(
            "AUTOSUB job=%s release adapter skipped: measurement services are not configured",
            job.get("id") or "",
        )
        return
    if services.build_autosub_release_adapter is None:
        logger.warning(
            "AUTOSUB job=%s release adapter skipped: factory is not composed; "
            "release keeps the legacy overview sync",
            job.get("id") or "",
        )
        return
    session = _measurement_session()
    if session is None:
        logger.warning(
            "AUTOSUB job=%s release adapter skipped: measurement session is unavailable",
            job.get("id") or "",
        )
        return
    adapter = services.build_autosub_release_adapter(
        output_key=context["output_key"], channels=context["channels"])
    await session.register_autosub_release_adapter(adapter)


async def _finish_auto_sub_worker_cleanup(job: dict[str, Any] | None, job_id: str) -> None:
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
        if job is not None and "output_state_context" in job:
            await _restore_original_config_or_fail_job(
                job, {}, "Auto Sub Optimize failed to restore its output-state owner")
        if job is not None:
            try:
                await _register_autosub_release_adapter(job)
            except Exception:
                logger.exception(
                    "AUTOSUB job=%s release adapter registration failed", job_id
                )
        if measurement_sr_session is not None:
            try:
                await measurement_sr_session.unregister_auto_sub(job_id)
            except Exception:
                logger.exception(
                    "AUTOSUB job=%s measurement sample-rate session unregister failed", job_id
                )
        drop_candidate_owner(job_id)
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
