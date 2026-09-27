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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import speaker_take_test_support as takes
from audio.output_state import (default_output_state, set_bass_management, set_crossover,
                                set_mode_routing, switch_mode)
from dsp.crossover import design_crossover
from measurement.hybrid import build_complex_response
from measurement.alignment_backend import way_crossover_specs
from measurement.analyzer import MeasurementAnalyzer
from measurement.speaker_align import SpeakerAlignment, require_timing_reference
from measurement.target import (
    REFERENCE_TAP_INGRESS,
    freeze_measurement_target,
    target_output_mask,
)


RATE = 48000


def state_for(ways=("low", "high"), cutoffs=(2000,)):
    state = default_output_state()
    # Hardware order is deliberately unlike engine order; Low has fan-out.
    routes = [f"{side}_{way}" for side in ("right", "left") for way in reversed(ways)]
    routes += ["sub1", "left_low"]
    state = set_mode_routing(set_crossover(state, "stereo-sub", True), "stereo-sub", "dev", routes)
    state = switch_mode(state, "stereo-sub")
    state["revision"] = 7
    processing = state["modes"]["stereo-sub"]["processing"]
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


def planning_for(alignment, arrivals=(96, 240), *, rate=RATE, **take_options):
    """Planning take whose way arrivals are the given sample offsets.

    The suite expresses its intent in samples (as the per-way captures do), so
    the shared take is placed on the same offsets; the returned document is the
    real one ``SpeakerAlignment.planning`` produces.
    """
    roles = [request["role"] for request in alignment.capture_requests()]
    return takes.planning_document(
        alignment,
        {role: arrivals[index] * 1000.0 / rate for index, role in enumerate(roles)},
        sample_rate_hz=rate, **take_options)


def proposal_for(alignment, live, arrivals=(96, 240), *, cutoffs=(2000,), rate=RATE,
                 **take_options):
    """A proposal whose delays are planned from a shared take, not per way."""
    return alignment.propose(
        captures_for(alignment, arrivals, cutoffs=cutoffs),
        planning=planning_for(alignment, arrivals, rate=rate, **take_options),
        live_target=live)


def plan_delays(state, channels):
    """Per-role delay of the rendered processing plan."""
    from dsp.processing_plan import compile_processing_plan
    plan = compile_processing_plan(
        state, output_key="dev", channels=channels, sample_rate_hz=RATE,
        preset_loader=lambda name: {"chain": []}, neutralize_banks=True)
    return {row["role"]: row["delay_ms"] for row in plan["outputs"]}


def assert_timing_kept(test, start_state, proposal, channels):
    """Rendered delays move only by the added way delays plus one constant.

    Any rebase offset has to be common to every routed role, so no role
    moves against another in the rendered plan.
    """
    before = plan_delays(start_state, channels)
    after = plan_delays(proposal["candidate_state"], channels)
    test.assertEqual(set(after), set(before))
    residual = {role: after[role] - before[role] - proposal["added_delay_ms"].get(role, 0.0)
                for role in before}
    common = residual[next(iter(residual))]
    for role, value in residual.items():
        test.assertAlmostEqual(value, common, delta=1e-9,
                               msg=f"{role} moved against the other routed roles")


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


def shared_take(alignment, state, arrivals_ms=None, *, rates=None, level_spread_db=0.0):
    """One take in which every way of the side plays its own band at once.

    Each way contributes the causal crossover response the engine renders,
    placed at its own total arrival; way level differences come from an IR gain.
    """
    processing = alignment.way_models()
    roles = [request["role"] for request in alignment.capture_requests()]
    arrivals_ms = arrivals_ms or {role: float(index) for index, role in enumerate(roles)}
    rates = rates or {}
    samples = 8192
    # Real captures place the sweep well inside the take: keep the same head
    # room so no way's kernel is truncated at the buffer start.
    origin_samples = 1024
    frequencies = np.fft.rfftfreq(samples, 1.0 / RATE)
    z = np.exp(-2j * np.pi * frequencies / RATE)
    spectrum = np.zeros_like(frequencies, dtype=complex)
    for role in roles:
        spectrum = spectrum + way_spectrum(
            processing, role, z, frequencies,
            origin_samples * 1000.0 / RATE + arrivals_ms[role], rates.get(role, 1.0))
    take = copy.deepcopy(captures_for(alignment)[0])
    take["measurement_target"] = alignment.verification_request()["measurement_target"]
    take["impulse_response"] = np.fft.irfft(spectrum, n=samples)
    return take


def way_spectrum(processing, role, z, frequencies, arrival_ms, rate_factor):
    response = np.ones_like(frequencies, dtype=complex)
    for spec in way_crossover_specs(processing[role]):
        for b0, b1, b2, _, a1, a2 in design_crossover(spec, RATE):
            response = response * (b0 + b1 * z + b2 * z * z) / (1 + a1 * z + a2 * z * z)
    delay = int(round(arrival_ms * RATE / 1000.0))
    return rate_factor * response * np.exp(-2j * np.pi * frequencies * delay / RATE)


class SideConfirmationTests(unittest.TestCase):
    """Speaker Align judges its side from one shared take on one time base."""

    def setUp(self):
        self.state, channels = state_for()
        self.alignment, self.live = alignment_for(self.state, channels)

    def take(self, **kwargs):
        return shared_take(self.alignment, self.state, **kwargs)

    def test_verification_request_measures_only_this_side(self):
        request = self.alignment.verification_request()
        self.assertEqual(request["side"], "left")
        self.assertEqual(request["roles"], ["left_low", "left_high"])
        self.assertEqual(request["reference_tap"], REFERENCE_TAP_INGRESS)
        target = request["measurement_target"]
        self.assertEqual(target["measured_roles"], ["left_low", "left_high"])
        self.assertEqual(target["roles"], self.alignment.capture_requests()[0]["measurement_target"]["roles"])
        self.assertEqual(target["revision"], 7)
        self.assertEqual(target["processing_fingerprint"], "frozen-plan")
        # Only this side plays: every other routed role stays muted, so the
        # shared take mutes strictly less than a single way's planning take.
        self.assertEqual(request["output_mask"], target_output_mask(target, roles=target["roles"]))
        self.assertNotEqual(request["output_mask"], 0)
        per_way = [capture["output_mask"] for capture in self.alignment.capture_requests()]
        for mask in per_way:
            self.assertEqual(request["output_mask"] & ~mask, 0)
        self.assertNotEqual(request["output_mask"], per_way[0])

    def test_planning_request_measures_only_this_side(self):
        planning = self.alignment.planning_request()
        verification = self.alignment.verification_request()
        self.assertEqual(planning["side"], "left")
        self.assertEqual(planning["roles"], ["left_low", "left_high"])
        # Planning and verification read the same side, the same way: only the
        # take's role in the run differs, so both use one shared time base.
        self.assertEqual(planning["measurement_target"], verification["measurement_target"])
        self.assertEqual(planning["output_mask"], verification["output_mask"])
        self.assertNotEqual(planning["output_mask"], self.alignment.capture_requests()[0]["output_mask"])

    def test_planning_take_plans_the_offset_it_measured(self):
        document = self.alignment.planning(self.take(arrivals_ms={"left_low": 0.0, "left_high": 7.5417}))
        self.assertEqual(set(document["arrival_ms"]), {"left_low", "left_high"})
        spread = max(document["arrival_ms"].values()) - min(document["arrival_ms"].values())
        self.assertAlmostEqual(spread, 7.5417, delta=0.05)
        # Applying exactly that spread lands the ways together.
        landed = self.alignment.confirmation(
            self.take(arrivals_ms={"left_low": spread, "left_high": 7.5417}))
        residual = max(landed["arrival_ms"].values()) - min(landed["arrival_ms"].values())
        self.assertLessEqual(residual, 0.02)

    def test_planned_offset_survives_level_spread_and_reflections(self):
        """The offset is a band arrival, not a property of the take's artifacts."""
        geometry = {"left_low": 0.0, "left_high": 7.5417}
        for options in ({"gains_db": {"left_low": 6.0, "left_high": -6.0}},
                        {"reflections": [(0.0035, 0.7)]},
                        {"gains_db": {"left_low": -9.0, "left_high": 3.0},
                         "reflections": [(0.006, 0.5), (0.011, -0.4)]}):
            with self.subTest(options=options):
                document = takes.planning_document(self.alignment, geometry, **options)
                arrivals = document["arrival_ms"]
                self.assertAlmostEqual(max(arrivals.values()) - min(arrivals.values()),
                                       7.5417, delta=0.05)

    def test_confirmation_reports_the_shared_take_offset(self):
        document = self.alignment.confirmation(
            self.take(arrivals_ms={"left_low": 7.5417, "left_high": 0.0}))
        self.assertEqual(set(document["arrival_ms"]), {"left_low", "left_high"})
        spread = max(document["arrival_ms"].values()) - min(document["arrival_ms"].values())
        self.assertAlmostEqual(spread, 7.5417, delta=0.05)
        self.assertTrue(document["band_limited"])
        self.assertEqual(document["start_revision"], 7)
        self.assertEqual(document["processing_fingerprint"], "frozen-plan")
        for margin in document["way_isolation_db"].values():
            self.assertIsNotNone(margin)

    def test_compensated_shared_take_reads_as_aligned(self):
        document = self.alignment.confirmation(
            self.take(arrivals_ms={"left_low": 7.5417, "left_high": 7.5417}))
        spread = max(document["arrival_ms"].values()) - min(document["arrival_ms"].values())
        self.assertLessEqual(spread, 0.02)
        self.assertEqual(set(document["way_isolation_db"].values()), {None})

    def test_confirmation_rejects_takes_that_are_not_this_side(self):
        def mutate(mutator):
            take = self.take()
            mutator(take)
            return take

        def pop_mic_pos(take):
            take["microphone_position_id"] = "other-position"

        def change_target(take):
            take["measurement_target"] = self.alignment.capture_requests()[0]["measurement_target"]

        def change_tap(take):
            take["reference_tap"] = "host-monitor"

        def change_origin(take):
            take["time_reference"] = "host-reference"

        def change_rate(take):
            take["analysis"]["sample_rate"] = RATE // 2

        def downgrade_quality(take):
            take["analysis"]["quality_checks"] = {
                "status": "fail", "items": [{"level": "error", "message": "silent"}]}

        def clip_mic(take):
            take["analysis"]["peak_dbfs"] = 0.0

        def clip_reference(take):
            take["analysis"]["reference_path"]["clipped"] = True

        def fallback_reference(take):
            take["analysis"]["reference_path"]["electrical_reference_fallback"] = True

        def host_reference(take):
            take["analysis"]["reference_path"]["electrical_reference_used"] = False

        for label, mutator in (("position", pop_mic_pos), ("target", change_target),
                               ("tap", change_tap), ("origin", change_origin),
                               ("rate", change_rate), ("quality", downgrade_quality),
                               ("mic clip", clip_mic), ("reference clip", clip_reference),
                               ("fallback", fallback_reference), ("host reference", host_reference)):
            with self.subTest(rejected=label), self.assertRaises(ValueError):
                self.alignment.confirmation(mutate(mutator))

    def test_confirmation_rejects_a_take_without_an_impulse_response(self):
        take = self.take()
        take["impulse_response"] = np.zeros(4096)
        with self.assertRaises(ValueError):
            self.alignment.confirmation(take)
        with self.assertRaises(ValueError):
            self.alignment.confirmation({})

    def test_way_level_spread_is_reported_but_not_a_timing_decision(self):
        document = self.alignment.confirmation(
            self.take(arrivals_ms={"left_low": 0.0, "left_high": 0.0},
                      rates={"left_low": 2.0, "left_high": 1.0}))
        levels = document["way_levels_db"]
        self.assertAlmostEqual(levels["left_low"], 0.0, delta=0.5)
        self.assertAlmostEqual(levels["left_high"], -6.0, delta=1.0)
        spread = max(document["arrival_ms"].values()) - min(document["arrival_ms"].values())
        self.assertLessEqual(spread, 0.02)


class RenderedCrossoverTests(unittest.TestCase):
    """Speaker Align models each way with every filter the plan renders on it.

    The configuration of the real right-side failure: stereo-sub with two subs
    routed, LR24 1 kHz / 3 kHz ways and an LR24 82 Hz Main high-pass. The
    woofer's own low-frequency roll-off is part of the take, not of the model.
    """

    ROUTES = ["left_low", "right_low", "sub1", "sub2", "left_high", "right_high"]
    LOW_BEHIND_HIGH_MS = 0.354

    def state(self, *, main_highpass_enabled=True):
        state, _ = state_for(cutoffs=(1000,))
        state = set_mode_routing(state, "stereo-sub", "dev", self.ROUTES)
        state = set_bass_management(state, "stereo-sub", frequency_hz=82,
                                    main_highpass_enabled=main_highpass_enabled)
        for side in ("left", "right"):
            state["modes"]["stereo-sub"]["processing"][f"{side}_high"]["highpass"]["frequency_hz"] = 3000
        return state

    def alignment(self, state):
        return alignment_for(state, len(self.ROUTES), side="right")

    def test_way_models_carry_the_rendered_main_highpass(self):
        alignment, _ = self.alignment(self.state())
        main_highpass = {"kind": "highpass", "family": "linkwitz-riley", "slope_db_oct": 24,
                         "frequency_hz": 82}
        models = alignment.way_models()
        self.assertEqual(set(models), {"right_low", "right_high"})
        for role in models:
            self.assertIn(main_highpass, models[role]["crossover"])
        models["right_low"]["crossover"].clear()
        self.assertIn(main_highpass, alignment.way_models()["right_low"]["crossover"])
        alignment, _ = self.alignment(self.state(main_highpass_enabled=False))
        self.assertEqual(alignment.way_models()["right_low"]["crossover"],
                         [{"kind": "lowpass", "family": "linkwitz-riley", "slope_db_oct": 24,
                           "frequency_hz": 1000}])

    def test_right_side_with_main_highpass_plans_the_real_offset(self):
        alignment, live = self.alignment(self.state())
        frequencies = np.fft.rfftfreq(takes.TAKE_SAMPLES, 1.0 / RATE)
        corner = 1j * frequencies / 100.0
        woofer = corner ** 2 / (corner ** 2 + corner / 0.7 + 1.0)
        spectrum = np.zeros_like(frequencies, dtype=complex)
        models = alignment.way_models()
        for role, arrival_ms, gain_db, driver in (
                ("right_low", self.LOW_BEHIND_HIGH_MS, 0.0, woofer),
                ("right_high", 0.0, 11.0, 1.0)):
            delay = takes.ORIGIN_SAMPLES + int(round(arrival_ms * RATE / 1000.0))
            spectrum += (10.0 ** (gain_db / 20.0) * driver * takes.way_response(models, role)
                         * np.exp(-2j * np.pi * frequencies * delay / RATE))
        request = alignment.planning_request()
        planning = alignment.planning(takes.take_document(
            alignment, request, np.fft.irfft(spectrum, n=takes.TAKE_SAMPLES)))
        proposal = alignment.propose(captures_for(alignment, cutoffs=(1000,)),
                                     planning=planning, live_target=live)
        self.assertEqual(proposal["reference_role"], "right_low")
        self.assertEqual(proposal["added_delay_ms"]["right_low"], 0.0)
        self.assertAlmostEqual(proposal["added_delay_ms"]["right_high"],
                               self.LOW_BEHIND_HIGH_MS, delta=0.1)


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
        state["modes"]["stereo-sub"]["processing"]["left_low"]["alignment_ms"] = -2.0
        state["modes"]["stereo-sub"]["processing"]["left_high"]["alignment_ms"] = 1.0
        original = copy.deepcopy(state)
        alignment, live = alignment_for(state, channels)
        captures = captures_for(alignment)
        original_ir = captures[0]["impulse_response"].copy()
        proposal = alignment.propose(
            captures, planning=planning_for(alignment), live_target=live)
        # The shared take reports arrivals relative to its earliest way; the
        # planned delays are the same max-minus-arrival as before.
        self.assertEqual(proposal["arrival_ms"], {"left_low": 0.0, "left_high": 3.0})
        self.assertEqual(proposal["added_delay_ms"], {"left_low": 3.0, "left_high": 0.0})
        self.assertEqual(proposal["arrival_source"], "shared-planning-take")
        expected = copy.deepcopy(original)
        expected["modes"]["stereo-sub"]["processing"]["left_low"]["alignment_ms"] = 1.0
        self.assertEqual(proposal["candidate_state"], expected)
        self.assertEqual(state, original)
        np.testing.assert_array_equal(captures[0]["impulse_response"], original_ir)
        self.assertEqual(proposal["reference_role"], "left_high")

    def test_three_and_four_ways_follow_configured_roles_and_latest_arrival(self):
        # The shared take locates a way only as well as that way's own band
        # allows. The widest band here (0-300 Hz) has a ~80-sample lobe, so the
        # low way's arrival is reproduced to a few samples rather than exactly:
        # its delay is asserted at that resolution. The fixture spacing is wider
        # than every band's own lobe, so no arrival sits inside a neighbour's.
        for ways, cutoffs, arrivals, delays in (
            (("low", "mid", "high"), (300, 2500), (96, 336, 576), (10.0, 5.0, 0.0)),
            (("low", "low_mid", "mid", "high"), (300, 1000, 3000),
             (96, 336, 576, 816), (15.0, 10.0, 5.0, 0.0)),
        ):
            with self.subTest(ways=ways):
                state, channels = state_for(ways, cutoffs)
                alignment, live = alignment_for(state, channels, side="right")
                proposal = proposal_for(alignment, live, arrivals, cutoffs=cutoffs)
                roles = [f"right_{way}" for way in ways]
                self.assertEqual(list(proposal["arrival_ms"]), roles)
                for role, delay in zip(roles, delays):
                    self.assertAlmostEqual(proposal["added_delay_ms"][role], delay, delta=0.1)

    def test_delay_past_the_window_rebases_the_candidate_within_it(self):
        """A span that still fits +-40 ms is shifted, not rejected or clamped."""
        state, channels = state_for()
        state["modes"]["stereo-sub"]["processing"]["left_low"]["alignment_ms"] = 39.0
        alignment, live = alignment_for(state, channels)
        # The 3 ms plan would land left_low at 42 ms; every routed role moves
        # by one common offset instead, so the 42 ms span is preserved.
        result = proposal_for(alignment, live)
        processing = result["candidate_state"]["modes"]["stereo-sub"]["processing"]
        self.assertEqual(processing["left_low"]["alignment_ms"], 40.0)
        self.assertEqual(processing["left_high"]["alignment_ms"], -2.0)
        for role in ("right_low", "right_high", "sub1"):
            self.assertEqual(processing[role]["alignment_ms"], -2.0, role)
        self.assertEqual(result["added_delay_ms"], {"left_low": 3.0, "left_high": 0.0})
        self.assertEqual(processing["left_low"]["alignment_ms"]
                         - processing["left_high"]["alignment_ms"], 42.0)

    def test_rebase_keeps_both_sides_and_the_sub_timed_in_the_rendered_plan(self):
        """Left and Right Align rebase every routed role by one offset."""
        for side, other in (("left", "right"), ("right", "left")):
            with self.subTest(side=side):
                state, channels = state_for()
                processing = state["modes"]["stereo-sub"]["processing"]
                processing[f"{side}_low"]["alignment_ms"] = 39.0
                processing[f"{side}_high"]["alignment_ms"] = 1.5
                processing[f"{other}_low"]["alignment_ms"] = -10.0
                processing[f"{other}_high"]["alignment_ms"] = 12.25
                processing["sub1"]["alignment_ms"] = 3.0
                original = copy.deepcopy(processing)
                alignment, live = alignment_for(state, channels, side=side)
                result = proposal_for(alignment, live)
                self.assertEqual(result["added_delay_ms"],
                                 {f"{side}_low": 3.0, f"{side}_high": 0.0})
                candidate = result["candidate_state"]["modes"]["stereo-sub"]["processing"]
                # 39 + 3 = 42 ms: every routed role moves by the same -2 ms.
                self.assertEqual(candidate[f"{side}_low"]["alignment_ms"], 40.0)
                for role in (f"{side}_high", f"{other}_low", f"{other}_high", "sub1"):
                    self.assertEqual(candidate[role]["alignment_ms"],
                                     original[role]["alignment_ms"] - 2.0, role)
                # Dormant roles outside the routed topology stay untouched.
                for role in ("main_l", "main_r"):
                    self.assertEqual(candidate[role], original[role], role)
                assert_timing_kept(self, state, result, channels)

    def test_rebase_the_other_roles_cannot_follow_is_rejected(self):
        """No shift of one side alone: an offset the routed roles cannot hold fails."""
        for side, other in (("left", "right"), ("right", "left")):
            for blocker in (f"{other}_low", "sub1"):
                with self.subTest(side=side, blocker=blocker):
                    state, channels = state_for()
                    processing = state["modes"]["stereo-sub"]["processing"]
                    processing[f"{side}_low"]["alignment_ms"] = 39.0
                    # The -2 ms common offset would put this role at -41.5 ms.
                    processing[blocker]["alignment_ms"] = -39.5
                    original = copy.deepcopy(state)
                    alignment, live = alignment_for(state, channels, side=side)
                    with self.assertRaisesRegex(
                            ValueError, r"needs 81\.500 ms between routed outputs"):
                        proposal_for(alignment, live)
                    self.assertEqual(state, original)

    def test_real_41ms_way_delay_rebases_every_routed_role_to_the_window(self):
        """The real 3-way plan: 41.625 ms plus a stored 0.479166 ms must validate.

        right_mid and right_high arrive 1998 samples (41.625 ms) before
        right_low, right_high already stores 0.479166 ms and left_high stores
        6.90666 ms, exactly like the live run that used to die on
        ``Output alignment must be finite and between -40 and 40``.
        """
        from measurement.speaker_commit import require_speaker_candidate
        state, channels = state_for(("low", "mid", "high"), (300, 2500))
        start = state["modes"]["stereo-sub"]["processing"]
        start["right_high"]["alignment_ms"] = 0.479166
        start["left_high"]["alignment_ms"] = 6.90666
        alignment, live = alignment_for(state, channels, side="right")
        proposal = proposal_for(alignment, live, (2400, 402, 402), cutoffs=(300, 2500))
        for role, delay in (("right_low", 0.0), ("right_mid", 41.625), ("right_high", 41.625)):
            self.assertAlmostEqual(proposal["added_delay_ms"][role], delay, delta=0.1)
        self.assertEqual(proposal["reference_role"], "right_low")
        routed = ("left_low", "left_mid", "left_high",
                  "right_low", "right_mid", "right_high", "sub1")
        candidate = proposal["candidate_state"]["modes"]["stereo-sub"]["processing"]
        # The largest value lands exactly on the +40 ms guard, and every
        # routed role shares the same minimal offset (-2.104166 ms).
        self.assertEqual(candidate["right_high"]["alignment_ms"], 40.0)
        for role in ("left_low", "left_mid", "right_low", "sub1"):
            self.assertAlmostEqual(candidate[role]["alignment_ms"], -2.104166, delta=0.1)
        self.assertAlmostEqual(candidate["right_mid"]["alignment_ms"], 39.520834, delta=0.1)
        self.assertAlmostEqual(candidate["left_high"]["alignment_ms"], 4.802494, delta=0.1)
        # Roles outside the topology keep their stored alignment untouched.
        for role, settings in candidate.items():
            if role not in routed:
                self.assertEqual(settings["alignment_ms"], start[role]["alignment_ms"], role)
        # The unshifted intent: stored alignment plus the planned acoustic
        # delays, exactly what the candidate held before the rebase.
        intent = {role: start[role]["alignment_ms"] + proposal["added_delay_ms"].get(role, 0.0)
                  for role in routed}
        for first in routed:
            for second in routed:
                self.assertAlmostEqual(
                    candidate[first]["alignment_ms"] - candidate[second]["alignment_ms"],
                    intent[first] - intent[second], delta=1e-9,
                    msg=f"relative delay changed: {first} vs {second}")
        # The rendered plan equals the intent the proposal always meant:
        # Left, Right and the sub keep their timing against each other.
        planned = plan_delays(proposal["candidate_state"], channels)
        for role in routed:
            self.assertAlmostEqual(planned[role], intent[role], delta=1e-9, msg=role)
        assert_timing_kept(self, state, proposal, channels)
        # The commit gate still admits the rebased candidate.
        accepted = require_speaker_candidate(
            state, proposal["candidate_state"], output_key="dev", channels=channels)
        self.assertEqual(accepted["revision"], state["revision"])

    def test_equal_arrivals_produce_an_unchanged_candidate(self):
        state, channels = state_for()
        alignment, live = alignment_for(state, channels)
        result = proposal_for(alignment, live, (144, 144))
        self.assertEqual(result["candidate_state"], state)

    def test_frozen_start_is_detached_and_proposals_never_accumulate(self):
        state, channels = state_for()
        alignment, live = alignment_for(state, channels)
        state["modes"]["stereo-sub"]["processing"]["left_low"]["alignment_ms"] = 20
        first = proposal_for(alignment, live)
        first["candidate_state"]["modes"]["stereo-sub"]["processing"]["left_low"]["alignment_ms"] = 30
        second = proposal_for(alignment, live)
        self.assertEqual(second["candidate_state"]["modes"]["stereo-sub"]["processing"]["left_low"]["alignment_ms"], 3.0)

    def test_causal_lr12_and_lr24_with_real_arrival_detection(self):
        for rate in (44100, 48000, 96000):
            for slope in (12, 24):
                with self.subTest(rate=rate, slope=slope):
                    state, channels = state_for()
                    for settings in state["modes"]["stereo-sub"]["processing"].values():
                        for kind in ("lowpass", "highpass"):
                            if settings[kind] is not None:
                                settings[kind]["slope_db_oct"] = slope
                    if slope == 12:
                        state["modes"]["stereo-sub"]["processing"]["left_high"]["polarity"] = "invert"
                    alignment, live = alignment_for(state, channels, rate=rate)
                    proposal = alignment.propose(
                        native_captures(alignment, slope=slope, rate=rate),
                        planning=planning_for(alignment, rate=rate), live_target=live)
                    # Known acoustic offset is 144 samples; small peak-detector
                    # quantization/group delay is admitted, not a whole cycle.
                    self.assertAlmostEqual(proposal["added_delay_ms"]["left_low"], 144000 / rate, delta=0.06)

    def test_host_monitor_reference_supports_a_microphone_only_setup(self):
        alignment, live = alignment_for()
        captures = captures_for(alignment, (240, 96))
        for capture in captures:
            capture["analysis"]["reference_path"].update(
                electrical_reference_used=False, timing_status="acoustic-only",
                stability="host-reference", capture_mode="dual-channel",
                timing_applied_to_mic=True, start_score=1.0, end_score=1.0, ir_sharpness_db=48.0)
            capture["reference_node"] = REFERENCE_TAP_INGRESS
        result = alignment.propose(
            captures, planning=planning_for(alignment, (240, 96)), live_target=live)
        self.assertEqual(result["reference_role"], "left_low")
        self.assertEqual(result["added_delay_ms"], {"left_low": 0.0, "left_high": 3.0})

    def test_levels_polarity_and_overlap_do_not_turn_timing_into_gain_optimization(self):
        state, channels = state_for()
        state["modes"]["stereo-sub"]["processing"]["left_high"]["level_db"] = -18
        alignment, live = alignment_for(state, channels)
        captures = captures_for(alignment, cutoffs=(400,))
        captures[1]["impulse_response"] *= -0.05
        result = alignment.propose(
            captures, planning=planning_for(alignment), live_target=live)
        expected = copy.deepcopy(state)
        expected["modes"]["stereo-sub"]["processing"]["left_low"]["alignment_ms"] = 3
        self.assertEqual(result["candidate_state"], expected)


class TimingReferenceGateTests(unittest.TestCase):
    """The gate must not re-decide what the store's ER evaluation already decided."""

    def require(self, reference, reference_node=None):
        require_timing_reference({"reference_path": dict(reference)}, reference_node)

    def assert_rejected(self, reference, reference_node=None):
        with self.assertRaisesRegex(ValueError, "reference"):
            self.require(reference, reference_node)

    @staticmethod
    def electrical(**overrides):
        return {
            "usable": True, "electrical_reference_used": True,
            "timing_status": "electrical-reference", "stability": "stable",
            "confidence": 0.97, "clipped": False, "peak_dbfs": -9.0,
            **overrides,
        }

    @staticmethod
    def ingress(**overrides):
        return {
            "electrical_reference_used": False, "timing_status": "acoustic-only",
            "stability": "host-reference", "capture_mode": "dual-channel",
            "timing_applied_to_mic": True, "start_score": 1.0, "end_score": 1.0,
            "ir_sharpness_db": 48.0, "confidence": 0.95,
            **overrides,
        }

    def test_band_limited_reference_below_the_old_confidence_floor_is_admitted(self):
        # A left_low/right_low way sweep is band-limited by its own crossover, so
        # the electrical reference lands near 37-43 dB sharpness and confidence
        # min(score, sharpness/60) lands near 0.62-0.72 while the store's own
        # 0.84 alignment and 18 dB sharpness floors stay satisfied.
        self.require(self.electrical(confidence=0.62, ir_sharpness_db=37.4,
                                     start_score=0.96, end_score=0.95))

    def test_plain_stable_reference_is_still_admitted(self):
        self.require(self.electrical())

    def test_tolerated_end_anchor_reference_is_admitted_like_the_store_does(self):
        # store._mark_dsp_tolerated_electrical_reference_usable records an
        # accepted ER with stability "dsp-end-anchor-tolerated".
        self.require(self.electrical(stability="dsp-end-anchor-tolerated", confidence=0.72))

    def test_fallback_unusable_or_malformed_reference_is_still_rejected(self):
        for reference in (
            self.electrical(electrical_reference_fallback=True),
            self.electrical(electrical_reference_used=False),
            self.electrical(usable=False),
            self.electrical(timing_status="electrical-reference-fallback"),
            self.electrical(timing_status="lr-repeat-unstable"),
            self.electrical(confidence=float("nan")),
            self.electrical(confidence=1.5),
            self.electrical(confidence=None),
        ):
            with self.subTest(reference=reference):
                self.assert_rejected(reference)

    def test_ingress_reference_keeps_its_own_confidence_gate(self):
        self.require(self.ingress(), REFERENCE_TAP_INGRESS)
        for confidence in (0.5, 0.74, 1.5):
            with self.subTest(confidence=confidence):
                self.assert_rejected(self.ingress(confidence=confidence), REFERENCE_TAP_INGRESS)
        # Unchanged host requirements: the ingress node, dual-channel capture
        # and applied mic timing remain mandatory.
        self.assert_rejected(self.ingress(), "hardware.monitor")
        self.assert_rejected(self.ingress(capture_mode="electrical-input"), REFERENCE_TAP_INGRESS)
        self.assert_rejected(self.ingress(timing_applied_to_mic=False), REFERENCE_TAP_INGRESS)
        self.assert_rejected(self.ingress(start_score=0.5), REFERENCE_TAP_INGRESS)
        self.assert_rejected(self.ingress(ir_sharpness_db=12.0), REFERENCE_TAP_INGRESS)


class BandLimitedReferenceProposalTests(unittest.TestCase):
    """End-to-end: a band-limited but store-accepted ER must still align."""

    def setUp(self):
        self.alignment, self.live = alignment_for()

    def align_with(self, **reference_overrides):
        captures = captures_for(self.alignment)
        for capture in captures:
            capture["analysis"]["reference_path"].update(reference_overrides)
        return captures, self.alignment.propose(
            captures, planning=planning_for(self.alignment), live_target=self.live)

    def test_band_limited_reference_below_the_old_confidence_floor_still_aligns(self):
        _, proposal = self.align_with(confidence=0.62, ir_sharpness_db=37.4,
                                      start_score=0.96, end_score=0.95)
        self.assertEqual(proposal["added_delay_ms"], {"left_low": 3.0, "left_high": 0.0})

    def test_tolerated_end_anchor_reference_still_aligns(self):
        _, proposal = self.align_with(stability="dsp-end-anchor-tolerated", confidence=0.72)
        self.assertEqual(proposal["added_delay_ms"], {"left_low": 3.0, "left_high": 0.0})

    def test_stable_reference_is_still_accepted(self):
        _, proposal = self.align_with()
        self.assertEqual(proposal["added_delay_ms"], {"left_low": 3.0, "left_high": 0.0})


class RejectionTests(unittest.TestCase):
    def setUp(self):
        self.alignment, self.live = alignment_for()
        self.captures = captures_for(self.alignment)
        self.planning = planning_for(self.alignment)

    def assert_rejected(self, text):
        with self.assertRaisesRegex(ValueError, text):
            self.alignment.propose(self.captures, planning=self.planning, live_target=self.live)

    def test_host_reference_from_a_downstream_node_is_rejected(self):
        for capture in self.captures:
            capture["analysis"]["reference_path"].update(
                electrical_reference_used=False, timing_status="acoustic-only",
                stability="host-reference", capture_mode="dual-channel",
                timing_applied_to_mic=True, start_score=1.0, end_score=1.0, ir_sharpness_db=48.0)
            capture["reference_node"] = "hardware.monitor"
        self.assert_rejected("reference")

    def test_missing_duplicate_and_foreign_ways_are_rejected(self):
        original = self.captures
        for invalid in (original[:1], original + original[:1],
                        [original[0], {**original[1], "role": "sub1"}]):
            with self.subTest(roles=[item["role"] for item in invalid]):
                self.captures = invalid
                self.assert_rejected("way|role")

    def test_fallback_unusable_or_malformed_reference_is_rejected(self):
        for change in ({"stability": "unstable", "timing_status": "lr-repeat-unstable"},
                       {"electrical_reference_used": False}, {"usable": False},
                       {"confidence": float("nan")}, {"confidence": 1.5},
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

    def test_per_way_direct_arrival_metadata_no_longer_plans_or_blocks(self):
        """The shared take plans the delays; a per-way early-candidate pick cannot.

        The per-way path used to take each way's delay from that one capture's
        own direct-arrival estimate and to reject the run when the estimate was
        not confident. Both outcomes came from one take's noise and lobe
        structure, not from the way's arrival on the side's common time base, so
        neither may veto the plan nor move it.
        """
        baseline = self.alignment.propose(
            self.captures, planning=self.planning, live_target=self.live)
        self.assertEqual(baseline["added_delay_ms"], {"left_low": 3.0, "left_high": 0.0})
        for change in ({"direct_arrival_index": -1}, {"reference_peak_index": 5000},
                       {"direct_arrival_index": 100.5}, {"direct_confidence": 0.1},
                       {"direct_confidence": float("nan")},
                       {"timing_source": "independent-peak-zero"}):
            with self.subTest(change=change):
                captures = captures_for(self.alignment)
                captures[0]["analysis"]["impulse_response"].update(change)
                proposal = self.alignment.propose(
                    captures, planning=self.planning, live_target=self.live)
                self.assertEqual(proposal["added_delay_ms"], baseline["added_delay_ms"])
                self.assertEqual(proposal["arrival_source"], "shared-planning-take")

    def test_planning_take_shape_is_gated(self):
        for planning in (None, {}, {"arrival_ms": {"left_low": 0.0}},
                         {"arrival_ms": {"left_low": 0.0, "left_high": 3.0}}):
            with self.subTest(planning=planning):
                with self.assertRaisesRegex(ValueError, "planning"):
                    self.alignment.propose(
                        self.captures, planning=planning, live_target=self.live)

    def test_planning_take_that_cannot_separate_the_ways_fails_closed(self):
        """A take whose bands cannot resolve the ways plans nothing, not a guess."""
        state, channels = state_for(("low", "low_mid", "mid", "high"), (300, 1000, 3000))
        alignment, live = alignment_for(state, channels, side="right")
        arrivals = (96, 144, 192, 240)
        with self.assertRaisesRegex(ValueError, "cannot separate the ways"):
            alignment.propose(
                captures_for(alignment, arrivals, cutoffs=(300, 1000, 3000)),
                planning=planning_for(alignment, arrivals), live_target=live)

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
                self.alignment.propose(self.captures, planning=self.planning,
                                       live_target=self.live, cancel_requested=cancelled)

    def test_span_beyond_eighty_milliseconds_is_rejected_instead_of_clamped(self):
        """Routed roles spanning more than 80 ms cannot fit +-40 ms; reject them."""
        state, channels = state_for()
        processing = state["modes"]["stereo-sub"]["processing"]
        processing["left_low"]["alignment_ms"] = 40.0
        processing["left_high"]["alignment_ms"] = -40.0
        self.alignment, self.live = alignment_for(state, channels)
        self.captures = captures_for(self.alignment)
        # The 3 ms plan would span 83 ms: a common offset would put
        # left_high below -40 ms, so the proposal is rejected, not clamped.
        self.assert_rejected("needs 83\\.000 ms between routed outputs")

    def test_unsupported_topology_and_missing_overlap_filters_fail_before_capture(self):
        for mutate in (
            lambda state: state.update(active_mode="stereo"),
            lambda state: state["modes"]["stereo-sub"]["processing"]["left_low"].update(lowpass=None),
        ):
            with self.subTest(mutate=mutate):
                state, channels = state_for()
                mutate(state)
                with self.assertRaises(ValueError):
                    alignment_for(state, channels)



class LostWayTests(unittest.TestCase):
    """A way lost in its neighbour's leak neither plans nor verifies.

    Far enough below its neighbour, a way's isolated band peaks on the
    neighbour's leak: both arrivals collapse onto one index, no arrival is
    apart and the take used to read as aligned (``None`` isolation, 0 ms
    residual) whatever the real offset. A coincidence the way's own energy
    cannot tell from that leak must now report its margin instead.
    """

    GEOMETRY = {"left_low": 5.0, "left_high": 12.5}

    def setUp(self):
        self.alignment, self.live = alignment_for()
        self.baseline = proposal_for(self.alignment, self.live)

    def confirm(self, arrival_ms, **take_options):
        from measurement.speaker_apply import verify_confirmation
        document = takes.confirmation_document(self.alignment, arrival_ms, **take_options)
        return document, verify_confirmation(self.baseline, document)

    def test_real_offset_of_a_quiet_way_is_not_confirmed(self):
        from measurement.speaker_verification import MIN_WAY_ISOLATION_DB
        for gain_db in (-20.0, -25.0):
            for offset_ms in (1.0, 7.5):
                arrivals = {"left_low": 5.0, "left_high": 5.0 + offset_ms}
                with self.subTest(gain_db=gain_db, offset_ms=offset_ms):
                    document, check = self.confirm(arrivals, gains_db={"left_low": gain_db})
                    self.assertFalse(check["confirmed"], check)
                    self.assertLess(document["way_isolation_db"]["left_low"], MIN_WAY_ISOLATION_DB)
                    self.assertTrue(any("cannot separate the ways" in reason
                                        for reason in check["reasons"]), check["reasons"])

    def test_collapsed_arrival_reports_its_margin_instead_of_none(self):
        # -25 dB with a 7.5 ms offset: the low band peaks on the high way's
        # leak, the residual reads 0 ms. It used to confirm with None/None.
        document, check = self.confirm(self.GEOMETRY, gains_db={"left_low": -25.0})
        spread = max(document["arrival_ms"].values()) - min(document["arrival_ms"].values())
        self.assertLessEqual(spread, 0.02, "the arrivals still collapse onto one peak")
        self.assertIsNotNone(document["way_isolation_db"]["left_low"])
        self.assertLess(document["way_isolation_db"]["left_low"], 3.0)
        self.assertFalse(check["confirmed"])

    def test_exactly_aligned_ways_still_confirm_without_a_margin(self):
        for gains in ({}, {"left_low": -6.0}, {"left_high": -20.0}):
            with self.subTest(gains=gains):
                document, check = self.confirm(
                    {"left_low": 5.0, "left_high": 5.0}, gains_db=gains)
                self.assertEqual(document["way_isolation_db"], {"left_low": None, "left_high": None})
                self.assertTrue(check["confirmed"], check["reasons"])

    def test_aligned_quieter_way_limits_in_both_directions(self):
        # Pinned limits of the coincidence check (flat leak model, no driver
        # headroom, the 10 dB isolation gate). The wide high band keeps a
        # quiet high way separable far down; the narrow low band loses a quiet
        # low way to the high way's leak sooner. Driver headroom here would
        # refuse even equally loud aligned ways, so it stays out.
        aligned = {"left_low": 5.0, "left_high": 5.0}
        for quiet, gains in (("left_high", (-10.0, -20.0, -25.0)), ("left_low", (-6.0, -10.0))):
            for gain_db in gains:
                with self.subTest(quiet=quiet, gain_db=gain_db):
                    document, check = self.confirm(aligned, gains_db={quiet: gain_db})
                    self.assertEqual(document["way_isolation_db"],
                                     {"left_low": None, "left_high": None})
                    self.assertTrue(check["confirmed"], check["reasons"])

    def test_refused_aligned_way_is_refused_apart_as_well(self):
        # A low way 15 dB down cannot be told from the high way's leak: aligned
        # it reports its coincidence margin, 1 ms apart the measured isolation
        # is below the gate too. Neither reading confirms.
        for arrivals in ({"left_low": 5.0, "left_high": 5.0},
                         {"left_low": 5.0, "left_high": 6.0}):
            with self.subTest(arrivals=arrivals):
                document, check = self.confirm(arrivals, gains_db={"left_low": -15.0})
                self.assertLess(document["way_isolation_db"]["left_low"], 10.0)
                self.assertFalse(check["confirmed"])

    def test_misaligned_quiet_high_way_is_not_confirmed(self):
        # The other direction of a real offset: a quiet high way stays apart
        # (no collapse), so the residual or its isolation refuses it.
        for gain_db in (-20.0, -25.0, -30.0):
            with self.subTest(gain_db=gain_db):
                document, check = self.confirm(self.GEOMETRY, gains_db={"left_high": gain_db})
                spread = max(document["arrival_ms"].values()) - min(document["arrival_ms"].values())
                self.assertAlmostEqual(spread, 7.5, delta=0.05)
                self.assertFalse(check["confirmed"])

    def test_planning_take_with_a_lost_way_plans_nothing(self):
        roles = [request["role"] for request in self.alignment.capture_requests()]
        for gain_db in (-20.0, -25.0):
            with self.subTest(gain_db=gain_db):
                planning = takes.planning_document(
                    self.alignment, self.GEOMETRY, gains_db={"left_low": gain_db})
                with self.assertRaisesRegex(ValueError, "cannot separate the ways"):
                    self.alignment.propose(captures_for(self.alignment),
                                           planning=planning, live_target=self.live)
        # The aligned, equally loud take still plans its zero delay.
        planning = takes.planning_document(self.alignment, {role: 5.0 for role in roles})
        proposal = self.alignment.propose(captures_for(self.alignment),
                                          planning=planning, live_target=self.live)
        self.assertEqual(set(proposal["added_delay_ms"].values()), {0.0})


if __name__ == "__main__":
    unittest.main(verbosity=2)
