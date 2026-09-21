"""Synthetic shared speaker takes for the planning and verification suites.

A shared take is one microphone impulse response in which every way of one
speaker side played the same sweep at once, the rest of the plan muted.  The
fixtures below render each way causally -- its own crossover response at its own
arrival -- so a take carries the structure a deconvolved sweep capture produces
and the estimators under test see real band-limited ways on one time base.

``planning_document``/``confirmation_document`` wrap such a take the way the
acquisition adapter does and hand it to the alignment, so a test exercises the
production take gate instead of a hand-built dictionary.
"""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np

from dsp.crossover import design_crossover

RATE = 48000
TAKE_SAMPLES = 32768
ORIGIN_SAMPLES = 8192


def side_processing(alignment: Any) -> dict:
    """The active mode's processing for the frozen state of one alignment."""
    state = alignment._state
    return state["modes"][state["active_mode"]]["processing"]


def side_roles(alignment: Any) -> list[str]:
    return list(alignment.planning_request()["roles"])


def way_response(processing: dict, role: str, *, sample_rate_hz: int = RATE,
                 size: int = TAKE_SAMPLES) -> np.ndarray:
    """Causal frequency response of one way's own crossover filters."""
    frequencies = np.fft.rfftfreq(size, 1.0 / sample_rate_hz)
    z = np.exp(-2j * np.pi * frequencies / sample_rate_hz)
    response = np.ones_like(frequencies, dtype=complex)
    for kind in ("highpass", "lowpass"):
        spec = processing[role].get(kind)
        if spec is None:
            continue
        for b0, b1, b2, _, a1, a2 in design_crossover({**spec, "kind": kind}, sample_rate_hz):
            response *= (b0 + b1 * z + b2 * z * z) / (1.0 + a1 * z + a2 * z * z)
    return response


def shared_take(arrival_ms: dict, *, processing: dict, roles: Sequence[str],
                sample_rate_hz: int = RATE, size: int = TAKE_SAMPLES,
                origin_samples: int = ORIGIN_SAMPLES, gains_db: dict | None = None,
                reflections: Sequence[tuple[float, float]] = ()) -> np.ndarray:
    """One microphone IR holding every way's causal response at its arrival.

    ``arrival_ms`` is each way's total arrival: acoustic geometry plus any
    applied way delay, exactly what the engine renders into one take.
    """
    gains_db = gains_db or {}
    frequencies = np.fft.rfftfreq(size, 1.0 / sample_rate_hz)
    spectrum = np.zeros_like(frequencies, dtype=complex)
    for role in roles:
        delay_samples = int(round(origin_samples + float(arrival_ms[role]) * sample_rate_hz / 1000.0))
        echo = np.ones_like(frequencies, dtype=complex)
        for seconds, gain in reflections:
            echo += gain * np.exp(-2j * np.pi * frequencies * seconds)
        gain = 10.0 ** (float(gains_db.get(role, 0.0)) / 20.0)
        spectrum += gain * way_response(processing, role, sample_rate_hz=sample_rate_hz, size=size) * np.exp(
            -2j * np.pi * frequencies * delay_samples / sample_rate_hz) * echo
    return np.fft.irfft(spectrum, n=size)


def take_document(alignment: Any, request: dict, impulse_response: np.ndarray, *,
                  sample_rate_hz: int = RATE) -> dict:
    """The take shape the acquisition adapter hands to planning/confirmation."""
    return {
        "measurement_target": request["measurement_target"],
        "reference_id": request["reference_id"],
        "microphone_position_id": request["microphone_position_id"],
        "reference_tap": request["reference_tap"],
        "reference_node": "interface:input-2",
        "time_reference": "deconvolved-sweep-origin",
        "impulse_response": impulse_response,
        "analysis": {
            "sample_rate": sample_rate_hz, "peak_dbfs": -12.0,
            "quality_checks": {"status": "pass", "items": []},
            "reference_path": {
                "usable": True, "electrical_reference_used": True,
                "timing_status": "electrical-reference", "stability": "stable",
                "confidence": 0.95, "clipped": False, "peak_dbfs": -9.0,
            },
        },
    }


def planning_document(alignment: Any, arrival_ms: dict, *, sample_rate_hz: int = RATE,
                      **take_options: Any) -> dict:
    """Plan one side from a synthetic shared take at the given way arrivals."""
    request = alignment.planning_request()
    take = take_document(
        alignment, request,
        shared_take(arrival_ms, processing=side_processing(alignment),
                    roles=request["roles"], sample_rate_hz=sample_rate_hz, **take_options),
        sample_rate_hz=sample_rate_hz)
    return alignment.planning(take)


def confirmation_document(alignment: Any, arrival_ms: dict, *, sample_rate_hz: int = RATE,
                          **take_options: Any) -> dict:
    """Confirm one side from a synthetic shared take at the given way arrivals."""
    request = alignment.verification_request()
    take = take_document(
        alignment, request,
        shared_take(arrival_ms, processing=side_processing(alignment),
                    roles=request["roles"], sample_rate_hz=sample_rate_hz, **take_options),
        sample_rate_hz=sample_rate_hz)
    return alignment.confirmation(take)
