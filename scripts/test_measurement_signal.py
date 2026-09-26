"""Equivalence checks for pure measurement sweep generation."""

import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from measurement.signal import build_inverse_sweep, generate_log_sweep, write_sweep_file
import measurement.signal as signals
from measurement.store import MeasurementStore


class MeasurementSignalTests(unittest.TestCase):
    def test_repeated_profiles_reuse_preparation_without_sharing_mutable_results(self):
        options = dict(sample_rate=48_000, sweep_seconds=0.251,
                       lead_in_seconds=0.1, tail_seconds=0.05,
                       start_hz=17.0, end_hz=19_000.0, peak_scale=0.7)
        expected = generate_log_sweep(48_000, 0.251, 17.0, 19_000.0, 0.7)
        inverse = build_inverse_sweep(expected, 48_000, 0.251, 17.0, 19_000.0)
        with tempfile.TemporaryDirectory() as directory, patch.object(
                signals, "generate_log_sweep", wraps=generate_log_sweep) as generate, patch.object(
                signals, "build_inverse_sweep", wraps=build_inverse_sweep) as build:
            paths = [Path(directory) / f"take-{i}.wav" for i in range(3)]
            first = write_sweep_file(paths[0], channel="left", **options)
            first["analysis_sweep"][:] = 0
            first["inverse_sweep"][:] = 0
            second = write_sweep_file(paths[1], channel="right", **options)
            third = write_sweep_file(paths[2], channel="left", **options)
            np.testing.assert_array_equal(second["analysis_sweep"], expected)
            np.testing.assert_array_equal(second["inverse_sweep"], inverse)
            self.assertEqual(paths[0].read_bytes(), paths[2].read_bytes())
            with wave.open(str(paths[1]), "rb") as wav:
                pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").reshape(-1, 2)
            self.assertFalse(np.any(pcm[:, 0]))
            np.testing.assert_array_equal(
                pcm[4800:4800 + expected.size, 1], np.round(expected * 32767).astype(np.int16))
            self.assertEqual(generate.call_count, 1, "Identical signals must be generated once")
            self.assertEqual(build.call_count, 1, "Identical inverse kernels must be built once")

    def test_preparation_cache_separates_signal_parameters_and_is_bounded(self):
        options = dict(sample_rate=48_000, sweep_seconds=0.253,
                       lead_in_seconds=0.03, tail_seconds=0.04, channel="left",
                       start_hz=23.0, end_hz=17_000.0, peak_scale=0.63)
        variants = [{}, {"sample_rate": 44100}, {"sweep_seconds": 0.254},
                    {"start_hz": 31.0}, {"end_hz": 16_000.0}, {"peak_scale": 0.59}]
        with tempfile.TemporaryDirectory() as directory, patch.object(
                signals, "generate_log_sweep", wraps=generate_log_sweep) as generate:
            path = Path(directory) / "take.wav"
            for variant in variants:
                args = {**options, **variant}
                result = write_sweep_file(path, **args)
                expected = generate_log_sweep(args["sample_rate"], args["sweep_seconds"],
                                              args["start_hz"], args["end_hz"], args["peak_scale"])
                np.testing.assert_array_equal(result["analysis_sweep"], expected)
            self.assertEqual(generate.call_count, len(variants))
            write_sweep_file(path, **options)
            self.assertEqual(generate.call_count, len(variants) + 1, "Old profiles must be evicted")
            write_sweep_file(path, **{**options, "lead_in_seconds": 0.06, "channel": "right"})
            self.assertEqual(generate.call_count, len(variants) + 1, "Routing/padding do not change the signal")

    def test_log_sweep_shape_peak_and_fade_are_stable(self):
        sweep = generate_log_sweep(
            sample_rate=48_000,
            duration_seconds=0.25,
            start_hz=10.0,
            end_hz=22_000.0,
            peak_scale=0.8,
        )

        self.assertEqual(sweep.dtype, np.float32)
        self.assertEqual(sweep.size, 12_000)
        self.assertTrue(np.isclose(np.max(np.abs(sweep)), 0.8, atol=1e-6))
        self.assertEqual(sweep[0], 0.0)
        self.assertEqual(sweep[-1], 0.0)

    def test_inverse_sweep_matches_generated_signal_shape_and_normalizes_reference_ir(self):
        sweep = generate_log_sweep(48_000, 0.25, 10.0, 22_000.0, 0.8)
        inverse = build_inverse_sweep(sweep, 48_000, 0.25, 10.0, 22_000.0)

        self.assertEqual(inverse.dtype, np.float64)
        self.assertEqual(inverse.size, sweep.size)
        fft_size = 1 << (2 * sweep.size - 1).bit_length()
        reference_ir = np.fft.irfft(
            np.fft.rfft(sweep.astype(np.float64), n=fft_size)
            * np.fft.rfft(inverse, n=fft_size),
            n=fft_size,
        )[: 2 * sweep.size - 1]
        self.assertTrue(np.isclose(np.max(np.abs(reference_ir)), 1.0, atol=1e-6))

    def test_analyzer_absolute_level_is_independent_of_sweep_profile(self):
        physical_gain_db = -40.0
        physical_gain = 10 ** (physical_gain_db / 20.0)
        profiles = (
            (11.0, 10.0, 22_000.0),
            (3.0, 20.0, 640.0),
        )
        calibrated_levels = []
        sweep_calibrations = []

        with tempfile.TemporaryDirectory() as directory:
            analyzer = MeasurementStore(home=Path(directory))._analyzer
            for duration_seconds, start_hz, end_hz in profiles:
                sweep = generate_log_sweep(
                    48_000, duration_seconds, start_hz, end_hz, peak_scale=0.8,
                )
                inverse = build_inverse_sweep(
                    sweep, 48_000, duration_seconds, start_hz, end_hz,
                )
                reference_ir = analyzer._fft_convolve(sweep.astype(np.float64), inverse)
                frequencies, magnitude, _ = analyzer._build_variable_window_response(
                    reference_ir, 48_000,
                )
                calibration_db = analyzer._sweep_level_calibration_db(
                    reference_sweep=sweep,
                    inverse_sweep=inverse,
                    sample_rate=48_000,
                )
                scaled_inverse_calibration_db = analyzer._sweep_level_calibration_db(
                    reference_sweep=sweep,
                    inverse_sweep=inverse * 0.5,
                    sample_rate=48_000,
                )
                self.assertAlmostEqual(
                    scaled_inverse_calibration_db,
                    calibration_db - 20.0 * np.log10(2.0),
                    delta=0.01,
                )
                display = analyzer._build_display_points(
                    frequencies=frequencies,
                    magnitude=magnitude * physical_gain,
                    calibration_curve=None,
                    sweep_level_calibration_db=calibration_db,
                )
                point = min(display["trusted_points"], key=lambda item: abs(item[0] - 200.0))
                calibrated_levels.append(point[1] + display["normalized_by"])
                sweep_calibrations.append(calibration_db)

        self.assertGreater(sweep_calibrations[1] - sweep_calibrations[0], 25.0)
        for calibrated_level in calibrated_levels:
            self.assertAlmostEqual(calibrated_level, physical_gain_db, delta=0.5)
        self.assertAlmostEqual(calibrated_levels[0], calibrated_levels[1], delta=0.5)

    def test_write_sweep_file_preserves_stereo_layout_and_pcm_format(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sweep.wav"
            result = write_sweep_file(
                path,
                sample_rate=48_000,
                sweep_seconds=0.25,
                lead_in_seconds=0.1,
                tail_seconds=0.05,
                channel="right",
                start_hz=10.0,
                end_hz=22_000.0,
                peak_scale=0.8,
            )
            with wave.open(str(path), "rb") as handle:
                self.assertEqual(handle.getnchannels(), 2)
                self.assertEqual(handle.getsampwidth(), 2)
                self.assertEqual(handle.getframerate(), 48_000)
                self.assertEqual(handle.getnframes(), result["samples"])
            self.assertEqual(result["channels"], 2)
            self.assertEqual(result["samples"], round(48_000 * (0.1 + 0.25 + 0.05)))


if __name__ == "__main__":
    unittest.main()
