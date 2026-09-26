#!/usr/bin/env python3
import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from measurement.analyzer import MeasurementAnalyzer
from measurement.signal import build_inverse_sweep, generate_log_sweep
from measurement.store import MeasurementStore


class MeasurementAnalyzerIrTests(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory(prefix="analyzer-ir-")
        self.addCleanup(home.cleanup)
        environment = patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(Path(home.name) / "config"),
            "XDG_STATE_HOME": str(Path(home.name) / "state"),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def test_display_smoothing_preserves_inclusive_bands_without_repeated_grid_scans(self):
        scans = []

        class CountingGrid(np.ndarray):
            def __ge__(self, other):
                scans.append(self.size)
                return super().__ge__(other)

            def __le__(self, other):
                scans.append(self.size)
                return super().__le__(other)

        frequencies = np.linspace(0, 24000, 131073)
        magnitude = 0.3 + np.random.default_rng(31).random(frequencies.size)
        calibration = (np.array([20., 1000., 22000.]), np.array([-1., 2., -3.]))
        corrected = magnitude * 10 ** (-np.interp(
            np.log(np.clip(frequencies, 1e-9, None)), np.log(calibration[0]), calibration[1]) / 20)
        with tempfile.TemporaryDirectory() as home:
            store = MeasurementStore(home=Path(home))
            result = store._analyzer._build_display_points(
                frequencies=frequencies.view(CountingGrid), magnitude=magnitude,
                calibration_curve=calibration, sweep_level_calibration_db=3.5)
            for center, level in result["review_points"]:
                band = corrected[(frequencies >= center / 2 ** (1 / 12))
                                 & (frequencies <= min(center * 2 ** (1 / 12), 22000))]
                expected_db = 20 * np.log10(np.sqrt(np.mean(band ** 2))) - 3.5
                self.assertAlmostEqual(level + result["normalized_by"], expected_db, delta=0.002)
        self.assertLessEqual(sum(scans), 4 * frequencies.size,
                             "Display smoothing must not scan the whole FFT grid for every point")

    def test_identity_resampling_reuses_mic_deconvolution_but_drift_keeps_separate_magnitude(self):
        rate = 48000
        sweep = generate_log_sweep(rate, 0.68, 10., 22000.)
        inverse = build_inverse_sweep(sweep, rate, 0.68, 10., 22000.)
        lead = 16320
        reference = np.concatenate([np.zeros(lead), sweep, np.zeros(12000)])
        mic = np.zeros_like(reference)
        mic[96:] = reference[:-96] * 0.5
        with tempfile.TemporaryDirectory() as home:
            store = MeasurementStore(home=Path(home))
            analyzer = store._analyzer
            path = store.captures_dir / "capture.wav"
            store._write_wav(path, np.column_stack([mic, reference]), rate)
            analyzer._sweep_level_calibration_db(
                reference_sweep=sweep, inverse_sweep=inverse, sample_rate=rate)
            for stretch, count in ((1., 2), (1.0005, 3)):
                with self.subTest(stretch=stretch), patch.object(
                        analyzer, "_fft_convolve", wraps=analyzer._fft_convolve) as convolve:
                    received = []
                    result = analyzer._analyze_sweep_capture(
                        path, expected_sample_rate=rate, channel="left", reference_sweep=sweep,
                        inverse_sweep=inverse, calibration_curve=None,
                        reference_channel_index=1, analysis_channel_index=0,
                        timing_ir_receiver=lambda ir, analysis: received.append(ir),
                        timing_override={"alignment_samples": lead, "observed_sweep_samples": sweep.size,
                                         "stretch_ratio": stretch, "start_score": 1., "end_score": 1.})
                    self.assertEqual(convolve.call_count, count)
                    self.assertFalse(result["clock"]["magnitude_drift_resampling_applied"])
                    if stretch == 1.:
                        self.assertEqual(int(np.argmax(np.abs(received[0]))) - (sweep.size - 1), 96)

    def test_direct_arrival_promotes_stronger_candidate_by_energy(self):
        impulse = np.zeros(6000, dtype=np.float64)
        impulse[4300] = 0.06
        impulse[4495:4506] = [0.055, 0.057, 0.059, 0.060, 0.061, 0.062, 0.061, 0.060, 0.059, 0.057, 0.055]
        impulse[5000] = 1.0
        reference = np.zeros_like(impulse)
        reference[5000] = 1.0

        analyzer = MeasurementAnalyzer(None, None)
        result = analyzer._estimate_impulse_direct_arrival(impulse, reference, 48_000)

        self.assertEqual(result["direct_arrival_index"], 4500)
        self.assertEqual(result["selection_rule"], "skipped_weak_threshold_edge_for_stronger_impulse_region")
        self.assertTrue(result["promotion_applied"])

    def test_arrival_before_electrical_reference_is_ambiguous(self):
        impulse = np.zeros(6000, dtype=np.float64)
        reference = np.zeros_like(impulse)
        impulse[4593] = 1.0
        reference[5000] = 1.0

        result = MeasurementAnalyzer(None, None)._estimate_impulse_direct_arrival(
            impulse, reference, 48_000
        )

        self.assertEqual(result["relative_samples"], -407)
        self.assertFalse(result["timing_valid"])
        self.assertEqual(result["timing_status"], "ambiguous")
        self.assertEqual(result["confidence"], 0.0)
        # A lone spike has no rise: the band-limited rule changes nothing.
        banded = MeasurementAnalyzer(None, None)._estimate_impulse_direct_arrival(
            impulse, reference, 48_000, band_limited=True)
        self.assertEqual(banded, result)

    @staticmethod
    def _slow_rise(size, peak_at, rise):
        """A band-limited reference shape: a long rise into its peak."""
        response = np.zeros(size, dtype=np.float64)
        response[peak_at - rise:peak_at + 1] = np.linspace(0.0, 1.0, rise + 1) ** 3
        response[peak_at + 1:peak_at + 1 + rise] = np.linspace(1.0, 0.0, rise) ** 3
        return response

    def test_band_limited_arrival_inside_the_reference_rise_is_timed_against_its_arrival(self):
        # A 120 Hz way: the reference rises for ~1300 samples before its peak,
        # and the microphone's arrival lands inside that rise.
        reference = self._slow_rise(12000, 8000, 1300)
        impulse = np.roll(reference, 240)
        analyzer = MeasurementAnalyzer(None, None)
        full_band = analyzer._estimate_impulse_direct_arrival(impulse, reference, 48_000)
        self.assertFalse(full_band["timing_valid"])
        self.assertEqual(full_band["timing_source"], "direct_arrival_minus_reference_peak")

        banded = analyzer._estimate_impulse_direct_arrival(impulse, reference, 48_000, band_limited=True)
        self.assertTrue(banded["timing_valid"])
        self.assertEqual(banded["relative_samples"], 240)
        self.assertEqual(banded["timing_source"], "direct_arrival_minus_reference_arrival")
        self.assertEqual(banded["reference_peak_index"], 8000)
        self.assertLess(banded["reference_arrival_index"], banded["reference_peak_index"])
        self.assertEqual(banded["direct_arrival_index"], full_band["direct_arrival_index"])

    def test_band_limited_take_the_peak_already_times_keeps_its_values(self):
        reference = self._slow_rise(12000, 8000, 60)
        impulse = np.roll(reference, 240)
        analyzer = MeasurementAnalyzer(None, None)
        full_band = analyzer._estimate_impulse_direct_arrival(impulse, reference, 48_000)
        banded = analyzer._estimate_impulse_direct_arrival(impulse, reference, 48_000, band_limited=True)
        self.assertTrue(full_band["timing_valid"])
        self.assertEqual(banded, full_band)
        self.assertEqual(banded["reference_arrival_index"], banded["reference_peak_index"])

    def test_negative_arrival_does_not_leave_analyzer_as_timing(self):
        rate = 48_000
        sweep = generate_log_sweep(rate, 0.68, 10.0, 22_000.0, peak_scale=0.8)
        lead = np.zeros(int(rate * 0.34), dtype=np.float32)
        tail = np.zeros(int(rate * 0.18), dtype=np.float32)
        reference = np.concatenate([lead, sweep, tail]).astype(np.float32)
        mic = np.zeros_like(reference)
        mic[:-407] = reference[407:]
        with tempfile.TemporaryDirectory() as home:
            store = MeasurementStore(home=Path(home))
            path = store.captures_dir / "negative-arrival.wav"
            store._write_wav(path, np.column_stack([mic, reference]), rate)
            analysis = store._analyzer._analyze_sweep_capture(
                path, expected_sample_rate=rate, channel="left",
                reference_sweep=sweep, inverse_sweep=np.array([1.0]),
                calibration_curve=None, capture_label="Negative arrival",
                reference_channel_index=1, analysis_channel_index=0,
                reference_channel_label="reference",
            )

        impulse_meta = analysis["impulse_response"]
        self.assertFalse(analysis["direct_arrival_timing_available"])
        self.assertEqual(impulse_meta["timing_status"], "ambiguous")
        self.assertLess(impulse_meta["direct_arrival_index"], impulse_meta["reference_peak_index"])
        self.assertEqual(impulse_meta["direct_confidence"], 0.0)
        for key in ("arrival_ms", "arrival_seconds", "arrival_samples"):
            self.assertNotIn(key, impulse_meta)
        analysis["reference_path"].update(peak_dbfs=-12.0, ir_sharpness_db=40.0)
        analysis["clock"].update(start_score=0.99, end_score=0.99)
        verdict = store._evaluate_electrical_reference_status(analysis)
        self.assertFalse(verdict["usable"])
        self.assertFalse(analysis["reference_path"].get("electrical_reference_used", False))


if __name__ == "__main__":
    unittest.main()
