#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Non-acoustic profile, calibration and capture regressions for Speaker Align."""

import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from measurement.alignment_backend import estimate_way_level, way_crossover_specs, way_passband
from measurement.signal import write_sweep_file
from measurement.speaker_profile import speaker_way_sweep_profile
from measurement.store import MeasurementStore, default_measurement_sweep_profile
from dsp.crossover import design_crossover
from test_speaker_align import alignment_for, state_for


def profile_for(model, rate):
    return speaker_way_sweep_profile(model, sample_rate_hz=rate)


def synthetic_way_capture(store, model, profile, *, rate=48000, gain_db=-30.,
                          reflection=0.0, noise=0.0, calibration=None):
    """Render one way into a WAV; use the real analyzer without audio processes."""
    profile = {**default_measurement_sweep_profile(), **profile}
    sweep = write_sweep_file(
        store.playbacks_dir / "dry.wav", sample_rate=rate, channel="left",
        sweep_seconds=profile["sweep_seconds"], lead_in_seconds=profile["lead_in_seconds"],
        tail_seconds=profile["tail_seconds"], start_hz=profile["sweep_start_hz"],
        end_hz=profile["sweep_end_hz"])
    signal = np.concatenate([
        np.zeros(round(rate * (profile["record_preroll_seconds"] + profile["lead_in_seconds"]))),
        sweep["analysis_sweep"],
        np.zeros(round(rate * (profile["tail_seconds"] + profile["record_postroll_seconds"]))),
    ])
    size = 1 << (signal.size - 1).bit_length()
    frequencies = np.fft.rfftfreq(size, 1. / rate)
    z = np.exp(-2j * np.pi * frequencies / rate)
    response = np.ones_like(z)
    for spec in way_crossover_specs(model):
        for b0, b1, b2, _, a1, a2 in design_crossover(spec, rate):
            response *= (b0 + b1 * z + b2 * z * z) / (1 + a1 * z + a2 * z * z)
    filtered = np.fft.irfft(np.fft.rfft(signal, n=size) * response, n=size)[:signal.size]
    mic = np.zeros_like(filtered)
    mic[96:] = filtered[:-96] * 10 ** (gain_db / 20)
    echo = round(rate * 0.0071)
    mic[echo:] += reflection * mic[:-echo].copy()
    mic += np.random.default_rng(42).normal(0, noise, mic.size)
    path = store.captures_dir / "dry.wav"
    store._write_wav(path, np.column_stack([mic, filtered * 0.25]), rate)
    store._last_successful_lag = None
    analysis = store._analyzer._analyze_sweep_capture(
        path, expected_sample_rate=rate, channel="left", reference_sweep=sweep["analysis_sweep"],
        inverse_sweep=sweep["inverse_sweep"], calibration_curve=calibration,
        reference_channel_index=1, analysis_channel_index=0,
        reference_channel_label="input_2_electrical_reference", band_limited_reference=True)
    verdict = store._evaluate_electrical_reference_status(analysis)
    level = estimate_way_level({"analysis": analysis}, way_passband(model, sample_rate_hz=rate),
                               processing=model, sample_rate_hz=rate)
    return analysis, verdict, level


class SpeakerRuntimeTests(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory(prefix="speaker-runtime-")
        self.addCleanup(home.cleanup)
        environment = patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(Path(home.name) / "config"),
            "XDG_STATE_HOME": str(Path(home.name) / "state"),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def test_way_profiles_cover_passbands_without_reducing_time_per_octave(self):
        default = default_measurement_sweep_profile()
        seconds_per_octave = 11. / math.log2(22000. / 10.)
        for rate in (44100, 48000, 96000):
            for ways, cutoffs in ((('low', 'high'), (3000,)), (('low', 'mid', 'high'), (400, 3000))):
                state, channels = state_for(ways, cutoffs)
                alignment, _ = alignment_for(state, channels, rate=rate)
                profiles = []
                for role, model in alignment.way_models().items():
                    with self.subTest(rate=rate, role=role, ways=len(ways)):
                        profile = profile_for(model, rate)
                        low, high = way_passband(model, sample_rate_hz=rate)
                        self.assertLessEqual(profile["sweep_start_hz"], low / 2)
                        self.assertGreaterEqual(profile["sweep_end_hz"], min(high * 2, 22000.))
                        self.assertLess(profile["sweep_end_hz"], rate / 2)
                        octaves = math.log2(profile["sweep_end_hz"] / profile["sweep_start_hz"])
                        self.assertGreaterEqual(profile["sweep_seconds"] / octaves, seconds_per_octave - 1e-9)
                        self.assertGreaterEqual(profile["sweep_seconds"], 2.)
                        merged = {**default, **profile}
                        for key in ("lead_in_seconds", "tail_seconds", "record_preroll_seconds", "record_postroll_seconds"):
                            self.assertEqual(merged[key], default[key])
                        profiles.append(profile)
                self.assertLess(sum(p["sweep_seconds"] for p in profiles), 8 * len(ways))

    def test_shortened_way_captures_preserve_absolute_calibrated_levels_and_reference_quality(self):
        calibration = (np.array([20., 200., 3000., 22000.]), np.array([-1., 0.4, -0.5, 1.2]))
        with tempfile.TemporaryDirectory() as home:
            store = MeasurementStore(home=Path(home))
            for ways, cutoffs, rate in ((('low', 'high'), (3000,), 48000),
                                        (('low', 'mid', 'high'), (400, 3000), 44100)):
                state, channels = state_for(ways, cutoffs)
                alignment, _ = alignment_for(state, channels, rate=rate)
                for role, model in alignment.way_models().items():
                    with self.subTest(rate=rate, role=role):
                        profile = profile_for(model, rate)
                        self.assertLessEqual(profile["sweep_seconds"], 11.)
                        common = dict(rate=rate, gain_db=-30., reflection=0.2, noise=0.00001,
                                      calibration=calibration)
                        full, full_reference, full_level = synthetic_way_capture(store, model, {}, **common)
                        short, short_reference, short_level = synthetic_way_capture(store, model, profile, **common)
                        self.assertTrue(full_reference["usable"], full_reference)
                        self.assertTrue(short_reference["usable"], short_reference)
                        # These ways are timed against the reference peak, as always.
                        for take in (full, short):
                            self.assertEqual(take["impulse_response"]["timing_source"],
                                             "direct_arrival_minus_reference_peak")
                        self.assertAlmostEqual(short_level["level_db"], full_level["level_db"], delta=0.1)
                        self.assertEqual(short_level["point_count"], full_level["point_count"])
                        self.assertFalse(short["clock"]["magnitude_drift_resampling_applied"])

    def test_narrow_bass_way_keeps_full_profile_and_reference_admission(self):
        state, channels = state_for(('low', 'mid', 'high'), (200, 1200))
        alignment, _ = alignment_for(state, channels)
        model = alignment.way_models()["left_low"]
        profile = profile_for(model, 48000)
        with tempfile.TemporaryDirectory() as home:
            store = MeasurementStore(home=Path(home))
            full, full_reference, full_level = synthetic_way_capture(
                store, model, {}, reflection=0.2, noise=0.00001)
            short, short_reference, short_level = synthetic_way_capture(
                store, model, profile, reflection=0.2, noise=0.00001)
            self.assertTrue(full_reference["usable"], full_reference)
            self.assertTrue(short_reference["usable"], short_reference)
            self.assertEqual(short_level, full_level)
            self.assertEqual(short["impulse_response"], full["impulse_response"])
            self.assertEqual(profile["sweep_seconds"], 11.)

    def test_open_edged_way_takes_the_band_profile_and_keeps_its_reference(self):
        # A 3-way low way without a bass-management high-pass has only its
        # 120 Hz low-pass, so its flat passband ends at the 50 Hz measurement
        # floor (50-71 Hz): narrow on paper, open towards the bottom. On .104
        # it took the full sweep and lost the electrical reference; it takes
        # the band profile a 2-way low way takes. A high way above 10 kHz is
        # the same case at the top edge.
        def spec(kind, frequency_hz):
            return {"kind": kind, "family": "linkwitz-riley", "slope_db_oct": 24,
                    "frequency_hz": frequency_hz}

        with tempfile.TemporaryDirectory() as home:
            store = MeasurementStore(home=Path(home))
            for model in ({"crossover": [spec("lowpass", 120)]}, {"crossover": [spec("highpass", 10000)]}):
                with self.subTest(model=model["crossover"][0]):
                    low, high = way_passband(model, sample_rate_hz=48000)
                    self.assertLess(math.log2(high / low), 0.5)
                    profile = profile_for(model, 48000)
                    self.assertLess(profile["sweep_seconds"], 11.)
                    self.assertLessEqual(profile["sweep_start_hz"], low / 2)
                    self.assertGreaterEqual(profile["sweep_end_hz"], min(high * 2, 22000.))
                    full, full_reference, _ = synthetic_way_capture(
                        store, model, {}, reflection=0.2, noise=0.00001)
                    short, short_reference, _ = synthetic_way_capture(
                        store, model, profile, reflection=0.2, noise=0.00001)
                    self.assertTrue(short_reference["usable"], short_reference)
                    self.assertGreaterEqual(short["impulse_response"]["arrival_samples"], 0)
                    if model["crossover"][0]["kind"] == "lowpass":
                        # The full sweep spreads the deconvolution over a band
                        # the way does not play; its arrival drowns in that noise.
                        self.assertFalse(full_reference["usable"])
                        self.assertEqual(short["impulse_response"]["timing_source"],
                                         "direct_arrival_minus_reference_arrival")


if __name__ == "__main__":
    unittest.main()
