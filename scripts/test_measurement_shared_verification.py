#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""One shared verification take must see a staged way delay.

Speaker Align judges its trial from one take in which every way of a speaker
side plays at once.  The decisive property is that the residual is the real
acoustic low/high offset on that take's single time base: a staged way delay
has to appear in it in full.  This is the exact quantity the per-way
microphone-minus-own-electrical-reference measurement cannot see, because
there the reference is tapped after the way's delay and moves with it.

The synthetic takes below render the way signals causally, the way the engine
does, so a take holds the structure the deconvolved sweep capture produces:
each way's own crossover response plus its arrival.

The ways of one crossover overlap by an octave and are equally loud there, and
nothing in a single take separates them in that overlap.  The last class pins
that the isolation step weights each way by the share of the take it owns, so a
quieter way still lands on its own arrival, and that an unseparable take is
reported instead of quietly confirming.
"""

import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dsp.crossover import design_crossover
from measurement.speaker_verification import (
    MIN_WAY_ISOLATION_DB,
    band_arrival,
    band_level,
    side_confirmation,
    way_band_impulse_response,
)

RATE = 48000
TAKE_SAMPLES = 32768
ORIGIN_SAMPLES = 8192
CROSSOVER_HZ = 2000.0
REFLECTIONS = ((0.0031, 0.35), (0.0072, 0.22))
# The known left case: the low way arrives ~7.54 ms after the high way.
GEOMETRY_MS = {"left_low": 7.5417, "left_high": 0.0}


def way(kind):
    spec = {"family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": CROSSOVER_HZ}
    return {kind: spec}


PROCESSING = {"left_low": way("lowpass"), "left_high": way("highpass")}
ROLES = ("left_low", "left_high")
# Driver level spread a real take may carry: a quieter way must still land on
# its own arrival.
QUIET_WAY_DB = -12.0


def way_response(processing, role):
    """Causal frequency response of one way's own crossover filters."""
    frequencies = np.fft.rfftfreq(TAKE_SAMPLES, 1.0 / RATE)
    z = np.exp(-2j * np.pi * frequencies / RATE)
    response = np.ones_like(frequencies, dtype=complex)
    for kind in ("highpass", "lowpass"):
        spec = processing[role].get(kind)
        if spec is None:
            continue
        for b0, b1, b2, _, a1, a2 in design_crossover({**spec, "kind": kind}, RATE):
            response *= (b0 + b1 * z + b2 * z * z) / (1.0 + a1 * z + a2 * z * z)
    return response


def synthetic_take(arrival_ms, *, gains_db=None, reflections=REFLECTIONS, roles=ROLES,
                   processing=None):
    """One microphone IR holding every way's causal response at its arrival.

    ``arrival_ms`` is the total arrival of a way: acoustic geometry plus the
    way's applied delay, exactly what the engine renders into one take.
    """
    gains_db = gains_db or {}
    processing = processing or PROCESSING
    frequencies = np.fft.rfftfreq(TAKE_SAMPLES, 1.0 / RATE)
    spectrum = np.zeros_like(frequencies, dtype=complex)
    for role in roles:
        delay_samples = ORIGIN_SAMPLES + int(round(arrival_ms[role] * RATE / 1000.0))
        echo = np.ones_like(frequencies, dtype=complex)
        for seconds, gain in reflections:
            echo += gain * np.exp(-2j * np.pi * frequencies * seconds)
        gain = 10.0 ** (float(gains_db.get(role, 0.0)) / 20.0)
        spectrum += gain * way_response(processing, role) * np.exp(
            -2j * np.pi * frequencies * delay_samples / RATE) * echo
    return np.fft.irfft(spectrum, n=TAKE_SAMPLES)


def confirm_take(take, **kwargs):
    options = dict(impulse_response=take, processing=PROCESSING, roles=ROLES,
                   sample_rate_hz=RATE, start_revision=7, processing_fingerprint="frozen-plan")
    options.update(kwargs)
    return side_confirmation(**options)


def residual_ms(take, **kwargs):
    document = confirm_take(take, **kwargs)
    values = list(document["arrival_ms"].values())
    return document, max(values) - min(values)


def arrival_index(role, arrival_ms):
    return ORIGIN_SAMPLES + int(round(arrival_ms * RATE / 1000.0))


class SharedVerificationResidualTests(unittest.TestCase):
    """A staged way delay must show up as the take's low/high offset."""

    def test_uncompensated_geometry_reads_as_the_full_offset(self):
        document, residual = residual_ms(synthetic_take(GEOMETRY_MS))
        self.assertAlmostEqual(residual, 7.5417, delta=0.05)
        self.assertAlmostEqual(document["arrival_ms"]["left_high"], 0.0, delta=0.05)
        self.assertAlmostEqual(document["arrival_ms"]["left_low"], 7.5417, delta=0.05)

    def test_staged_delay_compensates_the_offset_within_the_gate(self):
        document, residual = residual_ms(synthetic_take({"left_low": 7.5417, "left_high": 7.5417}))
        self.assertLessEqual(residual, 0.25)
        self.assertLessEqual(residual, 0.02)

    def test_restoring_the_delay_brings_the_offset_back(self):
        applied = residual_ms(synthetic_take({"left_low": 7.5417, "left_high": 7.5417}))[1]
        restored = residual_ms(synthetic_take(GEOMETRY_MS))[1]
        self.assertLessEqual(applied, 0.25)
        self.assertAlmostEqual(restored, 7.5417, delta=0.05)

    def test_residual_follows_a_partially_applied_delay(self):
        _, residual = residual_ms(synthetic_take({"left_low": 7.5417, "left_high": 4.0000}))
        self.assertAlmostEqual(residual, 3.5417, delta=0.05)

    def test_residual_tracks_small_offsets(self):
        _, residual = residual_ms(synthetic_take({"left_low": 1.0, "left_high": 0.25}))
        self.assertAlmostEqual(residual, 0.75, delta=0.05)

    def test_residual_ignores_way_gain_differences(self):
        for gain in (QUIET_WAY_DB, 6.0, 20.0):
            with self.subTest(gain_db=gain):
                _, residual = residual_ms(synthetic_take(GEOMETRY_MS, gains_db={"left_low": gain}))
                self.assertAlmostEqual(residual, 7.5417, delta=0.05)

    def test_take_survives_room_reflections(self):
        for reflections in ((), ((0.0031, 0.15),), ((0.0031, 0.6), (0.0072, 0.45))):
            with self.subTest(reflections=reflections):
                _, residual = residual_ms(synthetic_take(GEOMETRY_MS, reflections=reflections))
                self.assertAlmostEqual(residual, 7.5417, delta=0.05)
                aligned, spread = residual_ms(
                    synthetic_take({"left_low": 3.0, "left_high": 3.0}, reflections=reflections))
                self.assertLessEqual(spread, 0.02)
                self.assertIsNone(aligned["way_isolation_db"]["left_low"])

    def test_document_reports_identity_levels_and_isolation(self):
        document, _ = residual_ms(synthetic_take(GEOMETRY_MS, gains_db={"left_high": -6.0}))
        self.assertEqual(document["start_revision"], 7)
        self.assertEqual(document["processing_fingerprint"], "frozen-plan")
        self.assertEqual(document["arrival_origin"], "earliest-way-of-take")
        self.assertEqual(document["level_origin"], "loudest-way-of-take")
        self.assertTrue(document["band_limited"])
        self.assertAlmostEqual(document["way_levels_db"]["left_low"], 0.0, delta=0.5)
        self.assertAlmostEqual(document["way_levels_db"]["left_high"], -6.0, delta=1.0)
        # Ways that arrive apart must be separable well beyond the gate.
        for role, margin in document["way_isolation_db"].items():
            self.assertGreater(margin, MIN_WAY_ISOLATION_DB)
        for role in ROLES:
            band = document["bands"][role]
            self.assertEqual(band["arrival_index"], arrival_index(role, document["arrival_ms"][role]))
            self.assertLessEqual(band["onset_index"], band["arrival_index"])

    def test_aligned_take_reports_no_isolation_margin(self):
        document, _ = residual_ms(synthetic_take({"left_low": 4.0, "left_high": 4.0}))
        self.assertEqual(document["way_isolation_db"], {"left_low": None, "left_high": None})

    def test_coincident_arrivals_are_not_reported_as_apart(self):
        document, _ = residual_ms(synthetic_take({"left_low": 3.0, "left_high": 3.0002}))
        self.assertIsNone(document["way_isolation_db"]["left_low"])

    def test_a_way_too_quiet_to_locate_is_reported_by_its_level(self):
        # 40 dB down, the quiet way's isolated band is really the neighbour's
        # leak: its arrival collapses onto the neighbour's, so the take has to
        # be caught by the level spread, never confirmed.
        document, residual = residual_ms(synthetic_take(GEOMETRY_MS, gains_db={"left_high": -40.0}))
        self.assertLessEqual(residual, 0.02)
        self.assertGreater(abs(document["way_levels_db"]["left_high"]), 20.0)

    def test_barely_separable_way_is_reported_by_its_isolation(self):
        # A 12 dB quieter low way is still located on its own arrival, but only
        # just: the margin has to be reported below the gate, never confirmed.
        document, residual = residual_ms(synthetic_take(GEOMETRY_MS, gains_db={"left_low": -12.0}))
        self.assertAlmostEqual(residual, 7.5417, delta=0.05)
        self.assertLess(document["way_isolation_db"]["left_low"], MIN_WAY_ISOLATION_DB)
        self.assertLess(document["way_isolation_db"]["left_low"], 10.0)
        self.assertAlmostEqual(document["way_levels_db"]["left_low"], -11.5, delta=1.0)


class BandIsolationTests(unittest.TestCase):
    """Each band is recovered with its own crossover and its owned share."""

    def margin_db(self, band, own_index, foreign_index):
        energy = np.square(np.abs(band))
        return 10.0 * math.log10(float(energy[own_index]) / max(float(energy[foreign_index]), 1e-300))

    def test_owned_share_keeps_a_quiet_way_on_its_own_arrival(self):
        take = synthetic_take(GEOMETRY_MS, gains_db={"left_low": QUIET_WAY_DB})
        own = arrival_index("left_low", GEOMETRY_MS["left_low"])
        unweighted = way_band_impulse_response(take, PROCESSING["left_low"], sample_rate_hz=RATE)
        weighted = way_band_impulse_response(
            take, PROCESSING["left_low"], sample_rate_hz=RATE,
            foreign=[PROCESSING["left_high"]])
        # Both stay on the real arrival here, but the shared overlap costs the
        # unweighted filter several dB of margin against the neighbour: that is
        # what the owned share buys back.
        self.assertLessEqual(abs(band_arrival(unweighted, sample_rate_hz=RATE)["arrival_index"] - own), 2)
        self.assertEqual(band_arrival(weighted, sample_rate_hz=RATE)["arrival_index"], own)
        self.assertGreater(self.margin_db(weighted, own, ORIGIN_SAMPLES)
                           - self.margin_db(unweighted, own, ORIGIN_SAMPLES), 2.0)

    def test_a_much_quieter_way_is_pulled_onto_its_neighbour_without_the_share(self):
        take = synthetic_take(GEOMETRY_MS, gains_db={"left_low": -20.0})
        unweighted = way_band_impulse_response(take, PROCESSING["left_low"], sample_rate_hz=RATE)
        self.assertEqual(band_arrival(unweighted, sample_rate_hz=RATE)["arrival_index"],
                         ORIGIN_SAMPLES)

    def test_foreign_ways_must_be_a_sequence_of_ways(self):
        take = synthetic_take(GEOMETRY_MS)
        for foreign in (PROCESSING["left_high"], "left_high"):
            with self.subTest(foreign=foreign), self.assertRaises(ValueError):
                way_band_impulse_response(take, PROCESSING["left_low"],
                                          sample_rate_hz=RATE, foreign=foreign)

    def test_isolation_power_must_stay_in_range(self):
        take = synthetic_take(GEOMETRY_MS)
        for power in (0.5, 7.0, float("nan")):
            with self.subTest(power=power), self.assertRaises(ValueError):
                way_band_impulse_response(take, PROCESSING["left_low"],
                                          sample_rate_hz=RATE, isolation_power=power)

    def test_way_without_crossover_filters_is_rejected(self):
        take = synthetic_take(GEOMETRY_MS)
        with self.assertRaisesRegex(ValueError, "no crossover filters"):
            way_band_impulse_response(take, {}, sample_rate_hz=RATE)

    def test_level_estimate_reports_the_way_it_measured(self):
        take = synthetic_take(GEOMETRY_MS, gains_db={"left_low": QUIET_WAY_DB})
        low = way_band_impulse_response(take, PROCESSING["left_low"], sample_rate_hz=RATE)
        high = way_band_impulse_response(take, PROCESSING["left_high"], sample_rate_hz=RATE)
        low_level = band_level(low, (40.0, 1200.0), sample_rate_hz=RATE)
        high_level = band_level(high, (5000.0, 16000.0), sample_rate_hz=RATE)
        self.assertAlmostEqual(low_level["level_db"] - high_level["level_db"], QUIET_WAY_DB, delta=1.0)


class ArrivalEstimatorTests(unittest.TestCase):
    """The arrival is the strongest energy of the isolated band itself."""

    def band(self, *peaks):
        values = np.zeros(8192)
        for index, amplitude in peaks:
            values[index] = amplitude
        return values

    def test_silent_band_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "silent"):
            band_arrival(np.zeros(8192), sample_rate_hz=RATE)

    def test_arrival_is_the_strongest_energy_not_the_leading_leak(self):
        arrival = band_arrival(self.band((1000, 0.2), (1400, 1.0)), sample_rate_hz=RATE)
        self.assertEqual(arrival["arrival_index"], 1400)
        self.assertLess(arrival["onset_index"], 1400)

    def test_arrival_ignores_a_quiet_leading_lobe(self):
        arrival = band_arrival(self.band((1000, 1e-3), (5000, 1.0)), sample_rate_hz=RATE)
        self.assertEqual(arrival["arrival_index"], 5000)
        self.assertNotEqual(arrival["onset_index"], 1000)

    def test_threshold_must_be_a_fraction(self):
        for threshold in (0.0, 1.0, -0.2, float("inf")):
            with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                band_arrival(self.band((1000, 1.0)), sample_rate_hz=RATE,
                             threshold_relative=threshold)

    def test_short_or_non_finite_band_is_rejected(self):
        with self.assertRaises(ValueError):
            band_arrival(np.zeros(8), sample_rate_hz=RATE)
        with self.assertRaises(ValueError):
            band_arrival(np.full(8192, math.nan), sample_rate_hz=RATE)


class ConfirmationDocumentTests(unittest.TestCase):
    """Malformed input fails loudly; nothing is guessed."""

    def take(self):
        return synthetic_take(GEOMETRY_MS)

    def confirm(self, **overrides):
        return confirm_take(self.take(), **overrides)

    def test_single_way_or_duplicate_roles_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "two distinct speaker ways"):
            self.confirm(roles=("left_low",))
        with self.assertRaisesRegex(ValueError, "two distinct speaker ways"):
            self.confirm(roles=("left_low", "left_low"))

    def test_way_without_processing_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "no processing"):
            self.confirm(processing={"left_low": PROCESSING["left_low"]})

    def test_missing_identity_is_rejected(self):
        with self.assertRaises(ValueError):
            self.confirm(processing_fingerprint="")
        with self.assertRaises(ValueError):
            self.confirm(start_revision=True)

    def test_bad_rate_or_ir_is_rejected(self):
        with self.assertRaises(ValueError):
            self.confirm(sample_rate_hz=0)
        with self.assertRaises(ValueError):
            self.confirm(impulse_response=np.zeros(TAKE_SAMPLES))

    def test_three_way_side_is_judged_on_one_time_base(self):
        processing = {
            "left_low": way("lowpass"),
            "left_mid": {"highpass": {"family": "linkwitz-riley", "slope_db_oct": 24,
                                      "frequency_hz": 300.0},
                         "lowpass": {"family": "linkwitz-riley", "slope_db_oct": 24,
                                     "frequency_hz": 3000.0}},
            "left_high": way("highpass"),
        }
        roles = ("left_low", "left_mid", "left_high")
        take = synthetic_take({"left_low": 0.0, "left_mid": 2.0, "left_high": 2.0},
                              roles=roles, processing=processing)
        document = confirm_take(take, roles=roles, processing=processing)
        self.assertAlmostEqual(document["arrival_ms"]["left_mid"], 2.0, delta=0.05)
        self.assertAlmostEqual(document["arrival_ms"]["left_high"], 2.0, delta=0.05)
        self.assertAlmostEqual(document["arrival_ms"]["left_low"], 0.0, delta=0.05)


if __name__ == "__main__":
    unittest.main()
