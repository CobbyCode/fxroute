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
DIRECT_REFLECTION_SEARCH_START_MS = 0.7
DIRECT_REFLECTION_PEAK_RATIO = 0.12
DIRECT_REFLECTION_ENERGY_RATIO = 0.20
DIRECT_FREQUENCY_CYCLES = 1.5
COMPLEX_RESPONSE_MIN_HZ = 20.0
COMPLEX_RESPONSE_MAX_HZ = 2_000.0
COMPLEX_RESPONSE_POINTS = 160


def analyze_direct_window(
    impulse_response: np.ndarray,
    sample_rate: int,
    direct_arrival_index: int,
) -> dict[str, Any]:
    """Find the first material reflection and a conservative direct gate."""
    ir = np.asarray(impulse_response, dtype=np.float64)
    direct = max(0, min(int(direct_arrival_index), max(0, ir.size - 1)))
    minimum_samples = max(8, int(round(sample_rate * DIRECT_MIN_GATE_MS / 1000.0)))
    maximum_samples = max(minimum_samples, int(round(sample_rate * DIRECT_MAX_GATE_MS / 1000.0)))
    search_start = min(ir.size, direct + max(4, int(round(sample_rate * DIRECT_REFLECTION_SEARCH_START_MS / 1000.0))))
    search_end = min(ir.size, direct + maximum_samples)
    support = max(4, int(round(sample_rate * 0.00025)))
    direct_end = min(ir.size, direct + max(minimum_samples, support * 2))
    direct_peak = float(np.max(np.abs(ir[direct:direct_end]))) if direct_end > direct else 0.0
    direct_energy = float(np.sum(np.square(ir[direct:direct_end], dtype=np.float64)))

    reflection_index = None
    if direct_peak > 0 and search_end - search_start > support * 2:
        absolute = np.abs(ir)
        for index in range(search_start + support, search_end - support):
            value = float(absolute[index])
            if value < direct_peak * DIRECT_REFLECTION_PEAK_RATIO:
                continue
            if value < float(absolute[index - 1]) or value < float(absolute[index + 1]):
                continue
            local_energy = float(np.sum(np.square(ir[index - support:index + support + 1], dtype=np.float64)))
            if local_energy >= direct_energy * DIRECT_REFLECTION_ENERGY_RATIO:
                reflection_index = index
                break

    if reflection_index is None:
        gate_end = min(ir.size, direct + maximum_samples)
        status = "reflection-not-distinct"
    else:
        margin = max(2, int(round(sample_rate * 0.00015)))
        gate_end = max(direct + minimum_samples, reflection_index - margin)
        status = "ok"

    usable_samples = max(1, gate_end - direct)
    usable_seconds = usable_samples / float(sample_rate)
    lower_hz = DIRECT_FREQUENCY_CYCLES / usable_seconds
    confidence = min(1.0, max(0.0, direct_peak / max(float(np.max(np.abs(ir))), 1e-12)))
    usable = direct_peak > 1e-10 and usable_samples >= minimum_samples and confidence >= 0.05
    if not usable:
        status = "unusable-direct-arrival"
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
        "lower_reliable_hz": round(lower_hz, 1),
        "direct_confidence": round(confidence, 4),
        "method": "direct arrival to first material reflection",
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
        gain = 10.0 ** (np.interp(centers, cal_hz, cal_db, left=cal_db[0], right=cal_db[-1]) / 20.0)
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
