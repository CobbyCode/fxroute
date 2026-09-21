# SPDX-License-Identifier: AGPL-3.0-only
"""Shared AutoSub/Speaker alignment backend: physical Delay/Gain before PEQ.

Both alignment flows measure through the active crossover with Global and
area PEQ/convolver banks neutralized, then correct physical Delay/Gain
first; PEQ/convolver correction happens afterwards on top of the aligned
base. Speaker time alignment stays inside one speaker (no L/R), gain is
estimated per way from its usable passband (robust median, never a single
point and never total energy across differently wide ways).
"""

from __future__ import annotations

import copy
import math
import statistics

from dsp.crossover import crossover_response, design_crossover

# Usable passband: crossover attenuation must stay within this flatness.
# Narrow mids never reach -1 dB flat (overlap dip); fall back to -3 dB.
PASSBAND_FLAT_DB = 1.0
PASSBAND_FALLBACK_DB = 3.0
# Robustness gates for one way's level estimate.
# Real drivers/headphones vary broadly across the band; 1-octave smoothing
# removes narrow resonances before the median, the MAD gate only rejects
# broken captures (silence, clipping, interference). Confidence is graded,
# only extreme instability fails closed. Headphone close-mic proof shows
# MAD 11-16 dB on real captures with still meaningful medians.
MIN_PASSBAND_POINTS = 6
MIN_PASSBAND_OCTAVES = 1.0 / 6.0
MAX_PASSBAND_MAD_DB = 18.0
SMOOTHING_OCTAVES = 1.0
# Physical gain corrections are bounded; larger raw values fail closed.
MAX_WAY_GAIN_DB = 12.0
# Post-alignment verification: ways of one side must agree within this.
# 2 dB allows real driver variation while catching multi-dB mismatches.
MAX_VERIFIED_GAIN_SPREAD_DB = 2.0


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Alignment {label} must be finite")
    return float(value)


def _flat_band(sections: list, *, sample_rate_hz: int, threshold_db: float) -> tuple[float, float] | None:
    nyquist = sample_rate_hz / 2.0
    low = max(50.0, 20.0)
    high = min(18000.0, nyquist * 0.9)
    points = 240
    best: tuple[float, float] | None = None
    start: float | None = None
    previous: float | None = None

    def commit() -> None:
        nonlocal best, start, previous
        if start is not None and previous is not None and previous > start:
            width = math.log2(previous / start)
            current = 0.0 if best is None else math.log2(best[1] / best[0])
            if width > current:
                best = (start, previous)
        start = None
        previous = None

    for index in range(points):
        fraction = index / (points - 1)
        frequency = low * (high / low) ** fraction
        total = 1.0 + 0.0j
        for b0, b1, b2, _, a1, a2 in sections:
            total *= crossover_response([(b0, b1, b2, 1.0, a1, a2)], frequency, sample_rate_hz)
        magnitude_db = 20.0 * math.log10(max(abs(total), 1e-12))
        if magnitude_db >= -threshold_db:
            if start is None:
                start = frequency
            previous = frequency
        else:
            commit()
    commit()
    return best


def way_passband(processing: dict, *, sample_rate_hz: int) -> tuple[float, float]:
    """Return the usable flat passband of one crossover way in Hz.

    The band covers frequencies where the way's own crossover filters stay
    within PASSBAND_FLAT_DB of flat, intersected with the measurement range
    and Nyquist. Narrow mids whose overlap never reaches -1 dB fall back to
    -3 dB; a way without any flat region fails closed instead of guessing.
    """
    if type(sample_rate_hz) is not int or sample_rate_hz <= 0:
        raise ValueError("Alignment sample rate must be a positive integer")
    highpass = processing.get("highpass")
    lowpass = processing.get("lowpass")
    if highpass is None and lowpass is None:
        raise ValueError("Alignment way has no crossover filters")
    specs = []
    for kind, spec in (("highpass", highpass), ("lowpass", lowpass)):
        if spec is None:
            continue
        if not isinstance(spec, dict):
            raise ValueError("Alignment crossover filter must be an object")
        full = dict(spec)
        full["kind"] = kind
        specs.append(full)
    sections = []
    for spec in specs:
        sections.extend(design_crossover(spec, sample_rate_hz))
    for threshold in (PASSBAND_FLAT_DB, PASSBAND_FALLBACK_DB):
        band = _flat_band(sections, sample_rate_hz=sample_rate_hz, threshold_db=threshold)
        if band is not None and band[1] > band[0]:
            return float(band[0]), float(band[1])
    raise ValueError("Alignment way has no flat usable passband")


def _calibrated_points(capture: dict) -> tuple[list[list[float]], float]:
    """Reconstruct absolute calibrated points of one way capture."""
    analysis = capture.get("analysis") or {}
    normalized_by = analysis.get("normalized_by_db")
    if not isinstance(normalized_by, (int, float)) or not math.isfinite(float(normalized_by)):
        raise ValueError("Alignment capture carries no calibration reference")
    points = None
    for key in ("review_points", "trusted_points"):
        candidate = analysis.get(key)
        if isinstance(candidate, list) and len(candidate) >= 3:
            points = candidate
            if key == "review_points":
                break
    if not isinstance(points, list) or len(points) < 3:
        raise ValueError("Alignment capture carries no usable response points")
    calibrated = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        frequency, level = float(point[0]), float(point[1])
        if not math.isfinite(frequency) or frequency <= 0 or not math.isfinite(level):
            continue
        calibrated.append([frequency, level + float(normalized_by)])
    if len(calibrated) < 3:
        raise ValueError("Alignment capture points are not usable")
    ordered = sorted(calibrated, key=lambda item: item[0])
    for first, second in zip(ordered, ordered[1:]):
        if second[0] <= first[0]:
            raise ValueError("Alignment capture frequencies must increase")
    return ordered, float(normalized_by)


def _crossover_correction_db(processing: dict | None, frequency_hz: float,
                               *, sample_rate_hz: int | None) -> float:
    """Return the known crossover attenuation at one frequency (negative dB).

    The measured way response includes its own crossover shape; adding back
    this attenuation yields the driver level, so narrow mids whose overlap
    never reaches flat still estimate without bias. Without processing info
    the correction is 0 dB.
    """
    if processing is None or sample_rate_hz is None:
        return 0.0
    specs = []
    for kind in ("highpass", "lowpass"):
        spec = processing.get(kind)
        if spec is None:
            continue
        full = dict(spec)
        full["kind"] = kind
        specs.append(full)
    if not specs:
        return 0.0
    total = 1.0 + 0.0j
    for spec in specs:
        for b0, b1, b2, _, a1, a2 in design_crossover(spec, sample_rate_hz):
            total *= crossover_response([(b0, b1, b2, 1.0, a1, a2)], frequency_hz, sample_rate_hz)
    return 20.0 * math.log10(max(abs(total), 1e-12))


def octave_smooth(values: list[tuple[float, float]], *, octaves: float = SMOOTHING_OCTAVES) -> list[float]:
    """Moving-median smooth levels in log frequency (robust to narrow peaks).

    Shared by the per-way level estimate and the shared verification take.
    """
    half = 2.0 ** (octaves / 2.0)
    smoothed = []
    for frequency, _ in values:
        window = [level for other_freq, level in values
                  if frequency / half <= other_freq <= frequency * half]
        smoothed.append(float(statistics.median(window)) if window else 0.0)
    return smoothed


def estimate_way_level(capture: dict, passband: tuple[float, float],
                       *, processing: dict | None = None,
                       sample_rate_hz: int | None = None) -> dict:
    """Estimate one way's robust passband level (median, never single-point).

    Uses the median of 1-octave smoothed calibrated points inside the way's
    usable passband, corrected for the known crossover shape, with MAD as
    stability gate. Total energy is never compared across differently wide
    ways: only the robust level per unit bandwidth (median dB) enters the
    gain proposal.
    """
    low_hz, high_hz = passband
    _finite(low_hz, "passband low")
    _finite(high_hz, "passband high")
    if not high_hz > low_hz:
        raise ValueError("Alignment passband must increase")
    calibrated, _ = _calibrated_points(capture)
    inside = []
    for frequency, level in calibrated:
        if low_hz <= frequency <= high_hz:
            correction = _crossover_correction_db(processing, frequency, sample_rate_hz=sample_rate_hz)
            inside.append((frequency, level - correction))
    if len(inside) < MIN_PASSBAND_POINTS:
        raise ValueError(
            f"Alignment way has {len(inside)} passband points; {MIN_PASSBAND_POINTS} required")
    span_octaves = math.log2(high_hz / low_hz)
    if span_octaves < MIN_PASSBAND_OCTAVES:
        raise ValueError("Alignment passband is too narrow for a robust level")
    smoothed = octave_smooth(inside)
    level_db = float(statistics.median(smoothed))
    mad_db = float(statistics.median(abs(value - level_db) for value in smoothed))
    if not math.isfinite(level_db) or not math.isfinite(mad_db):
        raise ValueError("Alignment way level is not finite")
    if mad_db > MAX_PASSBAND_MAD_DB:
        raise ValueError(f"Alignment way passband is unstable (MAD {mad_db:.2f} dB)")
    return {"level_db": round(level_db, 3), "mad_db": round(mad_db, 3),
            "point_count": len(inside), "coverage_octaves": round(span_octaves, 3),
            "passband_hz": [round(low_hz, 3), round(high_hz, 3)]}


def propose_way_gains(levels_db: dict[str, float]) -> dict[str, float]:
    """Propose start-relative gain corrections equalizing one side's ways.

    The target is the robust median of the measured way levels, so no single
    way dominates. Corrections are bounded; implausible raw values fail
    closed instead of being clamped silently.
    """
    if not isinstance(levels_db, dict) or len(levels_db) not in (2, 3, 4):
        raise ValueError("Alignment gain needs 2-4 way levels of one side")
    for role, level in levels_db.items():
        _finite(level, f"way level {role}")
    target = float(statistics.median(levels_db.values()))
    corrections = {}
    for role, level in levels_db.items():
        correction = target - float(level)
        if not math.isfinite(correction) or abs(correction) > MAX_WAY_GAIN_DB:
            raise ValueError(f"Alignment gain for {role} is implausible ({correction:.2f} dB)")
        corrections[role] = round(correction, 2)
    return corrections


def verify_gain_spread(levels_db: dict[str, float],
                       *, max_spread_db: float = MAX_VERIFIED_GAIN_SPREAD_DB) -> dict:
    """Verify one side's way levels agree within the spread tolerance."""
    if type(max_spread_db) not in (int, float) or not math.isfinite(max_spread_db) or not 0 < max_spread_db < 24:
        raise ValueError("Alignment gain spread must be a positive dB tolerance")
    if not isinstance(levels_db, dict) or len(levels_db) not in (2, 3, 4):
        raise ValueError("Alignment gain verification needs 2-4 way levels")
    for role, level in levels_db.items():
        _finite(level, f"confirmation level {role}")
    spread = max(levels_db.values()) - min(levels_db.values())
    reasons = []
    if spread > max_spread_db:
        reasons.append(f"gain spread {spread:.3f} dB exceeds {max_spread_db:.3f} dB: ways differ in level")
    return {"confirmed": not reasons, "reasons": reasons,
            "spread_db": round(float(spread), 3), "tolerance_db": float(max_spread_db),
            "levels_db": {role: round(float(level), 3) for role, level in levels_db.items()}}


def neutralized_candidate_state(state: dict) -> dict:
    """Return a detached copy stating the alignment rendering contract.

    The persisted document itself is never neutralized here; the plan
    compiler renders Global/area banks bypassed via neutralize_banks. This
    helper documents the shared principle: Delay/Gain are physical base
    tuning measured through the active crossover, PEQ/convolver apply
    afterwards.
    """
    return copy.deepcopy(state)
