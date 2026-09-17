#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Speaker alignment proposals from full-resolution, common-reference evidence."""

from __future__ import annotations

import copy
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audio.output_state import default_output_state, set_mode_routing, switch_mode
from dsp.crossover import design_crossover
from measurement.hybrid import build_complex_response
from measurement.analyzer import MeasurementAnalyzer
from measurement.speaker_align import SpeakerAlignment
from measurement.target import REFERENCE_TAP_INGRESS, freeze_measurement_target


RATE = 48000


def state_for(ways=("low", "high"), cutoffs=(2000,)):
    state = default_output_state()
    # Hardware order is deliberately unlike engine order; Low has fan-out.
    routes = [f"{side}_{way}" for side in ("right", "left") for way in reversed(ways)]
    routes += ["sub1", "left_low"]
    state = set_mode_routing(state, "crossover", "dev", routes)
    state = switch_mode(state, "crossover")
    state["revision"] = 7
    processing = state["modes"]["crossover"]["processing"]
    for side in ("left", "right"):
        for index, way in enumerate(ways):
            for kind, cutoff in (("highpass", cutoffs[index - 1] if index else None),
                                 ("lowpass", cutoffs[index] if index < len(cutoffs) else None)):
                processing[f"{side}_{way}"][kind] = None if cutoff is None else {
                    "family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": cutoff,
                }
    return state, len(routes)


def alignment_for(state=None, channels=None, *, side="left", rate=RATE):
    if state is None:
        state, channels = state_for()
    alignment = SpeakerAlignment(
        state, side=side, output_key="dev", channels=channels, sample_rate_hz=rate,
        fingerprint="frozen-plan", reference_id="interface:input-2:upstream",
        microphone_position_id="fixed-position-1",
    )
    live = freeze_measurement_target(
        state, bank_id="global", output_key="dev", channels=channels,
        sample_rate_hz=rate, fingerprint="frozen-plan",
    )
    return alignment, live


def lowpass(cutoff):
    offsets = np.arange(-256, 257, dtype=float)
    # Gaussian lowpass with -6 dB at cutoff and a broad, known-phase overlap.
    # A long windowed sinc would be a brick wall, not an alignable crossover.
    sigma = np.sqrt(2 * np.log(2)) * RATE / (2 * np.pi * cutoff)
    kernel = np.exp(-0.5 * (offsets / sigma) ** 2)
    return kernel / np.sum(kernel)


def captures_for(alignment, arrivals=(96, 240), *, cutoffs=(2000,)):
    """Linear-phase band-limited IRs: literal arrival offsets are independent truth."""
    delta = np.zeros(513)
    delta[256] = 1
    boundaries = [np.zeros(513), *(lowpass(cutoff) for cutoff in cutoffs), delta]
    captures = []
    for index, request in enumerate(alignment.capture_requests()):
        reference = 700 + index * 131  # Different capture alignment, same upstream origin.
        arrival = reference + arrivals[index]
        ir = np.zeros(4096)
        ir[arrival - 256:arrival + 257] = boundaries[index + 1] - boundaries[index]
        captures.append({
            "role": request["role"],
            "measurement_target": request["measurement_target"],
            "reference_id": request["reference_id"],
            "microphone_position_id": request["microphone_position_id"],
            "reference_tap": REFERENCE_TAP_INGRESS,
            "time_reference": "deconvolved-sweep-origin",
            "impulse_response": ir,
            "analysis": {
                "sample_rate": RATE, "peak_dbfs": -12.0,
                "quality_checks": {"status": "pass", "items": []},
                "reference_path": {
                    "usable": True, "electrical_reference_used": True,
                    "timing_status": "electrical-reference", "stability": "stable",
                    "confidence": 0.95, "clipped": False, "peak_dbfs": -9.0,
                },
                "impulse_response": {
                    "direct_arrival_index": arrival, "reference_peak_index": reference,
                    "direct_confidence": 0.95,
                    "timing_source": "direct_arrival_minus_reference_peak",
                },
            },
        })
    return captures


def native_captures(alignment, *, slope, rate=RATE, detect_arrival=True):
    """Causal LR IRs from the actual SOS design, with independently delayed onsets."""
    captures = captures_for(alignment)
    for index, capture in enumerate(captures):
        kind = "lowpass" if index == 0 else "highpass"
        z = np.exp(-2j * np.pi * np.fft.rfftfreq(4096))
        spectrum = np.ones_like(z)
        for b0, b1, b2, _, a1, a2 in design_crossover({
            "kind": kind, "family": "linkwitz-riley", "slope_db_oct": slope,
            "frequency_hz": 2000,
        }, rate):
            spectrum *= (b0 + b1 * z + b2 * z * z) / (1 + a1 * z + a2 * z * z)
        # Odd LR half-orders require the configured opposite way polarity.
        polarity = -1 if slope % 24 == 12 and index == 1 else 1
        onset = capture["analysis"]["impulse_response"]["direct_arrival_index"]
        capture["impulse_response"] = np.concatenate([np.zeros(onset), polarity * np.fft.irfft(spectrum)])
        capture["analysis"]["sample_rate"] = rate
        if detect_arrival:
            reference = np.zeros_like(capture["impulse_response"])
            reference[capture["analysis"]["impulse_response"]["reference_peak_index"]] = 1
            timing = MeasurementAnalyzer(None, RuntimeError)._estimate_impulse_direct_arrival(
                capture["impulse_response"], reference, rate,
            )
            capture["analysis"]["impulse_response"].update(
                direct_arrival_index=timing["direct_arrival_index"], direct_confidence=timing["confidence"],
            )
    return captures


class ComplexOriginTests(unittest.TestCase):
    def test_exact_high_frequency_phase_includes_slice_and_reference_origin(self):
        ir = np.zeros(8192)
        ir[2407] = 0.7
        frequencies = [2173.25, 3001.5, 7319.125]
        response = build_complex_response(ir, RATE, frequencies_hz=frequencies, reference_sample=2311)
        self.assertEqual([point[0] for point in response["points"]], frequencies)
        measured = np.array([complex(point[1], point[2]) for point in response["points"]])
        expected = 0.7 * np.exp(-2j * np.pi * np.array(frequencies) * 96 / RATE)
        np.testing.assert_allclose(measured, expected, atol=1e-8)
        self.assertEqual(response["time_reference"], "upstream-reference-peak")

    def test_direct_event_is_retained_when_a_late_reflection_is_stronger(self):
        ir = np.zeros(8192)
        ir[1000] = -1.0
        ir[1960] = 1.1
        frequencies = np.array([1517.0, 2013.5, 2471.25])
        response = build_complex_response(
            ir, RATE, frequencies_hz=frequencies, reference_sample=800,
            direct_arrival_sample=1000,
        )
        measured = np.array([complex(point[1], point[2]) for point in response["points"]])
        expected = (-np.exp(-2j * np.pi * frequencies * 200 / RATE)
                    + 1.1 * np.exp(-2j * np.pi * frequencies * 1160 / RATE))
        np.testing.assert_allclose(measured, expected, atol=1e-8)


class RoutingAndProposalTests(unittest.TestCase):
    def test_requests_use_canonical_way_masks_not_hardware_order_or_subs(self):
        alignment, _ = alignment_for()
        requests = alignment.capture_requests()
        self.assertEqual([item["role"] for item in requests], ["left_low", "left_high"])
        self.assertEqual([item["output_mask"] for item in requests], [30, 29])
        self.assertEqual([item["channel"] for item in requests], ["left", "left"])
        requests[0]["measurement_target"]["revision"] = 999
        self.assertEqual(alignment.capture_requests()[0]["measurement_target"]["revision"], 7)

    def test_known_offsets_are_added_to_existing_alignment_without_mutating_input(self):
        state, channels = state_for()
        state["modes"]["crossover"]["processing"]["left_low"]["alignment_ms"] = -2.0
        state["modes"]["crossover"]["processing"]["left_high"]["alignment_ms"] = 1.0
        original = copy.deepcopy(state)
        alignment, live = alignment_for(state, channels)
        captures = captures_for(alignment)
        original_ir = captures[0]["impulse_response"].copy()
        proposal = alignment.propose(captures, live_target=live)
        self.assertEqual(proposal["arrival_ms"], {"left_low": 2.0, "left_high": 5.0})
        self.assertEqual(proposal["added_delay_ms"], {"left_low": 3.0, "left_high": 0.0})
        expected = copy.deepcopy(original)
        expected["modes"]["crossover"]["processing"]["left_low"]["alignment_ms"] = 1.0
        self.assertEqual(proposal["candidate_state"], expected)
        self.assertEqual(state, original)
        np.testing.assert_array_equal(captures[0]["impulse_response"], original_ir)
        self.assertGreater(proposal["overlap_checks"][0]["after_sum_db"], -0.01)

    def test_three_and_four_ways_validate_every_adjacent_overlap_including_above_2khz(self):
        for ways, cutoffs, arrivals, delays in (
            (("low", "mid", "high"), (300, 2500), (96, 144, 240), (3.0, 2.0, 0.0)),
            (("low", "low_mid", "mid", "high"), (300, 1000, 3000), (96, 144, 192, 240), (3.0, 2.0, 1.0, 0.0)),
        ):
            with self.subTest(ways=ways):
                state, channels = state_for(ways, cutoffs)
                alignment, live = alignment_for(state, channels, side="right")
                proposal = alignment.propose(captures_for(alignment, arrivals, cutoffs=cutoffs), live_target=live)
                self.assertEqual(proposal["added_delay_ms"], dict(zip((f"right_{way}" for way in ways), delays)))
                self.assertEqual(len(proposal["overlap_checks"]), len(ways) - 1)

    def test_equal_arrivals_produce_an_unchanged_candidate(self):
        state, channels = state_for()
        alignment, live = alignment_for(state, channels)
        result = alignment.propose(captures_for(alignment, (144, 144)), live_target=live)
        self.assertEqual(result["candidate_state"], state)

    def test_frozen_start_is_detached_and_proposals_never_accumulate(self):
        state, channels = state_for()
        alignment, live = alignment_for(state, channels)
        state["modes"]["crossover"]["processing"]["left_low"]["alignment_ms"] = 20
        first = alignment.propose(captures_for(alignment), live_target=live)
        first["candidate_state"]["modes"]["crossover"]["processing"]["left_low"]["alignment_ms"] = 30
        second = alignment.propose(captures_for(alignment), live_target=live)
        self.assertEqual(second["candidate_state"]["modes"]["crossover"]["processing"]["left_low"]["alignment_ms"], 3.0)

    def test_causal_lr12_and_lr24_with_real_arrival_detection(self):
        for rate in (44100, 48000, 96000):
            for slope in (12, 24):
                with self.subTest(rate=rate, slope=slope):
                    state, channels = state_for()
                    for settings in state["modes"]["crossover"]["processing"].values():
                        for kind in ("lowpass", "highpass"):
                            if settings[kind] is not None:
                                settings[kind]["slope_db_oct"] = slope
                    if slope == 12:
                        state["modes"]["crossover"]["processing"]["left_high"]["polarity"] = "invert"
                    alignment, live = alignment_for(state, channels, rate=rate)
                    proposal = alignment.propose(native_captures(alignment, slope=slope, rate=rate), live_target=live)
                    # Known acoustic offset is 144 samples; small peak-detector
                    # quantization/group delay is admitted, not a whole cycle.
                    self.assertAlmostEqual(proposal["added_delay_ms"]["left_low"], 144000 / rate, delta=0.06)
                    self.assertGreater(proposal["overlap_checks"][0]["after_sum_db"], -0.5)


class RejectionTests(unittest.TestCase):
    def setUp(self):
        self.alignment, self.live = alignment_for()
        self.captures = captures_for(self.alignment)

    def assert_rejected(self, text):
        with self.assertRaisesRegex(ValueError, text):
            self.alignment.propose(self.captures, live_target=self.live)

    def test_reversed_polarity_is_rejected_without_guessing_an_inversion(self):
        self.captures[0]["impulse_response"] *= -1
        self.assert_rejected("phase|polarity")

    def test_ambiguous_low_way_arrival_is_rejected_by_overlap_phase(self):
        # A peak detector chooses a late low-way lobe; the actual IR is unchanged.
        self.captures[0]["analysis"]["impulse_response"]["direct_arrival_index"] += 12
        self.assert_rejected("phase|combined")

    def test_reversed_polarity_cannot_be_hidden_by_half_cycle_arrival_error(self):
        self.captures[0]["impulse_response"] *= -1
        self.captures[0]["analysis"]["impulse_response"]["direct_arrival_index"] += 12
        self.assert_rejected("phase|timing")

    def test_full_cycle_error_cannot_pass_in_a_steep_native_overlap(self):
        # LR48 has equal low/high phase, but a narrow enough overlap that an
        # entire cycle of incorrect compensation can pass the RMS/sum gates.
        self.captures = native_captures(self.alignment, slope=48, detect_arrival=False)
        self.captures[0]["analysis"]["impulse_response"]["direct_arrival_index"] += 24
        self.assert_rejected("phase|timing")

    def test_steep_causal_peak_timing_is_declined_instead_of_forced_into_a_proposal(self):
        for slope in (48, 72):
            with self.subTest(slope=slope):
                self.captures = native_captures(self.alignment, slope=slope)
                self.assert_rejected("overlap|phase|timing")

    def test_stronger_late_reflections_cannot_replace_direct_phase_evidence(self):
        detector = MeasurementAnalyzer(None, RuntimeError)
        for index, capture in enumerate(self.captures):
            direct = capture["impulse_response"].copy()
            capture["impulse_response"] = (-direct if index == 0 else direct) + 1.1 * np.roll(direct, 960)
            reference = np.zeros_like(direct)
            reference[capture["analysis"]["impulse_response"]["reference_peak_index"]] = 1
            timing = detector._estimate_impulse_direct_arrival(capture["impulse_response"], reference, RATE)
            self.assertEqual(timing["direct_arrival_index"], 796 if index == 0 else 1071)
            capture["analysis"]["impulse_response"].update(
                direct_arrival_index=timing["direct_arrival_index"], direct_confidence=timing["confidence"],
            )
        self.assert_rejected("overlap|phase|timing|combined")

    def test_no_measured_overlap_is_rejected(self):
        self.captures = captures_for(self.alignment, cutoffs=(400,))
        self.assert_rejected("overlap")

    def test_missing_duplicate_and_foreign_ways_are_rejected(self):
        original = self.captures
        for invalid in (original[:1], original + original[:1],
                        [original[0], {**original[1], "role": "sub1"}]):
            with self.subTest(roles=[item["role"] for item in invalid]):
                self.captures = invalid
                self.assert_rejected("way|role")

    def test_tolerated_fallback_or_low_confidence_reference_is_rejected(self):
        for change in ({"stability": "dsp-end-anchor-tolerated"}, {"stability": "unstable"},
                       {"electrical_reference_used": False}, {"usable": False},
                       {"confidence": 0.1}, {"confidence": float("nan")},
                       {"electrical_reference_fallback": True}):
            with self.subTest(change=change):
                self.captures = captures_for(self.alignment)
                self.captures[0]["analysis"]["reference_path"].update(change)
                self.assert_rejected("reference")

    def test_clipping_on_either_mic_or_reference_is_rejected(self):
        for path, field, value in (("", "peak_dbfs", 0.0), ("reference_path", "clipped", True),
                                   ("reference_path", "peak_dbfs", 0.0)):
            with self.subTest(path=path, field=field):
                self.captures = captures_for(self.alignment)
                analysis = self.captures[0]["analysis"]
                (analysis[path] if path else analysis)[field] = value
                self.assert_rejected("clip")

    def test_missing_or_bad_quality_metadata_fails_closed(self):
        for change in ({"quality_checks": {}}, {"quality_checks": {"status": "fail", "items": []}},
                       {"quality_checks": {"status": "warn", "items": [{"level": "error"}]}},
                       {"peak_dbfs": float("nan")}):
            with self.subTest(change=change):
                self.captures = captures_for(self.alignment)
                self.captures[0]["analysis"].update(change)
                self.assert_rejected("quality|peak")

    def test_bad_or_peak_relative_ir_is_rejected(self):
        for field, value in (("impulse_response", []), ("impulse_response", [0.0] * 4096),
                             ("impulse_response", [float("nan")] * 4096),
                             ("impulse_response", np.zeros((100, 2))),
                             ("time_reference", "peak-relative")):
            with self.subTest(field=field, shape=np.shape(value)):
                self.captures = captures_for(self.alignment)
                self.captures[0][field] = value
                self.assert_rejected("IR|origin")

    def test_invalid_or_unconfident_arrival_indices_are_rejected(self):
        for change in ({"direct_arrival_index": -1}, {"reference_peak_index": 5000},
                       {"direct_arrival_index": 100.5}, {"direct_confidence": 0.1},
                       {"direct_confidence": float("nan")},
                       {"timing_source": "independent-peak-zero"}):
            with self.subTest(change=change):
                self.captures = captures_for(self.alignment)
                self.captures[0]["analysis"]["impulse_response"].update(change)
                self.assert_rejected("arrival|timing")

    def test_changed_capture_identity_is_rejected(self):
        for field, value in (("reference_id", "other-input"), ("microphone_position_id", "moved"),
                             ("reference_tap", "left_low.after_filters")):
            with self.subTest(field=field):
                self.captures = captures_for(self.alignment)
                self.captures[0][field] = value
                self.assert_rejected("reference|position")

    def test_changed_capture_target_or_rate_is_rejected(self):
        for change in ({"revision": 8}, {"processing_fingerprint": "other"}, {"bank_id": "left_high"}):
            with self.subTest(change=change):
                self.captures = captures_for(self.alignment)
                self.captures[0]["measurement_target"].update(change)
                self.assert_rejected("target")
        self.captures = captures_for(self.alignment)
        self.captures[0]["analysis"]["sample_rate"] = 44100
        self.assert_rejected("rate")

    def test_stale_revision_even_with_equal_fingerprint_and_live_device_drift_are_rejected(self):
        original = self.live
        for change in ({"revision": 8}, {"processing_fingerprint": "other"}, {"device_key": "other"},
                       {"sample_rate_hz": 44100}, {"reference_tap": "downstream"}):
            with self.subTest(change=change):
                self.live = {**original, **change}
                self.assert_rejected("stale|target")

    def test_cancellation_vetoes_analysis_and_return_of_a_proposal(self):
        import asyncio
        for cancel_on in (1, 4):
            calls = 0

            def cancelled():
                nonlocal calls
                calls += 1
                return calls >= cancel_on

            with self.subTest(cancel_on=cancel_on), self.assertRaises(asyncio.CancelledError):
                self.alignment.propose(self.captures, live_target=self.live, cancel_requested=cancelled)

    def test_out_of_range_candidate_is_rejected_instead_of_clamped(self):
        state, channels = state_for()
        state["modes"]["crossover"]["processing"]["left_low"]["alignment_ms"] = 39.0
        self.alignment, self.live = alignment_for(state, channels)
        self.captures = captures_for(self.alignment)
        self.assert_rejected("alignment")

    def test_unsupported_topology_and_missing_overlap_filters_fail_before_capture(self):
        for mutate in (
            lambda state: state.update(active_mode="stereo"),
            lambda state: state["modes"]["crossover"]["processing"]["left_low"].update(lowpass=None),
            lambda state: state["modes"]["crossover"]["processing"]["left_high"]["highpass"].update(frequency_hz=10000),
        ):
            with self.subTest(mutate=mutate):
                state, channels = state_for()
                mutate(state)
                with self.assertRaises(ValueError):
                    alignment_for(state, channels)


if __name__ == "__main__":
    unittest.main(verbosity=2)
