#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Reference timing of a deliberately band-limited way.

A Speaker-Align way (or a bank measurement) plays one band of the sweep.  Its
electrical reference carries exactly that band, so the registers the way does
not reproduce must not decide whether the reference is usable.  These checks
pin the register selection and the matching electrical-reference verdict on
real synthetic captures.
"""

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dsp.crossover import design_crossover
from measurement.constants import SWEEP_TIMING_ANCHOR_LAYOUT
from measurement.signal import build_inverse_sweep, generate_log_sweep
from measurement.store import CaptureQualityError, MeasurementStore

RATE = 48000
SWEEP_SECONDS = 4.0
SWEEP_START_HZ = 10.0
SWEEP_END_HZ = 22000.0


def matches_for(score_by_name, *, level_by_name=None, delay_samples=100):
    """Anchor matches in sweep order, with one constant delay and no jitter."""
    levels = level_by_name or {}
    matches = []
    for name, fraction in SWEEP_TIMING_ANCHOR_LAYOUT:
        offset = int(fraction * RATE * SWEEP_SECONDS)
        matches.append({
            "name": name,
            "offset_samples": offset,
            "expected_start": offset + delay_samples,
            "observed_start": offset + delay_samples,
            "residual_samples": delay_samples,
            "score": float(score_by_name[name]),
            "raw_score": float(score_by_name[name]),
            "polarity": 1.0,
            "level_dbfs": float(levels.get(name, -20.0)),
        })
    return matches


def scores(**overrides):
    """A reproduced sweep register by default; overrides silence registers."""
    values = {name: 0.95 for name, _ in SWEEP_TIMING_ANCHOR_LAYOUT}
    values.update(overrides)
    return values


def apply_band(signal, specs):
    """Apply crossover sections in the frequency domain (same response formula)."""
    size = 1 << (2 * signal.size).bit_length()
    spectrum = np.fft.rfft(signal.astype(np.float64), n=size)
    frequencies = np.fft.rfftfreq(size, 1.0 / RATE)
    z = np.exp(-2j * np.pi * frequencies / RATE)
    response = np.ones_like(frequencies, dtype=complex)
    for spec in specs:
        for b0, b1, b2, _, a1, a2 in design_crossover(spec, RATE):
            response *= (b0 + b1 * z + b2 * z * z) / (1.0 + a1 * z + a2 * z * z)
    return np.fft.irfft(spectrum * response, n=size)[: signal.size]


def crossover_spec(kind, frequency_hz):
    return {"kind": kind, "family": "linkwitz-riley", "slope_db_oct": 24,
            "frequency_hz": frequency_hz}


class RegionAnchorSelectionTests(unittest.TestCase):
    """Which anchors judge the start/end alignment of one capture."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory(prefix="band-limited-")
        self.addCleanup(self._directory.cleanup)
        self.store = MeasurementStore(home=Path(self._directory.name))

    def fit(self, score_by_name, *, band_limited, level_by_name=None):
        return self.store._analyzer._fit_sweep_timing_from_matches(
            matches=matches_for(score_by_name, level_by_name=level_by_name),
            reference_sweep_samples=int(RATE * SWEEP_SECONDS),
            sample_rate=RATE,
            allow_drift_compensation=False,
            band_limited=band_limited,
        )

    def test_full_band_capture_is_unchanged_by_the_band_limited_flag(self):
        full = scores()
        legacy = self.fit(full, band_limited=False)
        flagged = self.fit(full, band_limited=True)
        self.assertAlmostEqual(legacy["start_score"], flagged["start_score"], places=6)
        self.assertAlmostEqual(legacy["end_score"], flagged["end_score"], places=6)
        self.assertEqual(legacy["region_anchor_sources"],
                         {"start": "registers", "end": "registers", "band_limited": False})
        self.assertEqual(flagged["region_anchor_sources"]["start"], "reproduced-registers")
        self.assertEqual(flagged["region_anchor_sources"]["end"], "reproduced-registers")

    def test_high_pass_way_is_judged_on_the_registers_it_plays(self):
        # A way above ~2.5 kHz reproduces no start register at all.
        high = scores(**{"start-inner": 0.0, "start-body": 0.0, "mid-low": 0.0, "mid-high": 0.02,
                         "end-body": 0.93, "end-inner": 0.91})
        legacy = self.fit(high, band_limited=False)
        self.assertEqual(legacy["start_score"], 0.0)
        flagged = self.fit(high, band_limited=True)
        self.assertAlmostEqual(flagged["start_score"], 0.92, places=6)
        self.assertAlmostEqual(flagged["end_score"], 0.92, places=6)
        self.assertEqual(flagged["region_anchor_sources"]["start"], "earliest-reproduced-registers")
        self.assertEqual(flagged["region_anchor_sources"]["end"], "reproduced-registers")

    def test_low_pass_way_is_judged_on_the_registers_it_plays(self):
        # A 20 Hz - 2.5 kHz way keeps the start registers and mid-high, but
        # none of its own high registers.
        low = scores(**{"end-body": 0.0, "end-inner": 0.0,
                        "start-inner": 0.92, "start-body": 0.99, "mid-low": 0.99})
        flagged = self.fit(low, band_limited=True)
        self.assertAlmostEqual(flagged["start_score"], 0.99, places=6)
        self.assertAlmostEqual(flagged["end_score"], 0.95, places=6)
        self.assertEqual(flagged["region_anchor_sources"]["start"], "reproduced-registers")
        self.assertEqual(flagged["region_anchor_sources"]["end"], "reproduced-registers")

    def test_sub_only_way_uses_its_own_registers_for_both_end_scores(self):
        sub = scores(**{"mid-low": 0.0, "mid-high": 0.0,
                        "end-body": 0.0, "end-inner": 0.0,
                        "start-inner": 0.92, "start-body": 0.99})
        flagged = self.fit(sub, band_limited=True)
        self.assertAlmostEqual(flagged["start_score"], 0.955, places=6)
        self.assertAlmostEqual(flagged["end_score"], 0.955, places=6)
        self.assertEqual(flagged["region_anchor_sources"]["end"], "latest-reproduced-registers")

    def test_capture_without_any_reproduced_register_scores_zero(self):
        silent = scores(**{name: 0.0 for name, _ in SWEEP_TIMING_ANCHOR_LAYOUT})
        flagged = self.fit(silent, band_limited=True)
        self.assertEqual(flagged["start_score"], 0.0)
        self.assertEqual(flagged["end_score"], 0.0)
        self.assertEqual(flagged["region_anchor_sources"]["start"], "no-reproduced-register")
        self.assertEqual(flagged["region_anchor_sources"]["end"], "no-reproduced-register")

    def test_coherent_residue_far_below_the_take_is_not_a_reproduced_register(self):
        # A steep filter leaves a coherent but tiny residue of a register the
        # way does not play; its level, not its correlation, decides.
        high = scores(**{"start-inner": 0.0, "start-body": 0.0, "mid-low": 0.81, "mid-high": 0.94,
                         "end-body": 0.94, "end-inner": 0.80})
        levels = {"mid-low": -96.0, "start-inner": -100.0}
        flagged = self.fit(high, band_limited=True, level_by_name=levels)
        # No start register survives, so the earliest reproduced registers at
        # the same end of the sweep judge the start alignment.
        self.assertAlmostEqual(flagged["start_score"], 0.94, places=6)
        self.assertEqual(flagged["region_anchor_sources"]["start"], "earliest-reproduced-registers")
        rows = {row["name"]: row for row in flagged["anchor_matches"]}
        self.assertFalse(rows["mid-low"]["band_present"])
        self.assertTrue(rows["mid-high"]["band_present"])

    def test_reproduced_registers_are_recorded_per_anchor(self):
        high = scores(**{"start-inner": 0.0, "start-body": 0.0, "mid-low": 0.0, "mid-high": 0.02})
        rows = {row["name"]: row for row in self.fit(high, band_limited=True)["anchor_matches"]}
        self.assertFalse(rows["mid-low"]["band_present"])
        self.assertFalse(rows["mid-high"]["band_present"])
        self.assertTrue(rows["end-body"]["band_present"])


class BandLimitedReferenceCaptureTests(unittest.TestCase):
    """Real analyzer runs on synthesized band-limited way captures."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory(prefix="band-limited-capture-")
        self.addCleanup(self._directory.cleanup)
        self.store = MeasurementStore(home=Path(self._directory.name))
        self.sweep = generate_log_sweep(RATE, SWEEP_SECONDS, SWEEP_START_HZ, SWEEP_END_HZ, peak_scale=0.8)
        self.inverse = build_inverse_sweep(
            self.sweep, RATE, SWEEP_SECONDS, SWEEP_START_HZ, SWEEP_END_HZ)

    def capture_path(self, specs, *, name):
        played = apply_band(self.sweep, specs)
        reference = np.concatenate([np.zeros(120), played, np.zeros(RATE // 2)])
        mic = np.zeros_like(reference)
        delay = 240
        mic[delay:] = reference[:-delay] * 0.5
        path = self.store.captures_dir / f"{name}.wav"
        self.store._write_wav(path, np.column_stack([mic, reference]), RATE)
        return path

    def analyze(self, path, *, band_limited):
        return self.store._analyzer._analyze_sweep_capture(
            path,
            expected_sample_rate=RATE,
            channel="left",
            reference_sweep=self.sweep,
            inverse_sweep=self.inverse,
            calibration_curve=None,
            reference_channel_index=1,
            analysis_channel_index=0,
            reference_channel_label="input_8_electrical_reference",
            band_limited_reference=band_limited,
        )

    def assert_usable_reference(self, path, *, band_limited, start_source=None):
        analysis = self.analyze(path, band_limited=band_limited)
        clock = analysis["clock"]
        self.assertGreaterEqual(clock["start_score"], 0.84)
        self.assertGreaterEqual(clock["end_score"], 0.84)
        self.assertEqual(clock["band_limited_reference"], band_limited)
        if start_source is not None:
            self.assertEqual(clock["region_anchor_sources"]["start"], start_source)
        status = self.store._evaluate_electrical_reference_status(analysis)
        self.assertTrue(status["usable"], status)
        self.assertTrue(analysis["reference_path"]["electrical_reference_used"])
        self.assertEqual(analysis["reference_path"]["timing_status"], "electrical-reference")
        return analysis

    def test_high_way_reference_is_admitted_only_with_band_aware_timing(self):
        path = self.capture_path([crossover_spec("highpass", 2500.0)], name="high-way")
        # The legacy verdict is exactly what failed on .104: no start register
        # of the played band exists, so the start score collapses to zero.
        with self.assertRaises(CaptureQualityError) as raised:
            self.analyze(path, band_limited=False)
        self.assertIn("start alignment score was too weak", str(raised.exception))
        self.assert_usable_reference(path, band_limited=True,
                                     start_source="earliest-reproduced-registers")

    def test_low_way_and_band_pass_way_references_stay_admitted(self):
        low = self.capture_path([crossover_spec("lowpass", 2500.0)], name="low-way")
        analysis = self.assert_usable_reference(low, band_limited=True,
                                                start_source="reproduced-registers")
        self.assertEqual(analysis["clock"]["region_anchor_sources"]["end"], "reproduced-registers")
        band_pass = self.capture_path(
            [crossover_spec("highpass", 300.0), crossover_spec("lowpass", 2500.0)], name="mid-way")
        self.assert_usable_reference(band_pass, band_limited=True,
                                     start_source="reproduced-registers")

    def test_full_band_way_reference_is_identical_with_and_without_the_flag(self):
        path = self.capture_path([], name="full-band")
        legacy = self.analyze(path, band_limited=False)
        flagged = self.assert_usable_reference(path, band_limited=True,
                                               start_source="reproduced-registers")
        self.assertAlmostEqual(legacy["clock"]["start_score"], flagged["clock"]["start_score"], places=5)
        self.assertAlmostEqual(legacy["clock"]["end_score"], flagged["clock"]["end_score"], places=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
