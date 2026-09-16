# SPDX-License-Identifier: AGPL-3.0-only
"""Analog-prototype crossover design with prewarped bilinear transform.

Butterworth poles are analytic. Linkwitz-Riley cascades two Butterworth
halves at the same cutoff, so its cutoff sits at -6 dB. Bessel poles are
the roots of the reverse Bessel polynomial; the prototype is renormalized
so its magnitude cutoff (-3 dB) defines the frequency, which the docstring
and UI must label as magnitude-normalized rather than a group-delay cutoff.
"""

from __future__ import annotations

import cmath
import math

import numpy as np

from audio.output_state import FILTER_SLOPES
from audio.samplerate.constants import FXROUTE_MAX_PROCESSING_RATE

_KINDS = ("lowpass", "highpass")


def _validate_spec(spec: object) -> dict:
    if not isinstance(spec, dict) or set(spec) != {"kind", "family", "slope_db_oct", "frequency_hz"}:
        raise ValueError("Crossover filter requires kind, family, slope_db_oct, frequency_hz")
    if spec["kind"] not in _KINDS:
        raise ValueError("Crossover kind must be lowpass or highpass")
    if not isinstance(spec["family"], str) or spec["family"] not in FILTER_SLOPES:
        raise ValueError("Unsupported crossover filter family")
    if type(spec["slope_db_oct"]) is not int or spec["slope_db_oct"] not in FILTER_SLOPES[spec["family"]]:
        raise ValueError("Unsupported crossover family/slope combination")
    frequency = spec["frequency_hz"]
    if type(frequency) not in (float, int) or not math.isfinite(frequency) or not 20 <= frequency <= 20000:
        raise ValueError("Crossover frequency must be finite and between 20 and 20000")
    return spec


def _butterworth_poles(order: int) -> list[complex]:
    return [cmath.exp(1j * math.pi * (2 * k + order + 1) / (2 * order)) for k in range(order)]


def _bessel_coefficients(order: int) -> list[float]:
    return [math.factorial(2 * order - k) / (2 ** (order - k) * math.factorial(k) * math.factorial(order - k))
            for k in range(order + 1)]


def _bessel_poles(order: int) -> list[complex]:
    ascending = _bessel_coefficients(order)
    roots = np.roots(list(reversed(ascending)))
    poles = [complex(root) for root in roots]
    if any(pole.real >= 0.0 for pole in poles):
        raise ValueError("Bessel prototype has no stable pole set")
    return poles


def _prototype_gain(coefficients: list[float], omega: float) -> float:
    response = sum(coef * (1j * omega) ** power for power, coef in enumerate(coefficients))
    return abs(coefficients[0] / response)


def _bessel_3db_omega(order: int) -> float:
    coefficients = _bessel_coefficients(order)
    low, high = 1e-3, 1e3
    target = 1.0 / math.sqrt(2.0)
    for _ in range(100):
        middle = math.sqrt(low * high)
        if _prototype_gain(coefficients, middle) > target:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


def _sections_from_poles(poles: list[complex]) -> list[tuple]:
    """Group prototype poles into (w0, Q) biquad or (w0,) first-order sections."""
    remaining = sorted(poles, key=lambda pole: (round(pole.real, 9), round(abs(pole.imag), 9)))
    sections = []
    while remaining:
        pole = remaining.pop(0)
        if abs(pole.imag) < 1e-12:
            sections.append((abs(pole.real),))
            continue
        partner = min(range(len(remaining)),
                      key=lambda index: abs(remaining[index] - pole.conjugate()))
        if abs(remaining[partner] - pole.conjugate()) > 1e-6 * max(1.0, abs(pole)):
            raise ValueError("Prototype pole has no conjugate partner")
        remaining.pop(partner)
        w0 = abs(pole)
        sections.append((w0, w0 / (2.0 * abs(pole.real))))
    return sections


def _analog_coefficients(kind: str, section: tuple, warped: float, scale: float) -> tuple:
    """Build analog (b0,b1,b2,a0,a1,a2) for one prototype section at cutoff ``warped``.

    ``scale`` normalizes the prototype so its magnitude cutoff sits at 1 rad/s
    (1.0 for Butterworth, 1/w3db for Bessel). Lowpass scales the pole radius up
    (``w0 = w0_proto * scale * warped``); highpass maps it reciprocally
    (``w0 = warped / (w0_proto * scale)``) via the LP->HP substitution. The two
    coincide for Butterworth (unit-circle poles) but differ for Bessel, where
    reusing the lowpass radius would move the highpass -3 dB point.
    """
    if len(section) == 1:
        (w0_proto,) = section
        w0 = w0_proto * scale * warped if kind == "lowpass" else warped / (w0_proto * scale)
        if kind == "lowpass":
            return (w0, 0.0, 0.0, w0, 1.0, 0.0)
        return (0.0, 1.0, 0.0, w0, 1.0, 0.0)
    w0_proto, quality = section
    w0 = w0_proto * scale * warped if kind == "lowpass" else warped / (w0_proto * scale)
    if kind == "lowpass":
        return (w0 * w0, 0.0, 0.0, w0 * w0, w0 / quality, 1.0)
    return (0.0, 0.0, 1.0, w0 * w0, w0 / quality, 1.0)


def _bilinear(coefficients: tuple, rate: int) -> list[float]:
    b0, b1, b2, a0, a1, a2 = coefficients
    c = 2.0 * rate
    b0d = b0 + b1 * c + b2 * c * c
    b1d = 2.0 * b0 - 2.0 * b2 * c * c
    b2d = b0 - b1 * c + b2 * c * c
    a0d = a0 + a1 * c + a2 * c * c
    a1d = 2.0 * a0 - 2.0 * a2 * c * c
    a2d = a0 - a1 * c + a2 * c * c
    return [b0d / a0d, b1d / a0d, b2d / a0d, 1.0, a1d / a0d, a2d / a0d]


def design_crossover(spec: dict, sample_rate_hz: int) -> list[list[float]]:
    """Design normalized ``[b0, b1, b2, 1, a1, a2]`` second-order sections."""
    spec = _validate_spec(spec)
    if type(sample_rate_hz) is not int or not 0 < sample_rate_hz <= FXROUTE_MAX_PROCESSING_RATE:
        raise ValueError("Sample rate must be a positive integer up to 384000")
    if not spec["frequency_hz"] < sample_rate_hz / 2:
        raise ValueError("Crossover frequency must be below Nyquist")
    order = spec["slope_db_oct"] // 6
    if spec["family"] == "bessel":
        sections = _sections_from_poles(_bessel_poles(order))
        scale = 1.0 / _bessel_3db_omega(order)
    else:
        half = order // 2 if spec["family"] == "linkwitz-riley" else order
        sections = _sections_from_poles(_butterworth_poles(half))
        if spec["family"] == "linkwitz-riley":
            sections = list(sections) + list(sections)
        scale = 1.0
    warped = 2.0 * sample_rate_hz * math.tan(math.pi * spec["frequency_hz"] / sample_rate_hz)
    return [_bilinear(_analog_coefficients(spec["kind"], section, warped, scale), sample_rate_hz)
            for section in sections]


def crossover_response(sections: list, frequency_hz: float, sample_rate_hz: int) -> complex:
    z = cmath.exp(2j * math.pi * frequency_hz / sample_rate_hz)
    total = 1.0 + 0.0j
    for b0, b1, b2, _, a1, a2 in sections:
        total *= (b0 + b1 / z + b2 / z / z) / (1.0 + a1 / z + a2 / z / z)
    return total


def crossover_magnitude_db(sections: list, frequency_hz: float, sample_rate_hz: int) -> float:
    return 20.0 * math.log10(abs(crossover_response(sections, frequency_hz, sample_rate_hz)))
