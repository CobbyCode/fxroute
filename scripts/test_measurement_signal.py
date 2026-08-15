"""Equivalence checks for pure measurement sweep generation."""

import sys
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from measurement.signal import build_inverse_sweep, generate_log_sweep, write_sweep_file


class MeasurementSignalTests(unittest.TestCase):
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
