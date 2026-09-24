#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Trial apply must prove the improvement acoustically, then restore."""

import asyncio
import os
from copy import deepcopy
import sys
import threading
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import speaker_take_test_support as takes
from audio.output_state import default_output_state, set_mode_routing, switch_mode, set_crossover
from measurement.speaker_align import SpeakerAlignment
from measurement.speaker_apply import (
    MAX_CONFIRMED_RESIDUAL_MS,
    apply_and_confirm,
    verify_confirmation,
)
from measurement.target import REFERENCE_TAP_INGRESS, freeze_measurement_target

RATE = 48000


def crossover_state():
    state = default_output_state()
    routes = [f"{side}_{way}" for side in ("right", "left") for way in ("high", "low")]
    routes += ["sub1", "left_low"]
    state = set_mode_routing(set_crossover(state, "stereo-sub", True), "stereo-sub", "dev", routes)
    state = switch_mode(state, "stereo-sub")
    state["revision"] = 7
    processing = state["modes"]["stereo-sub"]["processing"]
    for side in ("left", "right"):
        processing[f"{side}_low"]["lowpass"] = {
            "family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 2000,
        }
        processing[f"{side}_high"]["highpass"] = {
            "family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 2000,
        }
    return state


def alignment_and_live(state):
    alignment = SpeakerAlignment(
        state, side="left", output_key="dev", channels=6, sample_rate_hz=RATE,
        fingerprint="frozen-plan", reference_id="interface:input-2:upstream",
        microphone_position_id="seat-1-fixed",
    )
    live = freeze_measurement_target(
        state, bank_id="global", output_key="dev", channels=6,
        sample_rate_hz=RATE, fingerprint="frozen-plan",
    )
    return alignment, live


def planning_from(alignment, arrivals):
    """One shared planning take at the given per-way sample offsets."""
    roles = [request["role"] for request in alignment.capture_requests()]
    return takes.planning_document(
        alignment, {role: arrivals[index] * 1000.0 / RATE for index, role in enumerate(roles)})


def confirmation_from(alignment, arrivals):
    """One shared verification take at the given per-way sample offsets."""
    roles = [request["role"] for request in alignment.capture_requests()]
    return takes.confirmation_document(
        alignment, {role: arrivals[index] * 1000.0 / RATE for index, role in enumerate(roles)})


def planned(alignment, live, arrivals, captures):
    """A proposal whose relative delays come from one shared planning take."""
    return alignment.propose(
        captures, planning=planning_from(alignment, arrivals), live_target=live)


def lowpass_kernel(cutoff):
    offsets = np.arange(-256, 257, dtype=float)
    sigma = np.sqrt(2 * np.log(2)) * RATE / (2 * np.pi * cutoff)
    kernel = np.exp(-0.5 * (offsets / sigma) ** 2)
    return kernel / np.sum(kernel)


def captures_for(alignment, arrivals):
    """Linear-phase band-limited IRs with independently known arrival offsets."""
    delta = np.zeros(513)
    delta[256] = 1
    lowpass = lowpass_kernel(2000)
    boundaries = [np.zeros(513), lowpass, delta]
    captures = []
    for index, request in enumerate(alignment.capture_requests()):
        reference = 700 + index * 131
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


class VerifyConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.state = crossover_state()
        self.alignment, self.live = alignment_and_live(self.state)
        self.baseline = planned(self.alignment, self.live, (96, 240),
                                captures_for(self.alignment, (96, 240)))

    def test_aligned_confirmation_confirms(self):
        confirmation = confirmation_from(self.alignment, (500, 500))
        check = verify_confirmation(self.baseline, confirmation)
        self.assertTrue(check["confirmed"])
        self.assertAlmostEqual(check["max_residual_ms"], 0.0, delta=0.05)
        self.assertEqual(len(check["pairs"]), 1)

    def test_residual_offset_rejects(self):
        confirmation = confirmation_from(self.alignment, (500, 548))
        check = verify_confirmation(self.baseline, confirmation)
        self.assertFalse(check["confirmed"])
        self.assertGreater(check["max_residual_ms"], MAX_CONFIRMED_RESIDUAL_MS)
        self.assertIn("residual", check["reasons"][0])

    def test_confirmation_reports_measured_arrivals_and_limit(self):
        confirmation = confirmation_from(self.alignment, (500, 500))
        check = verify_confirmation(self.baseline, confirmation)
        self.assertEqual(check["before_spread_ms"], 3.0)
        # The shared take reports each way's arrival relative to the earliest
        # way of that same take; here both ways arrive together.
        self.assertEqual(check["after_arrival_ms"], {"left_low": 0.0, "left_high": 0.0})
        self.assertEqual(check["tolerance_ms"], 0.25)

    def test_unseparable_shared_take_is_not_confirmed(self):
        confirmation = confirmation_from(self.alignment, (500, 500))
        tampered = deepcopy(confirmation)
        tampered["way_isolation_db"] = {"left_low": 3.0, "left_high": 30.0}
        check = verify_confirmation(self.baseline, tampered)
        self.assertFalse(check["confirmed"])
        self.assertIn("isolation", check["reasons"][0])
        self.assertEqual(check["isolation_margin_db"], 3.0)
        self.assertEqual(check["isolation_tolerance_db"], 10.0)
        self.assertEqual(check["way_isolation_db"], {"left_low": 3.0, "left_high": 30.0})

    def test_coincident_ways_need_no_isolation_margin(self):
        confirmation = confirmation_from(self.alignment, (500, 500))
        tampered = deepcopy(confirmation)
        tampered["way_isolation_db"] = {"left_low": None, "left_high": None}
        check = verify_confirmation(self.baseline, tampered)
        self.assertTrue(check["confirmed"])
        self.assertIsNone(check["isolation_margin_db"])

    def test_isolation_evidence_naming_other_ways_is_rejected(self):
        confirmation = confirmation_from(self.alignment, (500, 500))
        tampered = deepcopy(confirmation)
        tampered["way_isolation_db"] = {"left_low": 30.0, "right_high": 30.0}
        with self.assertRaisesRegex(ValueError, "isolation"):
            verify_confirmation(self.baseline, tampered)

    def test_predicted_zero_delays_cannot_hide_a_measured_residual(self):
        confirmation = confirmation_from(self.alignment, (500, 548))
        tampered = deepcopy(confirmation)
        tampered["added_delay_ms"] = {"left_low": 0, "left_high": 0}
        check = verify_confirmation(self.baseline, tampered)
        self.assertFalse(check["confirmed"])

    def test_rebased_confirmation_is_rejected(self):
        confirmation = confirmation_from(self.alignment, (500, 500))
        tampered = deepcopy(confirmation)
        tampered["start_revision"] += 1
        with self.assertRaisesRegex(ValueError, "revision"):
            verify_confirmation(self.baseline, tampered)

    def test_mismatched_ways_are_rejected(self):
        confirmation = confirmation_from(self.alignment, (500, 500))
        tampered = deepcopy(confirmation)
        tampered["arrival_ms"].pop("left_high")
        with self.assertRaisesRegex(ValueError, "ways"):
            verify_confirmation(self.baseline, tampered)

    def test_empty_confirmation_is_rejected(self):
        baseline = deepcopy(self.baseline)
        baseline["arrival_ms"] = {}
        confirmation = deepcopy(self.baseline)
        confirmation["arrival_ms"] = {}
        with self.assertRaisesRegex(ValueError, "ways"):
            verify_confirmation(baseline, confirmation)

    def test_missing_revision_fails_closed(self):
        confirmation = confirmation_from(self.alignment, (500, 500))
        baseline = deepcopy(self.baseline)
        del baseline["start_revision"]
        del confirmation["start_revision"]
        with self.assertRaisesRegex(ValueError, "revision"):
            verify_confirmation(baseline, confirmation)

    def test_none_fingerprint_fails_closed(self):
        confirmation = confirmation_from(self.alignment, (500, 500))
        baseline = deepcopy(self.baseline)
        baseline["processing_fingerprint"] = None
        confirmation["processing_fingerprint"] = None
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            verify_confirmation(baseline, confirmation)

    def test_nan_residual_is_rejected(self):
        confirmation = confirmation_from(self.alignment, (500, 500))
        tampered = deepcopy(confirmation)
        tampered["arrival_ms"]["left_low"] = float("nan")
        with self.assertRaisesRegex(ValueError, "finite"):
            verify_confirmation(self.baseline, tampered)

    def test_foreign_speaker_ways_are_rejected(self):
        confirmation = confirmation_from(self.alignment, (500, 500))
        tampered = deepcopy(confirmation)
        tampered["arrival_ms"]["right_high"] = tampered["arrival_ms"].pop("left_high")
        with self.assertRaisesRegex(ValueError, "ways"):
            verify_confirmation(self.baseline, tampered)

    def test_missing_arrival_evidence_is_rejected(self):
        confirmation = confirmation_from(self.alignment, (500, 500))
        tampered = deepcopy(confirmation)
        del tampered["arrival_ms"]
        with self.assertRaisesRegex(ValueError, "ways"):
            verify_confirmation(self.baseline, tampered)

    def test_malformed_confirmation_inputs_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "proposal"):
            verify_confirmation(None, self.baseline)
        with self.assertRaisesRegex(ValueError, "proposal"):
            verify_confirmation(self.baseline, "not-a-proposal")

    def test_numpy_float_residual_is_accepted(self):
        confirmation = confirmation_from(self.alignment, (500, 500))
        tampered = deepcopy(confirmation)
        tampered["arrival_ms"] = {
            role: np.float64(value) for role, value in tampered["arrival_ms"].items()}
        check = verify_confirmation(self.baseline, tampered)
        self.assertTrue(check["confirmed"])


def crossover_state_3way():
    state = default_output_state()
    routes = [f"{side}_{way}" for side in ("right", "left") for way in ("high", "mid", "low")]
    routes += ["sub1", "left_low"]
    state = set_mode_routing(set_crossover(state, "stereo-sub", True), "stereo-sub", "dev", routes)
    state = switch_mode(state, "stereo-sub")
    state["revision"] = 7
    processing = state["modes"]["stereo-sub"]["processing"]
    for side in ("left", "right"):
        processing[f"{side}_low"]["lowpass"] = {
            "family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 300,
        }
        processing[f"{side}_mid"]["highpass"] = {
            "family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 300,
        }
        processing[f"{side}_mid"]["lowpass"] = {
            "family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 2500,
        }
        processing[f"{side}_high"]["highpass"] = {
            "family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 2500,
        }
    return state


def alignment_and_live_3way(state):
    alignment = SpeakerAlignment(
        state, side="left", output_key="dev", channels=8, sample_rate_hz=RATE,
        fingerprint="frozen-plan", reference_id="interface:input-2:upstream",
        microphone_position_id="seat-1-fixed",
    )
    live = freeze_measurement_target(
        state, bank_id="global", output_key="dev", channels=8,
        sample_rate_hz=RATE, fingerprint="frozen-plan",
    )
    return alignment, live


def captures_for_3way(alignment, arrivals):
    """Three-way band-limited IRs with independently known arrival offsets."""
    delta = np.zeros(513)
    delta[256] = 1
    boundaries = [np.zeros(513), lowpass_kernel(300), lowpass_kernel(2500), delta]
    captures = []
    for index, request in enumerate(alignment.capture_requests()):
        reference = 700 + index * 131
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


class VerifyThreeWayTests(unittest.TestCase):
    def setUp(self):
        self.state = crossover_state_3way()
        self.alignment, self.live = alignment_and_live_3way(self.state)
        self.baseline = planned(self.alignment, self.live, (96, 144, 240),
                                captures_for_3way(self.alignment, (96, 144, 240)))

    def test_three_way_aligned_confirmation_confirms(self):
        confirmation = confirmation_from(self.alignment, (500, 500, 500))
        check = verify_confirmation(self.baseline, confirmation)
        self.assertTrue(check["confirmed"])
        self.assertEqual(len(check["pairs"]), 2)
        self.assertEqual([pair["roles"] for pair in check["pairs"]],
                         [["left_low", "left_mid"], ["left_mid", "left_high"]])

    def test_three_way_middle_way_residual_rejects(self):
        confirmation = confirmation_from(self.alignment, (500, 548, 500))
        check = verify_confirmation(self.baseline, confirmation)
        self.assertFalse(check["confirmed"])
        self.assertEqual(check["max_residual_ms"], 1.0)


class StageDoubles:
    """Thin hardware-boundary doubles: record calls, nothing else is faked."""

    def __init__(self, confirmation, *, failures=None):
        self.calls = []
        self.confirmation = confirmation
        self.failures = failures or {}

    async def stage(self, candidate):
        self.calls.append(("stage", deepcopy(candidate)))
        if "stage" in self.failures:
            raise self.failures["stage"]
        # A stage boundary must never mutate the caller's candidate.
        candidate["modes"]["stereo-sub"]["processing"]["left_low"]["alignment_ms"] = -999.0
        return {"staged": True}

    async def restore(self):
        self.calls.append(("restore",))
        if "restore" in self.failures:
            raise self.failures["restore"]

    async def confirm(self):
        self.calls.append(("confirm",))
        if "confirm" in self.failures:
            raise self.failures["confirm"]
        return deepcopy(self.confirmation)


class ApplyAndConfirmTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state = crossover_state()
        self.alignment, self.live = alignment_and_live(self.state)
        self.baseline = planned(self.alignment, self.live, (96, 240),
                                captures_for(self.alignment, (96, 240)))
        # The confirmation boundary returns the shared verification document.
        self.aligned = confirmation_from(self.alignment, (500, 500))
        self.offset = confirmation_from(self.alignment, (500, 548))

    async def run_trial(self, doubles, **overrides):
        options = dict(stage=doubles.stage, restore=doubles.restore,
                       confirm=doubles.confirm,
                       proposal=self.baseline, live_target=self.live)
        options.update(overrides)
        return await apply_and_confirm(**options)

    async def test_confirmed_trial_restores_and_detaches_candidate(self):
        doubles = StageDoubles(self.aligned)
        before = deepcopy(self.baseline["candidate_state"])
        result = await self.run_trial(doubles)
        self.assertTrue(result["confirmed"])
        self.assertTrue(result["restored"])
        self.assertEqual([call[0] for call in doubles.calls], ["stage", "confirm", "restore"])
        # The staged copy is detached: the double's marker never reaches us.
        self.assertEqual(self.baseline["candidate_state"], before)
        self.assertAlmostEqual(result["check"]["max_residual_ms"], 0.0, delta=0.05)

    async def test_unconfirmed_trial_restores_without_error(self):
        doubles = StageDoubles(self.offset)
        result = await self.run_trial(doubles)
        self.assertFalse(result["confirmed"])
        self.assertTrue(result["restored"])
        self.assertEqual([call[0] for call in doubles.calls], ["stage", "confirm", "restore"])

    async def test_stage_failure_runs_no_confirm_and_no_restore(self):
        doubles = StageDoubles(self.aligned, failures={"stage": RuntimeError("render failed")})
        with self.assertRaisesRegex(RuntimeError, "render failed"):
            await self.run_trial(doubles)
        self.assertEqual([call[0] for call in doubles.calls], ["stage"])

    async def test_confirm_failure_restores_and_reraises(self):
        doubles = StageDoubles(self.aligned, failures={"confirm": RuntimeError("mic unplugged")})
        with self.assertRaisesRegex(RuntimeError, "mic unplugged"):
            await self.run_trial(doubles)
        self.assertEqual([call[0] for call in doubles.calls], ["stage", "confirm", "restore"])

    async def test_malformed_confirmation_restores_and_reraises(self):
        broken = deepcopy(self.aligned)
        broken["arrival_ms"]["left_mid"] = 1.0
        doubles = StageDoubles(broken)
        with self.assertRaises(ValueError):
            await self.run_trial(doubles)
        self.assertEqual([call[0] for call in doubles.calls], ["stage", "confirm", "restore"])

    async def test_stale_live_target_fails_before_stage(self):
        doubles = StageDoubles(self.aligned)
        stale = deepcopy(self.live)
        stale["revision"] += 1
        with self.assertRaisesRegex(ValueError, "stale"):
            await self.run_trial(doubles, live_target=stale)
        self.assertEqual(doubles.calls, [])

    async def test_cancel_before_stage_starts_nothing(self):
        doubles = StageDoubles(self.aligned)
        with self.assertRaises(asyncio.CancelledError):
            await self.run_trial(doubles, cancel_requested=lambda: True)
        self.assertEqual(doubles.calls, [])

    async def test_cancel_during_confirm_restores_and_reraises(self):
        async def cancelling_confirm():
            raise asyncio.CancelledError("test cancel")

        doubles = StageDoubles(self.aligned)
        doubles.confirm = cancelling_confirm
        with self.assertRaises(asyncio.CancelledError):
            await self.run_trial(doubles)
        self.assertIn(("restore",), doubles.calls)

    async def test_outer_cancel_during_confirm_restores_and_reraises(self):
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        async def paused_confirm():
            loop.call_soon_threadsafe(entered.set)
            await asyncio.to_thread(release.wait, 10)
            return deepcopy(self.aligned)

        doubles = StageDoubles(self.aligned)
        doubles.confirm = paused_confirm
        worker = asyncio.create_task(self.run_trial(doubles))
        try:
            async with asyncio.timeout(10):
                await entered.wait()
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker
        finally:
            release.set()
            await asyncio.gather(worker, return_exceptions=True)
        self.assertIn(("restore",), doubles.calls)
        self.assertNotIn("outer-cancel-test-leak", str(doubles.calls))

    async def test_restore_failure_surfaces_loudly(self):
        doubles = StageDoubles(self.aligned, failures={"restore": RuntimeError("graph stuck")})
        with self.assertRaisesRegex(RuntimeError, "graph stuck"):
            await self.run_trial(doubles)
        self.assertEqual([call[0] for call in doubles.calls], ["stage", "confirm", "restore"])

    async def test_threshold_passthrough_accepts_known_residual(self):
        doubles = StageDoubles(self.offset)
        rejected = await self.run_trial(doubles)
        self.assertFalse(rejected["confirmed"])
        allowed = await self.run_trial(StageDoubles(self.offset), max_residual_ms=1.5)
        self.assertTrue(allowed["confirmed"])
        self.assertAlmostEqual(allowed["check"]["max_residual_ms"], 1.0, delta=0.1)

    async def test_missing_proposal_identity_fails_before_stage(self):
        doubles = StageDoubles(self.aligned)
        proposal = deepcopy(self.baseline)
        del proposal["start_revision"]
        with self.assertRaisesRegex(ValueError, "revision"):
            await self.run_trial(doubles, proposal=proposal)
        self.assertEqual(doubles.calls, [])

    async def test_malformed_proposal_envelope_is_rejected(self):
        doubles = StageDoubles(self.aligned)
        for proposal in (deepcopy(self.baseline) | {"candidate_state": None}, "not-a-proposal"):
            with self.subTest(proposal=type(proposal).__name__):
                with self.assertRaisesRegex(ValueError, "proposal"):
                    await self.run_trial(doubles, proposal=proposal)
        self.assertEqual(doubles.calls, [])

    async def test_cancel_during_error_restore_completes_restore(self):
        entered = asyncio.Event()
        released = threading.Event()
        restore_done = asyncio.Event()
        loop = asyncio.get_running_loop()
        calls = []

        async def slow_restore():
            calls.append("restore")
            loop.call_soon_threadsafe(entered.set)
            await asyncio.to_thread(released.wait, 10)
            calls.append("restore-done")
            loop.call_soon_threadsafe(restore_done.set)

        async def failing_confirm():
            calls.append("confirm")
            raise RuntimeError("mic unplugged")

        async def quick_stage(candidate):
            calls.append("stage")
            return {"staged": True}

        worker = asyncio.create_task(apply_and_confirm(
            stage=quick_stage, restore=slow_restore, confirm=failing_confirm,
            proposal=self.baseline, live_target=self.live))
        try:
            async with asyncio.timeout(10):
                await entered.wait()
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker
        finally:
            released.set()
        async with asyncio.timeout(10):
            await restore_done.wait()
        self.assertIn("restore-done", calls)


if __name__ == "__main__":
    unittest.main()
