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

from audio.output_state import default_output_state, set_mode_routing, switch_mode
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
    state = set_mode_routing(state, "crossover", "dev", routes)
    state = switch_mode(state, "crossover")
    state["revision"] = 7
    processing = state["modes"]["crossover"]["processing"]
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
        self.baseline = self.alignment.propose(
            captures_for(self.alignment, (96, 240)), live_target=self.live)

    def test_aligned_confirmation_confirms(self):
        confirmation = self.alignment.propose(
            captures_for(self.alignment, (500, 500)), live_target=self.live)
        check = verify_confirmation(self.baseline, confirmation)
        self.assertTrue(check["confirmed"])
        self.assertAlmostEqual(check["max_residual_ms"], 0.0, delta=0.05)
        self.assertEqual(len(check["pairs"]), 1)

    def test_residual_offset_rejects(self):
        confirmation = self.alignment.propose(
            captures_for(self.alignment, (500, 548)), live_target=self.live)
        check = verify_confirmation(self.baseline, confirmation)
        self.assertFalse(check["confirmed"])
        self.assertGreater(check["max_residual_ms"], MAX_CONFIRMED_RESIDUAL_MS)
        self.assertIn("residual", check["reasons"][0])

    def test_regressed_sum_rejects(self):
        confirmation = self.alignment.propose(
            captures_for(self.alignment, (500, 500)), live_target=self.live)
        tampered = deepcopy(confirmation)
        tampered["overlap_checks"][0]["after_sum_db"] -= 5.0
        check = verify_confirmation(self.baseline, tampered)
        self.assertFalse(check["confirmed"])
        self.assertIn("regression", check["reasons"][0])

    def test_collapsed_sum_rejects(self):
        confirmation = self.alignment.propose(
            captures_for(self.alignment, (500, 500)), live_target=self.live)
        tampered = deepcopy(confirmation)
        tampered["overlap_checks"][0]["after_sum_db"] = -10.0
        check = verify_confirmation(self.baseline, tampered)
        self.assertFalse(check["confirmed"])

    def test_rebased_confirmation_is_rejected(self):
        confirmation = self.alignment.propose(
            captures_for(self.alignment, (500, 500)), live_target=self.live)
        tampered = deepcopy(confirmation)
        tampered["start_revision"] += 1
        with self.assertRaisesRegex(ValueError, "revision"):
            verify_confirmation(self.baseline, tampered)

    def test_mismatched_pairs_are_rejected(self):
        confirmation = self.alignment.propose(
            captures_for(self.alignment, (500, 500)), live_target=self.live)
        tampered = deepcopy(confirmation)
        tampered["overlap_checks"] = []
        with self.assertRaisesRegex(ValueError, "pairs"):
            verify_confirmation(self.baseline, tampered)

    def test_empty_confirmation_is_rejected(self):
        baseline = deepcopy(self.baseline)
        baseline["overlap_checks"] = []
        confirmation = deepcopy(self.baseline)
        confirmation["overlap_checks"] = []
        with self.assertRaisesRegex(ValueError, "overlap checks"):
            verify_confirmation(baseline, confirmation)


class StageDoubles:
    """Thin hardware-boundary doubles: record calls, nothing else is faked."""

    def __init__(self, captures, *, failures=None):
        self.calls = []
        self.captures = captures
        self.failures = failures or {}

    async def stage(self, candidate):
        self.calls.append(("stage", deepcopy(candidate)))
        if "stage" in self.failures:
            raise self.failures["stage"]
        # A stage boundary must never mutate the caller's candidate.
        candidate["modes"]["crossover"]["processing"]["left_low"]["alignment_ms"] = -999.0
        return {"staged": True}

    async def restore(self):
        self.calls.append(("restore",))
        if "restore" in self.failures:
            raise self.failures["restore"]

    async def acquire(self):
        self.calls.append(("acquire",))
        if "acquire" in self.failures:
            raise self.failures["acquire"]
        return deepcopy(self.captures)


class ApplyAndConfirmTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state = crossover_state()
        self.alignment, self.live = alignment_and_live(self.state)
        self.baseline = self.alignment.propose(
            captures_for(self.alignment, (96, 240)), live_target=self.live)
        self.aligned = captures_for(self.alignment, (500, 500))

    async def run_trial(self, doubles, **overrides):
        options = dict(stage=doubles.stage, restore=doubles.restore,
                       acquire=doubles.acquire, alignment=self.alignment,
                       proposal=self.baseline, live_target=self.live)
        options.update(overrides)
        return await apply_and_confirm(**options)

    async def test_confirmed_trial_restores_and_detaches_candidate(self):
        doubles = StageDoubles(self.aligned)
        before = deepcopy(self.baseline["candidate_state"])
        result = await self.run_trial(doubles)
        self.assertTrue(result["confirmed"])
        self.assertTrue(result["restored"])
        self.assertEqual([call[0] for call in doubles.calls], ["stage", "acquire", "restore"])
        # The staged copy is detached: the double's marker never reaches us.
        self.assertEqual(self.baseline["candidate_state"], before)
        self.assertAlmostEqual(result["check"]["max_residual_ms"], 0.0, delta=0.05)

    async def test_unconfirmed_trial_restores_without_error(self):
        doubles = StageDoubles(captures_for(self.alignment, (500, 548)))
        result = await self.run_trial(doubles)
        self.assertFalse(result["confirmed"])
        self.assertTrue(result["restored"])
        self.assertEqual([call[0] for call in doubles.calls], ["stage", "acquire", "restore"])

    async def test_stage_failure_runs_no_acquire_and_no_restore(self):
        doubles = StageDoubles(self.aligned, failures={"stage": RuntimeError("render failed")})
        with self.assertRaisesRegex(RuntimeError, "render failed"):
            await self.run_trial(doubles)
        self.assertEqual([call[0] for call in doubles.calls], ["stage"])

    async def test_acquire_failure_restores_and_reraises(self):
        doubles = StageDoubles(self.aligned, failures={"acquire": RuntimeError("mic unplugged")})
        with self.assertRaisesRegex(RuntimeError, "mic unplugged"):
            await self.run_trial(doubles)
        self.assertEqual([call[0] for call in doubles.calls], ["stage", "acquire", "restore"])

    async def test_propose_failure_restores_and_reraises(self):
        broken = deepcopy(self.aligned)
        del broken[0]["role"]
        doubles = StageDoubles(broken)
        with self.assertRaises(ValueError):
            await self.run_trial(doubles)
        self.assertEqual([call[0] for call in doubles.calls], ["stage", "acquire", "restore"])

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

    async def test_cancel_during_acquire_restores_and_reraises(self):
        async def cancelling_acquire():
            raise asyncio.CancelledError("test cancel")

        doubles = StageDoubles(self.aligned)
        doubles.acquire = cancelling_acquire
        with self.assertRaises(asyncio.CancelledError):
            await self.run_trial(doubles)
        self.assertIn(("restore",), doubles.calls)

    async def test_outer_cancel_during_acquire_restores_and_reraises(self):
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        async def paused_acquire():
            loop.call_soon_threadsafe(entered.set)
            await asyncio.to_thread(release.wait, 10)
            return deepcopy(self.aligned)

        doubles = StageDoubles(self.aligned)
        doubles.acquire = paused_acquire
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
        self.assertEqual([call[0] for call in doubles.calls], ["stage", "acquire", "restore"])


if __name__ == "__main__":
    unittest.main()
