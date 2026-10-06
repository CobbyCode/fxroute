# SPDX-License-Identifier: AGPL-3.0-only
"""Speaker polarity from the sign of the measured impulse response at the direct arrival.

The deconvolved microphone impulse response is signed and keeps the polarity of
the whole active chain (output polarity, amplifier, speaker, microphone). At the
direct arrival, a positive stimulus giving a positive pressure peak means the
speaker plays correct; a negative peak means it plays reversed. The microphone
itself is assumed to be polarity correct.

The sign is read from the 100 Hz - 1 kHz range: the deconvolution noise rises
with frequency, so this range carries the clearest peak, and it is the range in
which a speaker sums with its neighbours and the subwoofers. A way that plays
only above that range has no verdict.

A verdict needs three things: the analyzer found a direct arrival (a capture the
sweep never reached has none, and its largest sample is only noise); the peak
stands clear of the late noise floor; and it stands clear of the opposite-sign
lobes next to it (a nearly antisymmetric wavelet, e.g. a way dominated by
crossover or panel phase, has no meaningful peak polarity).

Pure analysis: no hardware, no runtime.
"""

from __future__ import annotations

import math

import numpy as np

CORRECT = "correct"
REVERSED = "reversed"
INCONCLUSIVE = "inconclusive"
VERDICTS = (CORRECT, REVERSED, INCONCLUSIVE)
POLARITY_BAND_HZ = (100.0, 1000.0)
# The band edges roll off over this factor, zero phase, so the peak keeps its sign.
BAND_EDGE_RATIO = 1.4
# The band-limited peak sits within this span around the broadband arrival.
SEARCH_BEFORE_MS = 1.0
SEARCH_AFTER_MS = 4.0
# Opposite-sign samples this close to the peak compete with it for the verdict.
NEIGHBOUR_WINDOW_MS = 2.0
# The peak must exceed the strongest opposite-sign neighbour by this factor.
MIN_DOMINANCE_RATIO = 1.5
# The peak must exceed the late-tail noise by this much (dB).
MIN_SNR_DB = 20.0
# The noise floor is read after the room's early decay.
NOISE_TAIL_START_MS = 100.0
MIN_NOISE_TAIL_SECONDS = 0.5


def analyze_ir_polarity(impulse_response: np.ndarray, sample_rate: int, arrival_index: int | None) -> dict:
    """Judge the polarity of one measured impulse response.

    ``arrival_index`` is the direct arrival the analyzer found, or None when it
    found none. Returns ``verdict`` (correct / reversed / inconclusive),
    ``peak_sign``, ``dominance_db`` (peak over the strongest opposite-sign
    neighbour), ``snr_db`` (peak over the late noise floor) and, for an
    inconclusive verdict, its ``reason``.
    """
    ir = np.asarray(impulse_response, dtype=np.float64)
    if ir.size == 0 or not np.all(np.isfinite(ir)) or not np.any(ir):
        return _result(INCONCLUSIVE, 0, 0.0, 0.0, "no_signal")
    if arrival_index is None or not 0 <= arrival_index < ir.size:
        return _result(INCONCLUSIVE, 0, 0.0, 0.0, "no_arrival")
    band = _band_limit(ir, sample_rate)

    def samples(milliseconds: float) -> int:
        return max(1, int(round(sample_rate * milliseconds / 1000.0)))

    start = max(0, arrival_index - samples(SEARCH_BEFORE_MS))
    end = min(band.size, arrival_index + samples(SEARCH_AFTER_MS) + 1)
    peak_index = start + int(np.argmax(np.abs(band[start:end])))
    peak = float(abs(band[peak_index]))
    if peak <= 1e-18:
        return _result(INCONCLUSIVE, 0, 0.0, 0.0, "no_signal")
    peak_sign = 1 if band[peak_index] > 0 else -1

    reach = samples(NEIGHBOUR_WINDOW_MS)
    neighbours = band[max(0, peak_index - reach):peak_index + reach + 1] * -peak_sign
    opposite = float(np.max(neighbours))
    dominance_db = 20.0 * math.log10(peak / opposite) if opposite > 0 else 200.0

    tail = np.abs(band[arrival_index + samples(NOISE_TAIL_START_MS):])
    if tail.size < sample_rate * MIN_NOISE_TAIL_SECONDS:
        return _result(INCONCLUSIVE, peak_sign, dominance_db, 0.0, "weak_signal")
    noise = float(np.median(tail))
    snr_db = 20.0 * math.log10(peak / noise) if noise > 0 else 200.0

    if snr_db < MIN_SNR_DB:
        return _result(INCONCLUSIVE, peak_sign, dominance_db, snr_db, "weak_signal")
    if dominance_db < 20.0 * math.log10(MIN_DOMINANCE_RATIO):
        return _result(INCONCLUSIVE, peak_sign, dominance_db, snr_db, "mixed_lobes")
    return _result(CORRECT if peak_sign > 0 else REVERSED, peak_sign, dominance_db, snr_db, "")


def _band_limit(ir: np.ndarray, sample_rate: int) -> np.ndarray:
    """Zero-phase band-pass of the polarity band with cosine-tapered edges."""
    low, high = POLARITY_BAND_HZ
    frequencies = np.fft.rfftfreq(ir.size, 1.0 / sample_rate)

    def ramp(start: float, stop: float) -> np.ndarray:
        return np.clip((frequencies - start) / (stop - start), 0.0, 1.0)

    gain = ramp(low / BAND_EDGE_RATIO, low) * (1.0 - ramp(high, high * BAND_EDGE_RATIO))
    return np.fft.irfft(np.fft.rfft(ir) * gain, n=ir.size)


def _result(verdict: str, peak_sign: int, dominance_db: float, snr_db: float, reason: str) -> dict:
    return {"verdict": verdict, "peak_sign": peak_sign, "dominance_db": round(dominance_db, 2),
            "snr_db": round(snr_db, 2), "reason": reason}
