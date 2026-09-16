#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Crossover coefficient design: cutoff levels, stability, and valid orders."""

import cmath
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dsp.crossover import crossover_magnitude_db, design_crossover


def lowpass(kind):
    return {"kind": "lowpass", "family": kind[0], "slope_db_oct": kind[1], "frequency_hz": 2000}


class CrossoverDesignTests(unittest.TestCase):
    def test_known_cutoff_levels(self):
        rate = 48000
        for family, slope, expected in (
            ("butterworth", 12, -3.0103), ("butterworth", 24, -3.0103),
            ("bessel", 12, -3.0103), ("bessel", 24, -3.0103),
            ("linkwitz-riley", 12, -6.0206), ("linkwitz-riley", 24, -6.0206),
            ("linkwitz-riley", 72, -6.0206),
        ):
            for kind in ("lowpass", "highpass"):
                spec = {"kind": kind, "family": family, "slope_db_oct": slope, "frequency_hz": 2000}
                level = crossover_magnitude_db(design_crossover(spec, rate), 2000, rate)
                with self.subTest(family=family, slope=slope, kind=kind):
                    self.assertAlmostEqual(level, expected, delta=0.05)

    def test_passband_unity_and_stopband_asymptote(self):
        rate = 48000
        sections = design_crossover(lowpass(("butterworth", 24)), rate)
        self.assertAlmostEqual(crossover_magnitude_db(sections, 100, rate), 0.0, delta=0.05)
        # 24 dB/oct over ~3.3 octaves (2 kHz -> 20 kHz is threshold-limited; use 250 Hz lowpass instead)
        sections = design_crossover({**lowpass(("butterworth", 24)), "frequency_hz": 250}, rate)
        self.assertAlmostEqual(crossover_magnitude_db(sections, 8000, rate), -120.0, delta=6.0)
        sections = design_crossover({"kind": "highpass", "family": "butterworth",
                                     "slope_db_oct": 24, "frequency_hz": 8000}, rate)
        self.assertAlmostEqual(crossover_magnitude_db(sections, 20000, rate), 0.0, delta=0.1)

    def test_section_counts_follow_order(self):
        rate = 48000
        for family, slope, count in (("butterworth", 6, 1), ("butterworth", 12, 1),
                                     ("butterworth", 18, 2), ("butterworth", 72, 6),
                                     ("linkwitz-riley", 12, 2), ("linkwitz-riley", 24, 2),
                                     ("linkwitz-riley", 36, 4), ("linkwitz-riley", 72, 6)):
            with self.subTest(family=family, slope=slope):
                self.assertEqual(len(design_crossover(lowpass((family, slope)), rate)), count)

    def test_digital_poles_are_stable_at_all_rates(self):
        for rate in (44100, 48000, 96000, 192000, 384000):
            for family, slope in (("butterworth", 72), ("bessel", 72), ("linkwitz-riley", 72)):
                for kind in ("lowpass", "highpass"):
                    spec = {"kind": kind, "family": family, "slope_db_oct": slope, "frequency_hz": 2000}
                    for section in design_crossover(spec, rate):
                        b0, b1, b2, _, a1, a2 = section
                        for pole in self._poles(a1, a2):
                            with self.subTest(rate=rate, family=family, kind=kind):
                                self.assertLess(abs(pole), 1.0 - 1e-9)

    @staticmethod
    def _poles(a1, a2):
        if a2 == 0.0:
            return (-a1,)
        root = cmath.sqrt(a1 * a1 - 4.0 * a2)
        return ((-a1 + root) / 2.0, (-a1 - root) / 2.0)

    def test_linkwitz_riley_sums_flat_with_order_appropriate_polarity(self):
        rate = 48000
        for slope in (12, 24, 36, 48, 60, 72):
            # LR cascades two Butterworth halves: odd half-orders (12/36/60)
            # sum in-phase only with one side inverted, even ones without.
            invert_high = (slope // 12) % 2 == 1
            levels = []
            for frequency in (500, 1000, 2000, 4000, 8000):
                total = 0j
                for kind in ("lowpass", "highpass"):
                    spec = {"kind": kind, "family": "linkwitz-riley",
                            "slope_db_oct": slope, "frequency_hz": 2000}
                    response = self._response(design_crossover(spec, rate), frequency, rate)
                    total += -response if kind == "highpass" and invert_high else response
                levels.append(20.0 * math.log10(abs(total)))
            with self.subTest(slope=slope):
                for level in levels:
                    self.assertAlmostEqual(level, 0.0, delta=0.15)

    @staticmethod
    def _response(sections, frequency, rate):
        z = cmath.exp(2j * math.pi * frequency / rate)
        total = 1j
        total = 1.0 + 0.0j
        for b0, b1, b2, _, a1, a2 in sections:
            total *= (b0 + b1 / z + b2 / z / z) / (1.0 + a1 / z + a2 / z / z)
        return total

    def test_invalid_specs_fail(self):
        rate = 48000
        good = lowpass(("butterworth", 12))
        for spec, sample_rate in (
            ({**good, "kind": "bandpass"}, rate),
            ({**good, "family": "LR24"}, rate),
            ({**good, "slope_db_oct": 20}, rate),
            ({**good, "family": "linkwitz-riley", "slope_db_oct": 18}, rate),
            ({**good, "slope_db_oct": True}, rate),
            ({**good, "frequency_hz": float("nan")}, rate),
            ({**good, "frequency_hz": 24000}, rate),
            ({**good, "extra": 1}, rate),
            ("lowpass", rate),
            (good, 0),
            (good, True),
            (good, 385000),
        ):
            with self.subTest(spec=spec, rate=sample_rate), self.assertRaises(ValueError):
                design_crossover(spec, sample_rate)


if __name__ == "__main__":
    unittest.main()
