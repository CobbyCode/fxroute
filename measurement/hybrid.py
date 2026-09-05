"""Signal analysis primitives for guided hybrid measurements.

The normal measurement pipeline owns sweep generation and deconvolution.  This
module only derives additional, role-aware information from that impulse
response so the classic measurement behavior remains unchanged.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np


DIRECT_MIN_GATE_MS = 1.5
DIRECT_MAX_GATE_MS = 24.0
DIRECT_UNCERTAIN_GATE_MS = 3.0
DIRECT_ENVELOPE_WINDOW_MS = 0.20
DIRECT_ENERGY_VALLEY_RATIO = 0.10
DIRECT_ENERGY_VALLEY_DURATION_MS = 0.25
DIRECT_REFLECTION_MIN_ENVELOPE_RATIO = 0.02
DIRECT_REFLECTION_RISE_RATIO = 2.0
DIRECT_REFLECTION_RISE_DURATION_MS = 0.08
DIRECT_FREQUENCY_CYCLES = 1.5
DIRECT_GATE_CONTRAST_SCALE = 1.5
DIRECT_GATE_RUNNER_LIMIT = 1.15
DIRECT_GATE_RUNNER_RANGE = 0.45
DIRECT_GATE_CLUSTER_WINDOW_MS = 0.75
DIRECT_GATE_CONTRAST_WINDOW_MS = 0.50


def _direct_gate_confidence(
    candidates: list[dict[str, float]],
    sample_rate: int,
) -> tuple[dict[str, Any], float]:
    """Rate first-match decisiveness without second-guessing the candidate."""
    if not candidates:
        return {"contrast": 0.0, "cluster": 0, "runner_ratio": 0.0, "candidate_count": 0}, 0.0
    first = candidates[0]
    contrast = float(first["peak_contrast"])
    cluster_window = max(1, int(round(sample_rate * DIRECT_GATE_CLUSTER_WINDOW_MS / 1000.0)))
    cluster = sum(1 for item in candidates if item["rise_index"] - first["rise_index"] <= cluster_window)
    later = [item for item in candidates if item["rise_index"] - first["rise_index"] > cluster_window]
    runner_ratio = float(later[0]["peak_contrast"]) / max(contrast, 1e-12) if later else 0.0
    contrast_part = min(1.0, max(0.0, (contrast - 1.0) / DIRECT_GATE_CONTRAST_SCALE))
    runner_part = min(1.0, max(0.0, (DIRECT_GATE_RUNNER_LIMIT - runner_ratio) / DIRECT_GATE_RUNNER_RANGE))
    confidence = contrast_part * runner_part
    metrics = {
        "contrast": round(contrast, 4),
        "cluster": int(cluster),
        "runner_ratio": round(runner_ratio, 4),
        "candidate_count": len(candidates),
    }
    return metrics, confidence
DIRECT_GATE_REPEAT_MAX_SPREAD_OCTAVES = 1.0 / 6.0
DIRECT_IR_SEGMENT_PRE_SAMPLES = 64
DIRECT_IR_SEGMENT_POST_SAMPLES = 64
COMPLEX_RESPONSE_MIN_HZ = 20.0
COMPLEX_RESPONSE_MAX_HZ = 2_000.0
COMPLEX_RESPONSE_POINTS = 160


def analyze_direct_window(
    impulse_response: np.ndarray,
    sample_rate: int,
    direct_arrival_index: int,
    *,
    timing_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Find the first material reflection and a conservative direct gate."""
    ir = np.asarray(impulse_response, dtype=np.float64)
    direct = max(0, min(int(direct_arrival_index), max(0, ir.size - 1)))
    minimum_samples = max(8, int(round(sample_rate * DIRECT_MIN_GATE_MS / 1000.0)))
    maximum_samples = max(minimum_samples, int(round(sample_rate * DIRECT_MAX_GATE_MS / 1000.0)))
    search_end = min(ir.size, direct + maximum_samples)
    support = max(4, int(round(sample_rate * 0.00025)))
    direct_end = min(ir.size, direct + support * 2)
    direct_peak = float(np.max(np.abs(ir[direct:direct_end]))) if direct_end > direct else 0.0

    envelope_samples = max(4, int(round(sample_rate * DIRECT_ENVELOPE_WINDOW_MS / 1000.0)))
    envelope_kernel = np.ones(envelope_samples, dtype=np.float64) / envelope_samples
    energy_envelope = np.sqrt(np.convolve(np.square(ir, dtype=np.float64), envelope_kernel, mode="same"))
    valley_samples = max(2, int(round(sample_rate * DIRECT_ENERGY_VALLEY_DURATION_MS / 1000.0)))
    reflection_index = None
    direct_event_end = None
    direct_envelope_peak = float(energy_envelope[direct]) if direct < energy_envelope.size else 0.0
    quiet_start = None
    for index in range(direct, search_end):
        value = float(energy_envelope[index])
        direct_envelope_peak = max(direct_envelope_peak, value)
        valley_threshold = direct_envelope_peak * DIRECT_ENERGY_VALLEY_RATIO
        if value <= valley_threshold:
            quiet_start = index if quiet_start is None else quiet_start
            if index - quiet_start + 1 >= valley_samples:
                direct_event_end = quiet_start
                break
        else:
            quiet_start = None

    reflection_threshold = None
    reflection_candidates: list[dict[str, float]] = []
    contrast_samples = max(4, int(round(sample_rate * DIRECT_GATE_CONTRAST_WINDOW_MS / 1000.0)))
    if direct_event_end is not None:
        search_start = direct_event_end + valley_samples
        baseline_samples = max(valley_samples, int(round(sample_rate * 0.50 / 1000.0)))
        guard_samples = max(1, int(round(sample_rate * 0.10 / 1000.0)))
        rise_samples = max(2, int(round(sample_rate * DIRECT_REFLECTION_RISE_DURATION_MS / 1000.0)))
        rise_start = None
        for index in range(search_start, search_end):
            baseline_end = max(direct_event_end + 1, index - guard_samples)
            baseline_start = max(direct_event_end, baseline_end - baseline_samples)
            local_baseline = float(np.median(energy_envelope[baseline_start:baseline_end]))
            threshold = max(
                direct_envelope_peak * DIRECT_REFLECTION_MIN_ENVELOPE_RATIO,
                local_baseline * DIRECT_REFLECTION_RISE_RATIO,
            )
            reflection_threshold = threshold
            if float(energy_envelope[index]) >= threshold:
                rise_start = index if rise_start is None else rise_start
                if index - rise_start + 1 >= rise_samples:
                    window = energy_envelope[rise_start:rise_start + contrast_samples]
                    peak_contrast = float(np.max(window)) / max(threshold, 1e-12)
                    reflection_candidates.append(
                        {
                            "rise_index": float(rise_start),
                            "threshold": float(threshold),
                            "confirm_margin": float(
                                np.min(energy_envelope[rise_start:index + 1])
                                / max(threshold, 1e-12)
                            ),
                            "peak_contrast": peak_contrast,
                        }
                    )
                    if reflection_index is None:
                        reflection_index = rise_start
                        reflection_threshold = threshold
                    rise_start = None
            else:
                rise_start = None
    if reflection_candidates:
        reflection_threshold = float(reflection_candidates[0]["threshold"])
    gate_metrics, gate_confidence = _direct_gate_confidence(reflection_candidates, sample_rate)

    margin = max(2, int(round(sample_rate * 0.00015)))
    if reflection_index is None:
        uncertain_samples = max(minimum_samples, int(round(sample_rate * DIRECT_UNCERTAIN_GATE_MS / 1000.0)))
        gate_end = min(ir.size, direct + uncertain_samples)
        status = "reflection-not-identifiable"
    elif reflection_index - margin < direct + minimum_samples:
        gate_end = max(direct + 1, reflection_index - margin)
        status = "reflection-too-early"
    else:
        gate_end = reflection_index - margin
        status = "ok"

    usable_samples = max(1, gate_end - direct)
    usable_seconds = usable_samples / float(sample_rate)
    lower_hz = DIRECT_FREQUENCY_CYCLES / usable_seconds
    fallback_confidence = min(1.0, max(0.0, direct_peak / max(float(np.max(np.abs(ir))), 1e-12)))
    metadata_confidence = (timing_metadata or {}).get("confidence")
    try:
        confidence = min(1.0, max(0.0, float(metadata_confidence))) if metadata_confidence is not None else fallback_confidence
    except (TypeError, ValueError):
        confidence = fallback_confidence
    direct_reliable = direct_peak > 1e-10 and confidence >= 0.05
    usable = direct_reliable and usable_samples >= minimum_samples and status == "ok"
    if not direct_reliable:
        status = "direct-arrival-unreliable"
    retry_reasons = {
        "reflection-not-identifiable": (
            "No trustworthy reflection-free interval was identified. Keep the microphone about 1 m from the speaker, "
            "increase its distance from nearby walls or objects, and repeat the measurement."
        ),
        "reflection-too-early": (
            "A strong reflection arrived too soon for a reliable direct response. Move the microphone farther from "
            "nearby walls or objects, keep it about 1 m from the speaker, and repeat the measurement."
        ),
        "direct-arrival-unreliable": (
            "The direct sound could not be identified reliably. Check the microphone distance and alignment with the "
            "speaker, then repeat the measurement."
        ),
    }
    # Full-resolution replay segment covering the detector search range.
    segment_start = max(0, direct - DIRECT_IR_SEGMENT_PRE_SAMPLES)
    segment_end = min(ir.size, search_end + DIRECT_IR_SEGMENT_POST_SAMPLES)
    if segment_end <= segment_start:
        segment_end = min(ir.size, segment_start + 1)
    ir_segment = {
        "start_index": int(segment_start),
        "end_index": int(segment_end),
        "sample_rate": int(sample_rate),
        "samples": [float(value) for value in ir[segment_start:segment_end]],
    }
    return {
        "status": status,
        "usable": usable,
        "direct_arrival_index": direct,
        "first_reflection_index": reflection_index,
        "first_reflection_ms": (
            round((reflection_index - direct) * 1000.0 / sample_rate, 3)
            if reflection_index is not None else None
        ),
        "gate_end_index": int(gate_end),
        "usable_window_ms": round(usable_seconds * 1000.0, 3),
        "gated_direct_lower_limit_hz": round(lower_hz, 1),
        "direct_confidence": round(confidence, 4),
        "gate_confidence": round(gate_confidence, 4),
        "direct_detection": {
            "selection_rule": str((timing_metadata or {}).get("selection_rule") or ""),
            "selected_score": round(float((timing_metadata or {}).get("selected_score") or 0.0), 6),
            "source": "established-timing-analysis" if metadata_confidence is not None else "local-amplitude-fallback",
        },
        "reflection_detection": {
            "method": "energy-envelope-after-sustained-valley",
            "direct_event_end_index": int(direct_event_end) if direct_event_end is not None else None,
            "direct_event_duration_ms": (
                round((direct_event_end - direct) * 1000.0 / sample_rate, 3)
                if direct_event_end is not None else None
            ),
            "envelope_window_ms": DIRECT_ENVELOPE_WINDOW_MS,
            "valley_duration_ms": DIRECT_ENERGY_VALLEY_DURATION_MS,
            "valley_ratio": DIRECT_ENERGY_VALLEY_RATIO,
            "minimum_envelope_ratio": DIRECT_REFLECTION_MIN_ENVELOPE_RATIO,
            "local_rise_ratio": DIRECT_REFLECTION_RISE_RATIO,
            "rise_duration_ms": DIRECT_REFLECTION_RISE_DURATION_MS,
            "detected_threshold": round(float(reflection_threshold), 12) if reflection_threshold is not None else None,
            "ir_segment": ir_segment,
            "gate_metrics": gate_metrics,
        },
        "method": "direct arrival to first material reflection",
        "retry_reason": retry_reasons.get(status, ""),
    }


def build_gated_response(
    impulse_response: np.ndarray,
    sample_rate: int,
    direct_window: dict[str, Any],
) -> tuple[np.ndarray, np.ndarray]:
    """Return a cosine-tapered quasi-anechoic magnitude response."""
    ir = np.asarray(impulse_response, dtype=np.float64)
    direct = int(direct_window["direct_arrival_index"])
    gate_end = int(direct_window["gate_end_index"])
    pre_samples = max(8, int(round(sample_rate * 0.0008)))
    start = max(0, direct - pre_samples)
    end = max(start + 2, min(ir.size, gate_end))
    segment = np.array(ir[start:end], copy=True)
    rise = min(pre_samples, max(2, direct - start))
    fall = min(max(8, segment.size // 5), max(2, end - direct))
    window = np.ones(segment.size, dtype=np.float64)
    if rise > 1:
        window[:rise] = 0.5 - 0.5 * np.cos(np.linspace(0.0, math.pi, rise))
    if fall > 1:
        window[-fall:] *= 0.5 + 0.5 * np.cos(np.linspace(0.0, math.pi, fall))
    fft_size = 1 << max(12, int(math.ceil(math.log2(max(sample_rate, segment.size * 4)))))
    spectrum = np.fft.rfft(segment * window, n=fft_size)
    return np.fft.rfftfreq(fft_size, 1.0 / sample_rate), np.abs(spectrum)


def build_complex_response(
    impulse_response: np.ndarray,
    sample_rate: int,
    *,
    calibration_curve: tuple[np.ndarray, np.ndarray] | None = None,
) -> dict[str, Any]:
    """Persist a compact common-time-reference response for vector sums."""
    ir = np.asarray(impulse_response, dtype=np.float64)
    post_samples = min(ir.size, max(64, int(round(sample_rate * 0.50))))
    peak = int(np.argmax(np.abs(ir))) if ir.size else 0
    start = max(0, peak - int(round(sample_rate * 0.004)))
    end = min(ir.size, max(start + 64, peak + post_samples))
    segment = np.array(ir[start:end], copy=True)
    fade = min(max(16, int(round(sample_rate * 0.012))), max(1, segment.size // 3))
    if fade > 1:
        segment[-fade:] *= 0.5 + 0.5 * np.cos(np.linspace(0.0, math.pi, fade))
    fft_size = 1 << max(12, int(math.ceil(math.log2(max(sample_rate, segment.size * 2)))))
    spectrum = np.fft.rfft(segment, n=fft_size)
    # Restore the phase origin removed by slicing the IR.
    frequencies = np.fft.rfftfreq(fft_size, 1.0 / sample_rate)
    spectrum *= np.exp(-1j * 2.0 * math.pi * frequencies * start / sample_rate)
    centers = np.geomspace(COMPLEX_RESPONSE_MIN_HZ, min(COMPLEX_RESPONSE_MAX_HZ, sample_rate * 0.45), COMPLEX_RESPONSE_POINTS)
    indices = np.clip(np.searchsorted(frequencies, centers), 0, spectrum.size - 1)
    values = spectrum[indices]
    if calibration_curve is not None:
        cal_hz, cal_db = calibration_curve
        offsets = np.interp(
            np.log(centers),
            np.log(cal_hz),
            cal_db,
            left=cal_db[0],
            right=cal_db[-1],
        )
        gain = 10.0 ** (-offsets / 20.0)
        values *= gain
    points = [
        [round(float(frequency), 3), round(float(value.real), 9), round(float(value.imag), 9)]
        for frequency, value in zip(centers, values)
    ]
    return {
        "schema": "fxroute.complex-response.v1",
        "points": points,
        "sample_rate": int(sample_rate),
        "time_reference": "deconvolved-sweep-origin",
        "normalization": "none",
        "window_seconds": round((end - start) / sample_rate, 6),
        "max_frequency_hz": round(float(centers[-1]), 3),
    }


def sum_complex_points(*responses: dict[str, Any]) -> list[list[float]]:
    """Vector-sum responses sampled on a shared frequency grid."""
    valid = [item.get("points") or [] for item in responses if isinstance(item, dict) and item.get("points")]
    if not valid:
        return []
    count = min(len(points) for points in valid)
    result = []
    for index in range(count):
        frequency = float(valid[0][index][0])
        value = sum(complex(float(points[index][1]), float(points[index][2])) for points in valid)
        result.append([round(frequency, 3), round(value.real, 9), round(value.imag, 9)])
    return result
