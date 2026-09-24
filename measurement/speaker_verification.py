# SPDX-License-Identifier: AGPL-3.0-only
"""One shared verification take: a speaker side's way arrivals on one clock.

A Speaker Align trial applies relative way delays, and judging that apply by
re-measuring every way alone is structurally blind to it: each way's own
electrical reference is tapped after the way's delay, so a staged delay moves
the microphone arrival and its reference together and their difference never
changes.

This module judges the apply from one shared take instead. Every way of one
side plays the same sweep at once and the take's single microphone impulse
response is split into the ways' own bands on that take's one capture time
base. Each band is recovered with a matched copy of that way's crossover
response (``conj(H)``, sharpened by ``|H| ** (power - 1)``), so the crossover's
own phase is removed from the estimate instead of adding a second copy of it,
and the estimate peaks exactly on that way's real arrival. The matched filter is
additionally weighted by the way's own share of the take's power, because two
ways of one crossover overlap by an octave in which each one is as loud as the
other: without that weight a louder neighbour decides the arrival. The residual
is the spread of those arrivals only; the electrical reference keeps its
established job as the take's quality and provenance evidence, never as a
per-way zero.

``way_isolation_db`` states how far the way's own arrival stands above what the
neighbouring ways put into the same isolated band, so an ambiguous take is
recognizable instead of silently confirming. Levels are passband medians of the
same isolated bands, reported relative to the loudest way of the take: only
their spread is a verification criterion.

Numbers in, numbers out: no files, no hardware, no store, no global state.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Sequence

import numpy as np

from dsp.crossover import design_crossover
from measurement.alignment_backend import (
    MAX_PASSBAND_MAD_DB,
    MIN_PASSBAND_OCTAVES,
    MIN_PASSBAND_POINTS,
    octave_smooth,
    way_crossover_specs,
    way_passband,
)

__all__ = [
    "ARRIVAL_LEADING_MARGIN_DB",
    "ARRIVAL_LEADING_NULL_DB",
    "ARRIVAL_LOBE_MAX_MS",
    "ARRIVAL_SMOOTHING_SECONDS",
    "ARRIVAL_THRESHOLD_RELATIVE",
    "ISOLATION_POWER",
    "MIN_WAY_ISOLATION_DB",
    "band_arrival",
    "band_level",
    "side_confirmation",
    "way_band_impulse_response",
]

# The matched filter carries one extra power of the way's own crossover
# magnitude: isolation against the neighbouring way grows from 24 dB/oct to
# 48 dB/oct while the estimate stays a zero-phase one.
ISOLATION_POWER = 2.0
# Onset diagnostics: the first time the short-window band power reaches this
# fraction of the band's own peak power. The arrival itself is the band's
# strongest energy, which the matched filter places on the real arrival.
ARRIVAL_THRESHOLD_RELATIVE = 0.10
ARRIVAL_SMOOTHING_SECONDS = 0.0002
# Two arrivals this close are the same arrival: nothing to separate then.
ARRIVAL_LOBE_MAX_MS = 50.0
# A way's band can carry two lobes within a fraction of a decibel of each other
# and separated by a null: every real take measured so far put the low way's
# second lobe 0.03 to 0.37 dB from the first. The strongest energy then flips
# between those two lobes from take to take, and a planned or residual delay
# with it, while the leading lobe is the same physical arrival every time. A
# lobe pair inside this margin therefore resolves to the earlier one. The margin
# stays tight on purpose: the band's own ringing (2.7 dB down in synthetic
# takes) and the way's next real structure (5.2 dB or more down in real ones)
# must not be mistaken for a near tie.
ARRIVAL_LEADING_MARGIN_DB = 1.0
# Two lobes only count as two arrivals when a null this deep separates them: a
# mere shoulder belongs to the same lobe.
ARRIVAL_LEADING_NULL_DB = 6.0
# A way's own arrival must stand this far above what the side's other ways
# leave in the same isolated band. Two ways of one crossover overlap by an
# octave, so a way that is far quieter than its neighbour cannot be located in
# the shared take: its band is then the neighbour's leak and the take must not
# confirm timing from it.
MIN_WAY_ISOLATION_DB = 10.0
# Band level grid: 240 log-spaced points, the same resolution the crossover
# passband scan uses.
BAND_LEVEL_POINTS = 240
BAND_LEVEL_LOW_HZ = 20.0
BAND_LEVEL_HIGH_HZ = 18000.0


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Speaker verification {label} must be finite")
    return float(value)


def _crossover_sections(processing: object, sample_rate_hz: int) -> list[list[float]]:
    """Sections of every crossover filter the way renders (``way_crossover_specs``).

    The matched filter only removes the phase it models: a rendered filter
    missing here, such as the bass-management Main high-pass, stays in the
    isolated band and can make the way's rebound lobe its strongest energy.
    """
    if not isinstance(processing, dict):
        raise ValueError("Speaker verification way carries no processing")
    try:
        specs = way_crossover_specs(processing)
    except ValueError as exc:
        raise ValueError(f"Speaker verification crossover filter is malformed: {exc}") from exc
    sections: list[list[float]] = []
    for spec in specs:
        sections.extend(design_crossover(spec, sample_rate_hz))
    if not sections:
        raise ValueError("Speaker verification way has no crossover filters")
    return sections


def _sections_response(sections: Sequence[Sequence[float]], frequencies: np.ndarray,
                       sample_rate_hz: int) -> np.ndarray:
    z = np.exp(-2j * np.pi * frequencies / sample_rate_hz)
    response = np.ones_like(frequencies, dtype=complex)
    for b0, b1, b2, _, a1, a2 in sections:
        response *= (b0 + b1 * z + b2 * z * z) / (1.0 + a1 * z + a2 * z * z)
    return response


def _own_share(own_power: np.ndarray, foreign_power: np.ndarray) -> np.ndarray:
    """Share of the take's power this way owns at each frequency.

    In the crossover overlap both ways are equally loud, and nothing in the
    take separates them there; weighting by the owned share keeps the estimate
    on the energy the way actually contributes. A real nonnegative weight never
    moves a matched filter's peak off the real arrival.
    """
    total = own_power + foreign_power
    return np.divide(own_power, total, out=np.zeros_like(own_power), where=total > 0.0)


def way_band_impulse_response(
    impulse_response: object,
    processing: object,
    *,
    sample_rate_hz: int,
    isolation_power: float = ISOLATION_POWER,
    foreign: Sequence[object] = (),
) -> np.ndarray:
    """Isolate one way's band of a shared take with its own crossover filter.

    The filter is the conjugate of the way's crossover response, so the
    isolated band keeps the pure arrival and the driver/room structure instead
    of the crossover phase the real rendering already applied. ``isolation_power``
    of 1 is the plain matched filter; higher values add that many extra
    magnitudes of the same (zero-phase) crossover shape for steeper isolation
    from the neighbouring way. ``foreign`` holds the processing of the side's
    other ways; their power decides this way's share of the take, so the shared
    crossover overlap cannot pull the estimate onto a neighbour's arrival.
    Nothing here adds group delay of its own.
    """
    ir = np.asarray(impulse_response, dtype=np.float64)
    if ir.ndim != 1 or ir.size < 64 or not np.all(np.isfinite(ir)) or not np.any(ir):
        raise ValueError("Speaker verification take IR must be finite, non-silent and full resolution")
    if type(sample_rate_hz) is not int or sample_rate_hz <= 0:
        raise ValueError("Speaker verification sample rate must be a positive integer")
    power = _finite(isolation_power, "isolation power")
    if not 1.0 <= power <= 6.0:
        raise ValueError("Speaker verification isolation power must be between 1 and 6")
    sections = _crossover_sections(processing, sample_rate_hz)
    if isinstance(foreign, (dict, str)) or not isinstance(foreign, Sequence):
        raise ValueError("Speaker verification foreign ways must be a sequence of ways")
    foreign_sections = [_crossover_sections(way, sample_rate_hz) for way in foreign]
    # Linear convolution of two full-length responses: the isolated band stays
    # on the same sample origin as the take's IR.
    size = 1 << (2 * ir.size - 1).bit_length()
    spectrum = np.fft.rfft(ir, n=size)
    frequencies = np.fft.rfftfreq(size, 1.0 / sample_rate_hz)
    response = _sections_response(sections, frequencies, sample_rate_hz)
    magnitude = np.abs(response)
    weight = np.conj(response) * magnitude ** (power - 1.0)
    if foreign_sections:
        foreign_power = np.zeros_like(magnitude)
        for sections_other in foreign_sections:
            foreign_power = foreign_power + np.square(
                np.abs(_sections_response(sections_other, frequencies, sample_rate_hz)))
        weight = weight * _own_share(np.square(magnitude), foreign_power)
    isolated = np.fft.irfft(spectrum * weight, n=size)
    return np.array(isolated[: ir.size], dtype=np.float64)


def _leading_lobe(energy: np.ndarray, peak_index: int, reach: int) -> int:
    """The earlier of two comparable lobes, or the peak when there is only one.

    Walks back from the band's strongest energy and returns the earliest local
    maximum that is within ``ARRIVAL_LEADING_MARGIN_DB`` of it *and* separated
    from it by a null of at least ``ARRIVAL_LEADING_NULL_DB``. A near-tie then
    resolves to the leading arrival instead of to whichever lobe the take's
    noise made marginally louder; a band whose leading structure is far quieter
    than its peak keeps that peak.
    """
    margin = 10.0 ** (ARRIVAL_LEADING_MARGIN_DB / 10.0)
    null_ratio = 10.0 ** (-ARRIVAL_LEADING_NULL_DB / 10.0)
    peak_energy = float(energy[peak_index])
    leading = peak_index
    start = max(1, peak_index - reach)
    for index in range(start, peak_index):
        if float(energy[index]) * margin < peak_energy:
            continue
        if not (energy[index] >= energy[index - 1] and energy[index] >= energy[index + 1]):
            continue
        between = energy[index + 1: peak_index]
        if between.size == 0 or float(between.min()) > null_ratio * float(energy[index]):
            continue
        leading = index
        break
    return leading


def band_arrival(
    band: object,
    *,
    sample_rate_hz: int,
    threshold_relative: float = ARRIVAL_THRESHOLD_RELATIVE,
    smoothing_seconds: float = ARRIVAL_SMOOTHING_SECONDS,
) -> dict:
    """Return the arrival of one isolated band on the take's time base.

    The band is zero-phase around its way's real arrival, so the arrival is the
    strongest energy of the isolated band, resolved to the earlier lobe of a
    near-tie (see ``_leading_lobe``) so one arrival cannot flip between two
    lobes that are within a fraction of a decibel. The first significant energy
    (``onset_index``) is reported next to it for diagnosis: it can sit closer to
    a leading reflection or a leaked neighbour lobe, which is exactly why it
    does not decide the arrival. ``lobe_samples`` is how long the strongest
    energy stays above the same threshold, i.e. the time resolution of this
    arrival: two arrivals closer than that are the same event.
    """
    values = np.abs(np.asarray(band, dtype=np.float64))
    if values.ndim != 1 or values.size < 64 or not np.all(np.isfinite(values)):
        raise ValueError("Speaker verification band must be finite and full resolution")
    if type(sample_rate_hz) is not int or sample_rate_hz <= 0:
        raise ValueError("Speaker verification sample rate must be a positive integer")
    threshold = _finite(threshold_relative, "arrival threshold")
    if not 0.0 < threshold < 1.0:
        raise ValueError("Speaker verification arrival threshold must be between 0 and 1")
    window = max(1, int(round(sample_rate_hz * _finite(smoothing_seconds, "arrival smoothing"))))
    energy = np.square(values)
    power = np.convolve(energy, np.full(window, 1.0 / window), mode="same")
    peak_index = int(np.argmax(energy))
    if not float(energy[peak_index]) > 0.0:
        raise ValueError("Speaker verification band is silent")
    peak_energy = float(energy[peak_index])
    crossings = np.flatnonzero(power[: peak_index + 1] >= threshold * float(power[peak_index]))
    reach = max(1, int(round(sample_rate_hz * ARRIVAL_LOBE_MAX_MS / 1000.0)))
    stop = min(values.size, peak_index + reach)
    trailing = np.flatnonzero(energy[peak_index:stop] < threshold * peak_energy)
    return {
        "arrival_index": _leading_lobe(energy, peak_index, reach),
        "onset_index": int(crossings[0]) if crossings.size else peak_index,
        "lobe_samples": int(trailing[0]) if trailing.size else stop - peak_index,
    }


def band_level(band: object, passband: Sequence[float], *, sample_rate_hz: int) -> dict:
    """Robust passband level of one isolated band (median, never a single point).

    Mirrors the shared alignment level estimate: log-spaced points inside the
    way's own flat passband, one-octave smoothing, median and MAD stability
    gate. The result is only meaningful relative to the take's other ways.
    """
    values = np.asarray(band, dtype=np.float64)
    if values.ndim != 1 or values.size < 64 or not np.all(np.isfinite(values)):
        raise ValueError("Speaker verification band must be finite and full resolution")
    if not isinstance(passband, (list, tuple)) or len(passband) != 2:
        raise ValueError("Speaker verification passband must be a pair of frequencies")
    low_hz = _finite(passband[0], "passband low")
    high_hz = _finite(passband[1], "passband high")
    if not high_hz > low_hz:
        raise ValueError("Speaker verification passband must increase")
    if type(sample_rate_hz) is not int or sample_rate_hz <= 0:
        raise ValueError("Speaker verification sample rate must be a positive integer")
    grid_high = min(BAND_LEVEL_HIGH_HZ, sample_rate_hz * 0.45)
    grid = BAND_LEVEL_LOW_HZ * (grid_high / BAND_LEVEL_LOW_HZ) ** np.linspace(
        0.0, 1.0, BAND_LEVEL_POINTS)
    size = 1 << max(0, values.size - 1).bit_length()
    spectrum = np.abs(np.fft.rfft(values, n=size))
    bin_hz = sample_rate_hz / size
    levels_db = 20.0 * np.log10(np.maximum(spectrum, 1e-12))
    inside = []
    for frequency in grid:
        if low_hz <= float(frequency) <= high_hz:
            inside.append((float(frequency), float(np.interp(frequency, np.arange(size // 2 + 1) * bin_hz,
                                                             levels_db))))
    if len(inside) < MIN_PASSBAND_POINTS:
        raise ValueError(
            f"Speaker verification way has {len(inside)} passband points; {MIN_PASSBAND_POINTS} required")
    span_octaves = math.log2(high_hz / low_hz)
    if span_octaves < MIN_PASSBAND_OCTAVES:
        raise ValueError("Speaker verification passband is too narrow for a robust level")
    smoothed = octave_smooth(inside)
    level_db = float(np.median(smoothed))
    mad_db = float(np.median([abs(value - level_db) for value in smoothed]))
    if not math.isfinite(level_db) or not math.isfinite(mad_db):
        raise ValueError("Speaker verification way level is not finite")
    if mad_db > MAX_PASSBAND_MAD_DB:
        raise ValueError(f"Speaker verification way passband is unstable (MAD {mad_db:.2f} dB)")
    return {"level_db": round(level_db, 3), "mad_db": round(mad_db, 3),
            "point_count": len(inside), "coverage_octaves": round(span_octaves, 3),
            "passband_hz": [round(low_hz, 3), round(high_hz, 3)]}


def side_confirmation(
    *,
    impulse_response: object,
    processing: dict,
    roles: Sequence[str],
    sample_rate_hz: int,
    start_revision: int,
    processing_fingerprint: str,
    isolation_power: float = ISOLATION_POWER,
) -> dict:
    """Judge one shared take and return the confirmation document.

    ``arrival_ms`` holds every way's acoustic arrival on the take's own capture
    time base, relative to the earliest way of the take (so the values read as
    relative arrivals while their spread is exactly the residual the gate
    checks).    ``way_isolation_db`` holds, per way, the margin of its own arrival above
    what the side's other ways leave in the same isolated band, or ``None``
    when no other way arrives outside this way's own lobe.
    ``way_levels_db`` holds the same take's isolated passband levels relative to
    the loudest way; only their spread is a criterion, and the raw medians stay
    in ``bands`` for diagnosis.
    """
    if type(start_revision) is not int:
        raise ValueError("Speaker verification confirmation needs a start revision")
    if not isinstance(processing_fingerprint, str) or not processing_fingerprint:
        raise ValueError("Speaker verification confirmation needs a processing fingerprint")
    ordered_roles = [str(role) for role in roles]
    if len(ordered_roles) < 2 or len(set(ordered_roles)) != len(ordered_roles):
        raise ValueError("Speaker verification needs at least two distinct speaker ways")
    if not isinstance(processing, dict):
        raise ValueError("Speaker verification needs the side's processing")
    for role in ordered_roles:
        if not isinstance(processing.get(role), dict):
            raise ValueError(f"Speaker verification has no processing for {role}")
    ir = np.asarray(impulse_response, dtype=np.float64)
    bands: dict[str, dict] = {}
    energy: dict[str, np.ndarray] = {}
    for role in ordered_roles:
        others = [processing[other] for other in ordered_roles if other != role]
        isolated = way_band_impulse_response(
            ir, processing[role], sample_rate_hz=sample_rate_hz,
            isolation_power=isolation_power, foreign=others)
        arrival = band_arrival(isolated, sample_rate_hz=sample_rate_hz)
        level = band_level(isolated, way_passband(processing[role], sample_rate_hz=sample_rate_hz),
                           sample_rate_hz=sample_rate_hz)
        bands[role] = {**arrival, **level}
        energy[role] = np.square(np.abs(isolated))
    earliest = min(band["arrival_index"] for band in bands.values())
    arrivals_ms = {
        role: round((band["arrival_index"] - earliest) * 1000.0 / sample_rate_hz, 6)
        for role, band in bands.items()
    }
    # Isolation is only meaningful against a *different* arrival: two arrivals
    # inside this way's own lobe are one event, which is the state the trial
    # wants anyway, so there is nothing to isolate. The lobe width is the
    # resolution of the estimate itself, never a fixed distance.
    isolation_db: dict[str, float | None] = {}
    for role, band in bands.items():
        own_index = band["arrival_index"]
        coincidence = int(band["lobe_samples"])
        distinct = [float(energy[role][other["arrival_index"]])
                    for other_role, other in bands.items()
                    if other_role != role
                    and abs(other["arrival_index"] - own_index) > coincidence]
        own = float(energy[role][own_index])
        isolation_db[role] = (round(10.0 * math.log10(max(own, 1e-300) / max(max(distinct), 1e-300)), 3)
                              if distinct else None)
    loudest = max(band["level_db"] for band in bands.values())
    levels_db = {role: round(band["level_db"] - loudest, 3) for role, band in bands.items()}
    return {
        "start_revision": int(start_revision),
        "processing_fingerprint": processing_fingerprint,
        "arrival_ms": arrivals_ms,
        "way_isolation_db": isolation_db,
        "way_levels_db": levels_db,
        "band_limited": True,
        "arrival_origin": "earliest-way-of-take",
        "level_origin": "loudest-way-of-take",
        "isolation_power": float(isolation_power),
        "bands": copy.deepcopy(bands),
    }
