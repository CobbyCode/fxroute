#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Automatic SPL calibration: a capture whose noise had not settled is rejected, a steady one is not."""

import asyncio
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import measurement.spl_calibration as spl_calibration
import test_spl_pw_record_capture as support
from fastapi import HTTPException

RATE = 48_000
SENSITIVITY_DB = 0.371
UMM6_FIXTURE = ROOT / "scripts" / "fixtures" / "1880171.txt"


def pink_noise(seconds, seed):
    """Seeded 1/f noise with unit RMS."""
    count = int(seconds * RATE)
    spectrum = np.fft.rfft(np.random.default_rng(seed).standard_normal(count))
    frequencies = np.fft.rfftfreq(count, 1.0 / RATE)
    spectrum[1:] /= np.sqrt(frequencies[1:])
    spectrum[0] = 0.0
    noise = np.fft.irfft(spectrum, count)
    return noise / np.sqrt(np.mean(np.square(noise)))


def capture(onset_seconds, *, seconds=7.0, rms=0.05, seed=1, fade_seconds=1.0):
    """A mono capture that is silent until the noise starts, fades in like the product's noise file, then is steady."""
    signal = pink_noise(seconds, seed) * rms
    start = int(onset_seconds * RATE)
    signal[:start] = 0.0
    fade = min(int(fade_seconds * RATE), signal.size - start)
    signal[start:start + fade] *= np.linspace(0.0, 1.0, fade)
    return signal


def ffmpeg_pink_noise(path, seed):
    """The product's calibration noise (same filter chain, seeded so the test is repeatable)."""
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
        "-i", f"anoisesrc=color=pink:duration=60:sample_rate=48000:seed={seed}",
        "-af", "loudnorm=I=-23:TP=-3:LRA=7,afade=t=in:st=0:d=1,afade=t=out:st=59.5:d=0.5",
        "-ac", "2", "-ar", "48000", str(path)], check=True, timeout=60)
    return spl_calibration._read_pcm16_channel(path, 0)[1]


def write_capture(path, mono):
    pcm = np.clip(mono * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(np.stack([pcm, pcm], axis=1).tobytes())


class SplStationarityGateTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.calibration = support.calibration_file(self.directory)
        original = spl_calibration._runtime.dependencies
        self.addCleanup(setattr, spl_calibration._runtime, "dependencies", original)
        self.addCleanup(setattr, spl_calibration._runtime, "operation", None)
        support.configure(support.FakeMeasurementStore(self.calibration, supports_sample_count=True))
        profile = mock.patch.object(spl_calibration, "_spl_output_profile", return_value={"id": "dev", "label": "Dev"})
        profile.start()
        self.addCleanup(profile.stop)

    def levels(self, mono, profile=None, calibration=None):
        return spl_calibration._spl_window_levels(
            mono, RATE, SENSITIVITY_DB, calibration or self.calibration, profile=profile)

    def run_endpoint(self, mono=None, *, samples=None):
        """The automatic measurement endpoint over a recorder that delivers `mono`."""
        def popen(argv, **_kwargs):
            write_capture(Path(argv[-1]), mono)
            return support.LiveRecorder(argv)

        patches = [
            mock.patch("tempfile.gettempdir", return_value=str(self.directory)),
            mock.patch.object(spl_calibration.subprocess, "Popen", side_effect=popen),
            mock.patch.object(spl_calibration.subprocess, "run", side_effect=support.pactl_result),
            mock.patch.object(spl_calibration, "_start_spl_calibration_noise", return_value={"status": "playing"}),
        ]
        if samples is not None:
            patches.append(mock.patch.object(spl_calibration, "_read_pcm16_channel", return_value=(RATE, samples)))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return asyncio.run(spl_calibration.measure_spl_automatically())

    def test_a_steady_capture_reads_the_same_level_as_before_and_reports_its_drift(self):
        mono = capture(1.0)
        result = self.run_endpoint(mono)
        self.assertEqual(result["status"], "ok")
        self.assertLess(abs(result["level_drift_db"]), 0.5)
        pcm = np.clip(mono * 32767.0, -32768, 32767).astype(np.int16) / 32768.0
        previous = spl_calibration._c_weighted_spl_from_capture(
            pcm[-3 * RATE:], RATE, SENSITIVITY_DB, self.calibration)
        self.assertEqual(result["measured_spl_db"], round(previous, 2))
        self.assertEqual(result["averaging_seconds"], 3.0)
        self.assertEqual(set(result), {"status", "measured_spl_db", "required_adjustment_db", "weighting",
                                       "averaging_seconds", "level_drift_db", "output_profile",
                                       "microphone_model", "serial_number"})

    def test_noise_that_starts_in_time_is_accepted(self):
        for onset in (0.0, 1.0, 1.8, 2.0, 2.5, 3.0, 3.5):
            with self.subTest(onset=onset):
                result = self.run_endpoint(capture(onset))
                self.assertEqual(result["status"], "ok")
                self.assertLess(abs(result["level_drift_db"]), 0.5, result)

    def test_a_warm_noise_start_is_accepted(self):
        # Field measurement on .104: warm runs start the noise after 1.22-1.30 s.
        for onset in (1.22, 1.30):
            with self.subTest(onset=onset):
                result = self.run_endpoint(capture(onset))
                self.assertEqual(result["status"], "ok")
                self.assertLess(abs(result["level_drift_db"]), 0.5, result)

    def test_a_cold_start_after_the_noise_file_is_built_is_accepted(self):
        # Field measurement on .104: the first run generates the ffmpeg noise file and the
        # noise starts after 2.67-2.82 s. The 7 s capture keeps that inside the settled window.
        for onset in (2.67, 2.82):
            with self.subTest(onset=onset):
                result = self.run_endpoint(capture(onset))
                self.assertEqual(result["status"], "ok")
                self.assertLess(abs(result["level_drift_db"]), 0.5, result)

    def test_the_capture_keeps_the_3_s_window_and_the_0_5_db_gate(self):
        self.assertEqual(spl_calibration.SPL_CAPTURE_SECONDS, 7.0)
        self.assertEqual(spl_calibration.SPL_WINDOW_SECONDS, 3.0)
        self.assertEqual(spl_calibration.SPL_MAX_LEVEL_RISE_DB, 0.5)

    def test_noise_that_starts_late_is_rejected_with_a_clear_message(self):
        for onset in (4.5, 5.0, 5.5):
            with self.subTest(onset=onset):
                with self.assertRaises(HTTPException) as caught:
                    self.run_endpoint(capture(onset))
                self.assertEqual(caught.exception.status_code, 409)
                self.assertIn("The noise level was still rising during the measurement window. Repeat the measurement.",
                              caught.exception.detail)

    def test_a_rejected_run_releases_its_operation_and_removes_the_capture(self):
        with self.assertRaises(HTTPException):
            self.run_endpoint(capture(5.0))
        self.assertIsNone(spl_calibration._runtime.operation)
        self.assertEqual(list(self.directory.glob("fxroute-spl-*.wav")), [])

    def test_the_measured_rise_grows_with_the_lateness_of_the_onset(self):
        rises = [self.levels(capture(onset))[1] for onset in (2.5, 3.0, 4.0, 4.5, 5.0)]
        self.assertEqual(rises, sorted(rises))
        self.assertLess(rises[1], spl_calibration.SPL_MAX_LEVEL_RISE_DB)
        self.assertGreater(rises[3], spl_calibration.SPL_MAX_LEVEL_RISE_DB)
        self.assertEqual(spl_calibration.SPL_MAX_LEVEL_RISE_DB, 0.5)

    def test_a_silent_capture_keeps_its_previous_error(self):
        with self.assertRaises(HTTPException) as caught:
            self.run_endpoint(np.zeros(7 * RATE))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("outside the valid range", caught.exception.detail)
        self.assertNotIn("still rising", caught.exception.detail)

    def test_a_non_finite_capture_keeps_its_previous_error(self):
        with self.assertRaises(HTTPException) as caught:
            self.run_endpoint(np.zeros(7 * RATE), samples=np.full(7 * RATE, np.nan))
        self.assertIn("outside the valid range", caught.exception.detail)

    def test_a_too_short_capture_keeps_its_previous_error(self):
        with self.assertRaises(HTTPException) as caught:
            self.run_endpoint(np.zeros(RATE // 2), samples=np.zeros(RATE // 2))
        self.assertIn("Could not calculate UMIK-1 SPL", caught.exception.detail)
        self.assertIn("too short", caught.exception.detail)

    def test_steady_noise_is_almost_never_rejected(self):
        rng = np.random.default_rng(11)
        rises = []
        for seed in (1, 2, 3):
            noise = pink_noise(120.0, seed) * 0.05
            for _ in range(200):
                start = int(rng.integers(0, noise.size - 7 * RATE))
                rises.append(self.levels(noise[start:start + 7 * RATE])[1])
        rises = np.array(rises)
        self.assertLess(abs(float(rises.mean())), 0.05)
        self.assertLess(float(rises.std()), 0.25)
        self.assertLessEqual(int(np.sum(rises > spl_calibration.SPL_MAX_LEVEL_RISE_DB)), 6)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is needed to generate the product's noise")
    def test_the_products_own_noise_is_not_rejected_while_steady_and_is_when_it_starts_late(self):
        rng = np.random.default_rng(11)
        rises = []
        for seed in (1, 2, 3):
            noise = ffmpeg_pink_noise(self.directory / f"pink{seed}.wav", seed)
            for _ in range(200):
                start = int(rng.integers(3 * RATE, 55 * RATE - 7 * RATE))
                rises.append(self.levels(noise[start:start + 7 * RATE])[1])
            onset = {}
            for seconds in (2.82, 3.5, 4.0, 4.5, 5.0):
                heard = np.zeros(7 * RATE)
                start = int(seconds * RATE)
                heard[start:] = noise[:7 * RATE - start]
                onset[seconds] = self.levels(heard)[1]
            self.assertLessEqual(onset[2.82], spl_calibration.SPL_MAX_LEVEL_RISE_DB)
            self.assertLessEqual(onset[3.5], spl_calibration.SPL_MAX_LEVEL_RISE_DB)
            self.assertLessEqual(onset[4.0], spl_calibration.SPL_MAX_LEVEL_RISE_DB)
            self.assertGreater(onset[4.5], spl_calibration.SPL_MAX_LEVEL_RISE_DB)
            self.assertGreater(onset[5.0], spl_calibration.SPL_MAX_LEVEL_RISE_DB)
        rises = np.array(rises)
        self.assertLess(float(rises.std()), 0.2)
        self.assertLessEqual(int(np.sum(rises > spl_calibration.SPL_MAX_LEVEL_RISE_DB)), 1)

    def test_every_microphone_profile_goes_through_the_same_gate(self):
        umm6 = spl_calibration._UMM6_PROFILE
        for label, profile, calibration in (("UMIK-1", None, self.calibration), ("UMIK-2", None, self.calibration),
                                            ("UMM-6", umm6, UMM6_FIXTURE)):
            with self.subTest(label):
                steady = self.levels(capture(1.0), profile, calibration)
                late = self.levels(capture(5.0), profile, calibration)
                self.assertLess(abs(steady[1]), 0.5)
                self.assertGreater(late[1], 0.5)
                with self.assertRaises(RuntimeError):
                    spl_calibration._require_settled_level(late[1])
                spl_calibration._require_settled_level(steady[1])

    def test_a_falling_or_missing_drift_is_not_a_rise(self):
        for value in (0.0, -3.0, float("nan"), 0.5):
            with self.subTest(value=value):
                spl_calibration._require_settled_level(value)


if __name__ == "__main__":
    unittest.main()
