# SPDX-License-Identifier: AGPL-3.0-only
"""Speaker polarity from the first, high-frequency pulse of the measured impulse response.

The deconvolved microphone impulse response is signed and keeps the polarity of
the whole active chain (output polarity, amplifier, speaker, microphone). A
positive stimulus giving a positive pressure peak at the direct arrival means
the speaker plays correct; a negative peak means it plays reversed. This is
the usual reading of an impulse response, and the microphone itself is assumed
to be polarity correct.

The peak is read from the 4 kHz - 14 kHz range, where the first pulse of the
direct sound sits: a multi-way speaker's crossover and port rotate the phase
below that, so its mid and low range can show the opposite sign at the same
wiring. In that range the carrier phase at the envelope peak decides: a cosine
near +1 is a positive peak, near -1 a negative one, near 0 a wavelet without a
meaningful peak sign. A way that plays only below that range has no verdict.

A verdict needs three things: the analyzer found a direct arrival (a capture
the sweep never reached has none, and its largest sample is only noise); the
peak stands clear of the late noise floor; and its phase is clear.

Pure analysis: no hardware, no runtime.
"""

from __future__ import annotations

import math

import numpy as np

CORRECT = "correct"
REVERSED = "reversed"
INCONCLUSIVE = "inconclusive"
VERDICTS = (CORRECT, REVERSED, INCONCLUSIVE)
POLARITY_BAND_HZ = (4000.0, 14000.0)
# The band edges roll off over this factor, zero phase, so the peak keeps its sign.
BAND_EDGE_RATIO = 1.4
# The analysed part of the IR: from before the arrival to the end of the noise reference.
ANALYSIS_BEFORE_MS = 100.0
ANALYSIS_AFTER_MS = 2000.0
# The envelope peak sits within this span around the broadband arrival.
SEARCH_BEFORE_MS = 1.0
SEARCH_AFTER_MS = 2.0
# The carrier phase at the peak must be within about 60 degrees of 0 or 180.
MIN_PHASE_COS = 0.5
# The peak must exceed the late-tail noise by this much (dB).
MIN_SNR_DB = 20.0
# The noise floor is read after the room's early decay.
NOISE_TAIL_START_MS = 100.0
MIN_NOISE_TAIL_SECONDS = 0.5


def analyze_ir_polarity(impulse_response: np.ndarray, sample_rate: int, arrival_index: int | None) -> dict:
    """Judge the polarity of one measured impulse response.

    ``arrival_index`` is the direct arrival the analyzer found, or None when it
    found none. Returns ``verdict`` (correct / reversed / inconclusive),
    ``peak_sign``, ``phase_cos`` (carrier phase cosine at the envelope peak),
    ``snr_db`` (envelope peak over the late noise floor) and, for an
    inconclusive verdict, its ``reason``.
    """
    ir = np.asarray(impulse_response, dtype=np.float64)
    if ir.size == 0 or not np.all(np.isfinite(ir)) or not np.any(ir):
        return _result(INCONCLUSIVE, 0, 0.0, 0.0, "no_signal")
    if arrival_index is None or not 0 <= arrival_index < ir.size:
        return _result(INCONCLUSIVE, 0, 0.0, 0.0, "no_arrival")

    phase_cos, peak, tail = peak_phase(ir, sample_rate, arrival_index)
    if peak <= 1e-18:
        return _result(INCONCLUSIVE, 0, 0.0, 0.0, "no_signal")
    peak_sign = 1 if phase_cos > 0 else -1

    if tail.size < sample_rate * MIN_NOISE_TAIL_SECONDS:
        return _result(INCONCLUSIVE, peak_sign, phase_cos, 0.0, "weak_signal")
    noise = float(np.median(tail))
    snr_db = 20.0 * math.log10(peak / noise) if noise > 0 else 200.0

    if snr_db < MIN_SNR_DB:
        return _result(INCONCLUSIVE, peak_sign, phase_cos, snr_db, "weak_signal")
    if abs(phase_cos) < MIN_PHASE_COS:
        return _result(INCONCLUSIVE, peak_sign, phase_cos, snr_db, "unclear_phase")
    return _result(CORRECT if peak_sign > 0 else REVERSED, peak_sign, phase_cos, snr_db, "")


def peak_phase(impulse_response: np.ndarray, sample_rate: int, arrival_index: int) -> tuple[float, float, np.ndarray]:
    """Read the carrier phase at the envelope peak around the broadband arrival.

    ``impulse_response`` is a finite IR and ``arrival_index`` lies inside it.
    Returns ``(phase_cos, peak, tail)``: the carrier phase cosine at the
    envelope peak (0.0 without a peak), the envelope peak, and the envelope
    behind the room's early decay, which holds the noise floor.
    """
    ir = np.asarray(impulse_response, dtype=np.float64)

    def samples(milliseconds: float) -> int:
        return max(1, int(round(sample_rate * milliseconds / 1000.0)))

    # Only the arrival and the noise floor behind it matter; the rest of the long IR is skipped.
    first = max(0, arrival_index - samples(ANALYSIS_BEFORE_MS))
    ir = ir[first:arrival_index + samples(ANALYSIS_AFTER_MS)]
    arrival_index -= first
    analytic = _band_analytic(ir, sample_rate)
    envelope = np.abs(analytic)

    start = max(0, arrival_index - samples(SEARCH_BEFORE_MS))
    end = min(envelope.size, arrival_index + samples(SEARCH_AFTER_MS) + 1)
    peak_index = start + int(np.argmax(envelope[start:end]))
    peak = float(envelope[peak_index])
    phase_cos = float(analytic.real[peak_index]) / peak if peak > 1e-18 else 0.0
    return phase_cos, peak, envelope[arrival_index + samples(NOISE_TAIL_START_MS):]


def _band_analytic(ir: np.ndarray, sample_rate: int) -> np.ndarray:
    """Analytic signal of the zero-phase band-passed IR; its real part is that IR."""
    low, high = POLARITY_BAND_HZ
    frequencies = np.fft.rfftfreq(ir.size, 1.0 / sample_rate)

    def ramp(start: float, stop: float) -> np.ndarray:
        return np.clip((frequencies - start) / (stop - start), 0.0, 1.0)

    gain = ramp(low / BAND_EDGE_RATIO, low) * (1.0 - ramp(high, high * BAND_EDGE_RATIO))
    spectrum = np.zeros(ir.size, dtype=np.complex128)
    spectrum[:frequencies.size] = np.fft.rfft(ir) * gain
    spectrum[1:(ir.size + 1) // 2] *= 2.0
    return np.fft.ifft(spectrum)


def _result(verdict: str, peak_sign: int, phase_cos: float, snr_db: float, reason: str) -> dict:
    return {"verdict": verdict, "peak_sign": peak_sign, "phase_cos": round(phase_cos, 3),
            "snr_db": round(snr_db, 2), "reason": reason}
