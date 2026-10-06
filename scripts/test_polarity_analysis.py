#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Polarity verdicts from signed impulse responses and from synthetic sweep captures."""

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from measurement import polarity
from measurement.signal import build_inverse_sweep, generate_log_sweep
from measurement.store import MeasurementStore

RATE = 48_000
ARRIVAL = 2_400
LENGTH = RATE * 2


def spike(at=ARRIVAL, level=1.0):
    ir = np.zeros(LENGTH)
    ir[at] = level
    return ir


def with_noise(ir, level=1e-3, seed=1):
    return ir + np.random.default_rng(seed).normal(0.0, level, ir.size)


def gaussian_lobe(width_ms, at=ARRIVAL):
    t = (np.arange(LENGTH) - at) / (RATE * width_ms / 1000.0)
    return np.exp(-0.5 * t * t)


def quadrature(ir):
    """The same wavelet shifted by 90 degrees: equal positive and negative lobes."""
    spectrum = np.fft.rfft(ir) * -1j
    return np.fft.irfft(spectrum, n=ir.size)


def verdict(ir, arrival=ARRIVAL):
    return polarity.analyze_ir_polarity(ir, RATE, arrival)


class VerdictTests(unittest.TestCase):
    def test_a_positive_and_a_negative_spike(self):
        self.assertEqual(verdict(with_noise(spike()))["verdict"], polarity.CORRECT)
        result = verdict(with_noise(-spike()))
        self.assertEqual(result["verdict"], polarity.REVERSED)
        self.assertEqual(result["peak_sign"], -1)

    def test_a_speaker_like_wavelet_keeps_its_sign(self):
        # A woofer lobe plus a slow negative undershoot and some high-frequency ringing.
        wavelet = gaussian_lobe(0.4, ARRIVAL + 24) - 0.2 * gaussian_lobe(2.0, ARRIVAL + 200)
        wavelet += 0.1 * np.sin(np.arange(LENGTH) * 2 * np.pi * 6000 / RATE) * gaussian_lobe(0.3, ARRIVAL)
        self.assertEqual(verdict(with_noise(wavelet))["verdict"], polarity.CORRECT)
        self.assertEqual(verdict(with_noise(-wavelet))["verdict"], polarity.REVERSED)

    def test_a_low_passed_lobe_has_a_clear_sign(self):
        lobe = gaussian_lobe(1.0, ARRIVAL + 24)
        self.assertEqual(verdict(with_noise(lobe))["verdict"], polarity.CORRECT)
        self.assertEqual(verdict(with_noise(-lobe))["verdict"], polarity.REVERSED)

    def test_a_quadrature_wavelet_is_inconclusive(self):
        # Equal positive and negative lobes: the phase decides, not the wiring.
        result = verdict(with_noise(quadrature(spike())))
        self.assertEqual(result["verdict"], polarity.INCONCLUSIVE)
        self.assertEqual(result["reason"], "mixed_lobes")

    def test_a_far_opposite_peak_does_not_count(self):
        ir = spike()
        ir[ARRIVAL + 960] = -0.9  # 20 ms later: a reflection, not part of the arrival
        self.assertEqual(verdict(with_noise(ir))["verdict"], polarity.CORRECT)

    def test_no_arrival_means_no_verdict(self):
        result = verdict(with_noise(spike()), arrival=None)
        self.assertEqual((result["verdict"], result["reason"]), (polarity.INCONCLUSIVE, "no_arrival"))

    def test_noise_at_a_supposed_arrival_is_weak(self):
        # The capture never held the sweep: its largest sample is noise whatever its sign.
        for seed in range(20):
            noise = np.random.default_rng(seed).normal(0.0, 1.0, LENGTH)
            result = verdict(noise)
            self.assertEqual((result["verdict"], result["reason"]), (polarity.INCONCLUSIVE, "weak_signal"), seed)

    def test_a_short_capture_has_no_noise_reference(self):
        result = polarity.analyze_ir_polarity(with_noise(spike())[:RATE // 4], RATE, ARRIVAL)
        self.assertEqual((result["verdict"], result["reason"]), (polarity.INCONCLUSIVE, "weak_signal"))

    def test_silence_and_invalid_input_are_inconclusive(self):
        for ir in (np.zeros(LENGTH), np.array([]), np.array([1.0, float("nan")])):
            result = verdict(ir)
            self.assertEqual((result["verdict"], result["reason"]), (polarity.INCONCLUSIVE, "no_signal"))


class AnalyzerTests(unittest.TestCase):
    """The analyzer reports the verdict of the microphone channel, not of the timing reference."""

    def analyze(self, mic_for):
        sweep = generate_log_sweep(RATE, 0.68, 10.0, 22_000.0, peak_scale=0.8)
        lead = np.zeros(int(round(RATE * 0.34)), dtype=np.float32)
        tail = np.zeros(int(round(RATE * 0.32)), dtype=np.float32)
        reference = np.concatenate([lead, sweep, tail]).astype(np.float32)
        mic = mic_for(sweep, reference, lead.size + 4_800).astype(np.float32)
        with tempfile.TemporaryDirectory() as home:
            store = MeasurementStore(home=Path(home))
            path = store.captures_dir / "polarity.wav"
            store._write_wav(path, np.column_stack([mic, reference]), RATE)
            return store._analyzer._analyze_sweep_capture(
                path, expected_sample_rate=RATE, channel="left", reference_sweep=sweep,
                inverse_sweep=build_inverse_sweep(sweep, RATE, 0.68, 10.0, 22_000.0),
                calibration_curve=None, capture_label="Synthetic capture",
                reference_channel_index=1, analysis_channel_index=0, reference_channel_label="reference")

    @staticmethod
    def delayed_sweep(gain):
        def build(sweep, reference, offset):
            mic = np.zeros_like(reference)
            mic[offset:offset + sweep.size] = gain * sweep
            return mic
        return build

    def test_a_correct_speaker_reads_correct(self):
        analysis = self.analyze(self.delayed_sweep(0.5))
        self.assertEqual(analysis["polarity"]["verdict"], polarity.CORRECT, analysis["polarity"])
        self.assertEqual(analysis["polarity"]["peak_sign"], 1)

    def test_an_inverted_microphone_signal_reads_reversed(self):
        analysis = self.analyze(self.delayed_sweep(-0.5))
        self.assertEqual(analysis["polarity"]["verdict"], polarity.REVERSED, analysis["polarity"])
        self.assertEqual(analysis["polarity"]["peak_sign"], -1)

    def test_a_capture_without_the_sweep_has_no_verdict(self):
        # A silent speaker leaves the microphone with noise; the monitor still shows the sweep.
        def noise_only(sweep, reference, offset):
            return np.random.default_rng(7).normal(0.0, 1e-3, reference.size)
        analysis = self.analyze(noise_only)
        self.assertEqual(analysis["polarity"]["verdict"], polarity.INCONCLUSIVE, analysis["polarity"])
        self.assertIn(analysis["polarity"]["reason"], ("no_arrival", "weak_signal"))


class PersistenceTests(unittest.TestCase):
    def test_a_saved_measurement_keeps_the_polarity_reading(self):
        reading = {"verdict": "reversed", "peak_sign": -1, "dominance_db": 12.0, "snr_db": 60.0, "reason": ""}
        analysis = {
            "method": "test", "sample_rate": RATE, "rms_dbfs": -30.0, "peak_dbfs": -12.0, "window_count": 1,
            "normalized_by_db": 0.0, "alignment_samples": 0, "alignment_seconds": 0.0,
            "trusted_min_hz": 20.0, "trusted_max_hz": 20_000.0, "raw_point_count": 2,
            "review_point_count": 2, "display_point_count": 2, "trusted_band_meta": {},
            "review_band_meta": {}, "quality_checks": {"items": []}, "capture_audit": {}, "clock": {},
            "reference_path": {}, "impulse_response": {}, "variable_window": {}, "polarity": reading,
            "trusted_points": [[20, 0], [20_000, 0]], "review_points": [[20, 0], [20_000, 0]],
        }
        with tempfile.TemporaryDirectory() as home:
            store = MeasurementStore(home=Path(home))
            measurement = store._persistence._build_measurement_from_analysis(
                analysis, input_device={"id": "mic", "label": "Mic"}, channel="left",
                calibration={"applied": False})
        self.assertEqual(measurement["analysis"]["polarity"], reading)


if __name__ == "__main__":
    unittest.main()
