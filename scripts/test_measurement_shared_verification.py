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
from measurement.alignment_backend import way_crossover_specs
from measurement.speaker_verification import (
    MIN_WAY_ISOLATION_DB,
    ARRIVAL_LOBE_MAX_MS,
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
    """Causal frequency response of one way's crossover (``way_crossover_specs``)."""
    frequencies = np.fft.rfftfreq(TAKE_SAMPLES, 1.0 / RATE)
    z = np.exp(-2j * np.pi * frequencies / RATE)
    response = np.ones_like(frequencies, dtype=complex)
    for spec in way_crossover_specs(processing[role]):
        for b0, b1, b2, _, a1, a2 in design_crossover(spec, RATE):
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
        # The real confirmed case: seven samples apart at 48 kHz. The wide low
        # band cannot resolve that far inside its own lobe, so it reports no
        # margin at all; the narrow high band may resolve it and then has to
        # show a healthy margin, never a marginal one.
        document, residual = residual_ms(synthetic_take({"left_low": 3.0, "left_high": 3.0 - 7 / 48.0}))
        self.assertLessEqual(residual, 0.25)
        self.assertIsNone(document["way_isolation_db"]["left_low"])
        high_margin = document["way_isolation_db"]["left_high"]
        self.assertTrue(high_margin is None or high_margin > 20.0, high_margin)

    def test_first_significant_energy_and_lobe_width_are_reported(self):
        document, _ = residual_ms(synthetic_take(GEOMETRY_MS))
        for role in ROLES:
            band = document["bands"][role]
            # A resolved arrival: the lobe is far shorter than the way offset.
            self.assertLess(band["lobe_samples"], int(7.5417 * RATE / 1000.0))
            self.assertGreater(band["lobe_samples"], 0)

    def test_a_way_too_quiet_to_locate_is_reported_by_its_level(self):
        # 40 dB down, the quiet way's isolated band is mostly the neighbour's
        # leak: either its arrival collapses onto the neighbour's lobe (no
        # isolation margin at all) or it stands only marginally apart. The
        # residual then cannot stand alone, so the take reports the level
        # spread as evidence instead of confirming silently.
        document, residual = residual_ms(synthetic_take(GEOMETRY_MS, gains_db={"left_high": -40.0}))
        margin = document["way_isolation_db"]["left_high"]
        self.assertTrue(margin is None or margin < MIN_WAY_ISOLATION_DB, margin)
        self.assertGreater(abs(document["way_levels_db"]["left_high"]), 20.0)
        if margin is None:
            # A collapsed arrival: the residual gate is blind here, which is
            # exactly why the level evidence has to be reported.
            self.assertLessEqual(residual, 0.02)

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


class RenderedMainHighpassTests(unittest.TestCase):
    """The band model has to carry every filter the engine renders on a way.

    With subs routed, the plan adds the bass-management Main high-pass to every
    speaker way. The real right side on the test machine: LR24 1 kHz / 3 kHz
    ways, an LR24 82 Hz Main high-pass, the tweeter 0.354 ms ahead of the
    woofer and 11 dB louder, and a woofer with its own low-frequency roll-off.
    A matched filter built from the way's own filters leaves the Main
    high-pass phase in the low band, whose rebound lobe then outweighs the
    real arrival: the plan lands about 1 ms late and the take reads as
    unseparable (6.9 dB on the real take, below the 10 dB gate).
    """

    LOW = {"lowpass": {"family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 1000.0}}
    HIGH = {"highpass": {"family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 3000.0}}
    MAIN_HIGHPASS = {"kind": "highpass", "family": "linkwitz-riley", "slope_db_oct": 24,
                     "frequency_hz": 82.0}
    RENDERED = {
        "right_low": {"crossover": [{"kind": "lowpass", **LOW["lowpass"]}, MAIN_HIGHPASS]},
        "right_high": {"crossover": [{"kind": "highpass", **HIGH["highpass"]}, MAIN_HIGHPASS]},
    }
    OWN_FILTERS_ONLY = {"right_low": LOW, "right_high": HIGH}
    ROLES = ("right_low", "right_high")
    LOW_BEHIND_HIGH_MS = 0.354

    def take(self, low_behind_high_ms=None, *, woofer_hz=100.0, woofer_q=0.7,
             tweeter_reflection=None):
        if low_behind_high_ms is None:
            low_behind_high_ms = self.LOW_BEHIND_HIGH_MS
        frequencies = np.fft.rfftfreq(TAKE_SAMPLES, 1.0 / RATE)
        tweeter = 1.0
        if tweeter_reflection is not None:
            samples, gain = tweeter_reflection
            tweeter = 1.0 + gain * np.exp(-2j * np.pi * frequencies * samples / RATE)
        woofer = 1.0
        if woofer_hz is not None:
            corner = 1j * frequencies / woofer_hz
            woofer = corner ** 2 / (corner ** 2 + corner / woofer_q + 1.0)
        spectrum = np.zeros_like(frequencies, dtype=complex)
        for role, arrival_ms, gain_db, driver in (
                ("right_low", low_behind_high_ms, 0.0, woofer),
                ("right_high", 0.0, 11.0, tweeter)):
            delay = ORIGIN_SAMPLES + int(round(arrival_ms * RATE / 1000.0))
            spectrum += (10.0 ** (gain_db / 20.0) * driver * way_response(self.RENDERED, role)
                         * np.exp(-2j * np.pi * frequencies * delay / RATE))
        return np.fft.irfft(spectrum, n=TAKE_SAMPLES)

    def confirm(self, processing):
        return confirm_take(self.take(), processing=processing, roles=self.ROLES)

    def test_rendered_model_plans_the_real_offset(self):
        document = self.confirm(self.RENDERED)
        offset = document["arrival_ms"]["right_low"] - document["arrival_ms"]["right_high"]
        # The woofer's own roll-off is not modelled; it may pull the band by
        # a few samples, never by a lobe.
        self.assertAlmostEqual(offset, self.LOW_BEHIND_HIGH_MS, delta=0.1)
        for margin in document["way_isolation_db"].values():
            self.assertTrue(margin is None or margin >= MIN_WAY_ISOLATION_DB, margin)

    def test_own_filters_alone_land_on_the_rebound_and_cannot_separate(self):
        document = self.confirm(self.OWN_FILTERS_ONLY)
        offset = document["arrival_ms"]["right_low"] - document["arrival_ms"]["right_high"]
        self.assertGreater(offset, self.LOW_BEHIND_HIGH_MS + 0.5)
        self.assertLess(document["way_isolation_db"]["right_low"], MIN_WAY_ISOLATION_DB)

    def test_rendered_list_wins_over_the_ways_own_filters(self):
        both = {role: {**self.OWN_FILTERS_ONLY[role], **self.RENDERED[role]} for role in self.ROLES}
        self.assertEqual(self.confirm(both)["arrival_ms"], self.confirm(self.RENDERED)["arrival_ms"])

    def test_an_arrival_inside_the_leading_flank_is_the_same_lobe(self):
        """The woofer's roll-off stretches the low band's leading flank.

        With the tweeter 20 samples ahead, the tweeter's index lies past the
        low lobe's trailing extent but inside its leading one: the low band's
        energy there is its own lobe, not a leak, so it reports no margin
        instead of a failing one.
        """
        document = confirm_take(self.take(0.526, woofer_hz=200.0, woofer_q=1.2),
                                processing=self.RENDERED, roles=self.ROLES)
        low = document["bands"]["right_low"]
        ahead = low["arrival_index"] - document["bands"]["right_high"]["arrival_index"]
        self.assertGreater(low["lead_samples"], low["lobe_samples"])
        self.assertGreater(ahead, low["lobe_samples"])
        self.assertLessEqual(ahead, low["lead_samples"])
        band = way_band_impulse_response(
            self.take(0.526, woofer_hz=200.0, woofer_q=1.2), self.RENDERED["right_low"],
            sample_rate_hz=RATE, foreign=[self.RENDERED["right_high"]])
        energy = np.square(band)
        own_flank_db = 10.0 * math.log10(energy[low["arrival_index"]] / energy[low["arrival_index"] - ahead])
        self.assertLess(own_flank_db, MIN_WAY_ISOLATION_DB)
        self.assertIsNone(document["way_isolation_db"]["right_low"])

    def test_an_arrival_past_the_leading_flank_is_judged(self):
        document = confirm_take(self.take(0.610, woofer_hz=200.0, woofer_q=1.2),
                                processing=self.RENDERED, roles=self.ROLES)
        low = document["bands"]["right_low"]
        ahead = low["arrival_index"] - document["bands"]["right_high"]["arrival_index"]
        self.assertGreater(ahead, low["lead_samples"])
        self.assertGreaterEqual(document["way_isolation_db"]["right_low"], MIN_WAY_ISOLATION_DB)

    def test_band_levels_read_the_way_level_through_the_rendered_crossover(self):
        # The band filter leaves |H|^3 and the owned share on each way; the
        # level removes that weighting, so a 1 kHz / 3 kHz crossover reads the
        # 11 dB the ways really differ by, not the band shapes' difference.
        document = confirm_take(self.take(woofer_hz=None), processing=self.RENDERED, roles=self.ROLES)
        self.assertAlmostEqual(document["way_levels_db"]["right_low"], -11.0, delta=0.15)

    MICROPHONE = {"frequencies_hz": [20.0, 1000.0, 3000.0, 20000.0],
                  "offsets_db": [0.0, 0.0, 1.0, 3.0]}

    def through_microphone(self, take):
        frequencies = np.fft.rfftfreq(TAKE_SAMPLES, 1.0 / RATE)
        offsets = np.interp(np.log(np.clip(frequencies, 1e-9, None)),
                            np.log(self.MICROPHONE["frequencies_hz"]), self.MICROPHONE["offsets_db"],
                            left=0.0, right=3.0)
        return np.fft.irfft(np.fft.rfft(take) * 10.0 ** (offsets / 20.0), n=TAKE_SAMPLES)

    def test_band_levels_are_read_through_the_microphone_calibration(self):
        """The per-way levels the gain is planned from are calibrated; so is this one."""
        take = self.through_microphone(self.take(woofer_hz=None))
        calibrated = confirm_take(take, processing=self.RENDERED, roles=self.ROLES,
                                  calibration_curve=self.MICROPHONE)
        raw = confirm_take(take, processing=self.RENDERED, roles=self.ROLES)
        self.assertAlmostEqual(calibrated["way_levels_db"]["right_low"], -11.0, delta=0.15)
        # Uncalibrated, the microphone's own treble rise reads as tweeter level.
        self.assertLess(raw["way_levels_db"]["right_low"], -13.0)

    def test_malformed_calibration_curve_is_rejected(self):
        for curve in ({"frequencies_hz": [1000.0], "offsets_db": [0.0]},
                      {"frequencies_hz": [1000.0, 20.0], "offsets_db": [0.0, 1.0]},
                      {"frequencies_hz": [20.0, 1000.0], "offsets_db": [0.0]},
                      [[20.0, 0.0], [1000.0, 0.0]]):
            with self.subTest(curve=curve), self.assertRaises(ValueError):
                confirm_take(self.take(), processing=self.RENDERED, roles=self.ROLES,
                             calibration_curve=curve)

    def test_the_tweeters_own_reflection_after_its_arrival_is_not_a_leak(self):
        """The left side on the test machine, re-planned from a near-aligned state.

        The woofer's band arrival lands 8 samples after the tweeter's, right on
        a reflection of the tweeter itself (-9 dB). Through a 1 kHz / 3 kHz
        crossover the woofer can leave nowhere near that much in the tweeter's
        band, so the energy is the tweeter's own and the ways stay separable.
        """
        take = self.take(12 / 48.0, tweeter_reflection=(8, 0.35))
        document = confirm_take(take, processing=self.RENDERED, roles=self.ROLES)
        high = document["bands"]["right_high"]
        after = document["bands"]["right_low"]["arrival_index"] - high["arrival_index"]
        self.assertEqual(after, 8)
        band = way_band_impulse_response(take, self.RENDERED["right_high"], sample_rate_hz=RATE,
                                         foreign=[self.RENDERED["right_low"]])
        energy = np.square(band)
        measured_db = 10.0 * math.log10(energy[high["arrival_index"]]
                                        / energy[high["arrival_index"] + after])
        self.assertLess(measured_db, MIN_WAY_ISOLATION_DB)
        self.assertGreater(document["way_isolation_db"]["right_high"], MIN_WAY_ISOLATION_DB + 20.0)

    def test_malformed_rendered_list_is_rejected(self):
        for crossover in ({"kind": "lowpass"}, [{"family": "linkwitz-riley"}], ["lowpass"]):
            with self.subTest(crossover=crossover), self.assertRaises(ValueError):
                way_band_impulse_response(self.take(), {"crossover": crossover}, sample_rate_hz=RATE)


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

    def test_a_near_tie_resolves_to_the_leading_lobe_either_way(self):
        """Two lobes within a fraction of a dB are one arrival, not a coin flip.

        Real takes carried the way's second lobe 0.03-0.37 dB from the first,
        so the strongest energy picked whichever the take's noise made louder
        and the planned delay flipped by that distance. The leading lobe is the
        same physical arrival in both cases.
        """
        louder_later = band_arrival(self.band((1000, 10 ** -0.005), (1057, 1.0)),
                                    sample_rate_hz=RATE)
        louder_earlier = band_arrival(self.band((1000, 1.0), (1057, 10 ** -0.005)),
                                      sample_rate_hz=RATE)
        self.assertEqual(louder_later["arrival_index"], 1000)
        self.assertEqual(louder_earlier["arrival_index"], 1000)
        self.assertLess(louder_later["onset_index"], 1000)

    def test_a_leading_lobe_far_quieter_than_the_peak_keeps_the_peak(self):
        """Real takes put the neighbouring structure 5.2 dB or more down."""
        arrival = band_arrival(self.band((1000, 10 ** -0.3), (1057, 1.0)),
                               sample_rate_hz=RATE)
        self.assertEqual(arrival["arrival_index"], 1057)

    def test_a_leading_shoulder_without_a_null_stays_one_lobe(self):
        values = np.zeros(8192)
        for index in range(1000, 1058):
            values[index] = 0.9 + 0.1 * (index - 1000) / 57.0
        self.assertEqual(band_arrival(values, sample_rate_hz=RATE)["arrival_index"], 1057)

    def test_lobe_extent_is_reported_on_both_sides(self):
        values = np.zeros(8192)
        values[980:1000] = np.linspace(0.4, 0.9, 20)
        values[1000] = 1.0
        values[1001:1006] = 0.9
        arrival = band_arrival(values, sample_rate_hz=RATE)
        self.assertEqual(arrival["arrival_index"], 1000)
        self.assertEqual(arrival["lead_samples"], 21)
        self.assertEqual(arrival["lobe_samples"], 6)

    def test_a_leading_lobe_outside_the_lobe_window_keeps_the_peak(self):
        reach = int(ARRIVAL_LOBE_MAX_MS * RATE / 1000.0)
        arrival = band_arrival(self.band((1000, 0.9), (1000 + reach + 100, 1.0)),
                               sample_rate_hz=RATE)
        self.assertEqual(arrival["arrival_index"], 1000 + reach + 100)

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
