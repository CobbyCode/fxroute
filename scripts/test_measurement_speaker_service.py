#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Speaker service owns one alignment job at a time and never leaks IRs."""

import asyncio
import os
import threading
from copy import deepcopy
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audio.output_state import default_output_state, set_mode_routing, switch_mode
from measurement.speaker_align import SpeakerAlignment
from measurement.speaker_service import SpeakerAlignService
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


def lowpass_kernel(cutoff):
    offsets = np.arange(-256, 257, dtype=float)
    sigma = np.sqrt(2 * np.log(2)) * RATE / (2 * np.pi * cutoff)
    kernel = np.exp(-0.5 * (offsets / sigma) ** 2)
    return kernel / np.sum(kernel)


def captures_for(alignment, arrivals):
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


class FakeSession:
    """Test double with the SpeakerAlignSession surface; records its calls."""

    def __init__(self, service_test, *, confirmation_arrivals=(500, 500)):
        self.test = service_test
        self.confirmation_arrivals = confirmation_arrivals
        self.calls = []
        self.committed = False
        self.staged_count = 0

    async def stage_candidate(self, candidate):
        self.calls.append("stage")
        self.staged_count += 1
        return {"fingerprint": "fake"}

    async def restore_start(self):
        self.calls.append("restore")

    async def confirm_and_commit(self, *, acquire, alignment, proposal,
                                 live_target, cancel_requested=None, **options):
        from measurement.speaker_apply import apply_and_confirm, verify_confirmation
        self.calls.append("confirm_and_commit")
        # The trial re-acquires through the same injected boundary the
        # service passed in, so a hanging second acquisition is visible.
        await self.stage_candidate(proposal["candidate_state"])
        captures = await acquire()
        confirmation = alignment.propose(captures, live_target=live_target)
        check = verify_confirmation(proposal, confirmation)
        await self.restore_start()
        if not check["confirmed"]:
            return {"confirmed": False, "check": check, "confirmation": confirmation,
                    "restored": True}
        if self.test.commit_fails:
            raise RuntimeError("commit failed")
        self.committed = True
        return {"confirmed": True, "check": check, "confirmation": confirmation,
                "committed": {"revision": 8}, "restored": False}


class ServiceFixture:
    def setUp(self):
        self.state = crossover_state()
        self.first_arrivals = (96, 240)
        self.second_arrivals = (500, 500)
        self.acquire_calls = []
        self.acquire_queue = []
        self.commit_fails = False
        self.session = FakeSession(self)
        self.live_revision_bump = 0
        self.freeze_calls = 0
        self.stale_from_freeze_call = 0

    def describe(self, state):
        return {"output_key": "dev", "channels": 6, "sample_rate_hz": RATE,
                "fingerprint": "frozen-plan"}

    def freeze_live(self, state, *, output_key, channels, sample_rate_hz, fingerprint):
        self.freeze_calls += 1
        live = freeze_measurement_target(
            state, bank_id="global", output_key=output_key, channels=channels,
            sample_rate_hz=sample_rate_hz, fingerprint=fingerprint)
        bump = self.live_revision_bump
        if self.stale_from_freeze_call and self.freeze_calls >= self.stale_from_freeze_call:
            bump += 1
        live["revision"] += bump
        return live

    async def acquire(self, alignment, **kwargs):
        self.acquire_calls.append(deepcopy(kwargs))
        if self.acquire_queue:
            behaviour = self.acquire_queue.pop(0)
            if isinstance(behaviour, BaseException):
                raise behaviour
            return behaviour
        arrivals = self.first_arrivals if len(self.acquire_calls) == 1 else self.second_arrivals
        return {"captures": captures_for(alignment, arrivals),
                "provenance": {"job_ids": [f"job-{len(self.acquire_calls)}"]}}

    def service(self, **overrides):
        options = dict(get_state=lambda: deepcopy(self.state), describe=self.describe,
                       acquire=self.acquire, create_session=lambda start_state, **ctx: self.session,
                       freeze_live=self.freeze_live)
        options.update(overrides)
        return SpeakerAlignService(**options)

    def start(self, service=None, **kwargs):
        service = service if service is not None else self.service()
        params = dict(side="left", input_id="mic", reference_input_channel="2",
                      reference_id="interface:input-2:upstream",
                      microphone_position_id="seat-1-fixed")
        params.update(kwargs)
        return service, service.start(**params)

    async def wait_terminal(self, service, job_id):
        async with asyncio.timeout(10):
            while service.status(job_id)["status"] not in {
                    "committed", "trial-done", "unconfirmed", "failed", "cancelled"}:
                await asyncio.sleep(0)
        return service.status(job_id)


class StartValidationTests(ServiceFixture, unittest.TestCase):
    def test_unknown_side_fails_before_job_exists(self):
        service = self.service()
        with self.assertRaisesRegex(ValueError, "side"):
            service.start(side="center", input_id="mic", reference_input_channel="2",
                          reference_id="r", microphone_position_id="m")
        self.assertEqual(service.jobs(), [])

    def test_missing_reference_channel_fails_before_job_exists(self):
        service = self.service()
        with self.assertRaisesRegex(ValueError, "reference"):
            service.start(side="left", input_id="mic", reference_input_channel="",
                          reference_id="r", microphone_position_id="m")
        self.assertEqual(service.jobs(), [])

    def test_non_crossover_state_fails_before_job_exists(self):
        self.state["active_mode"] = "stereo"
        service = self.service()
        with self.assertRaises(ValueError):
            service.start(side="left", input_id="mic", reference_input_channel="2",
                          reference_id="r", microphone_position_id="m")
        self.assertEqual(service.jobs(), [])

    def test_unknown_job_status_raises_key_error(self):
        service = self.service()
        with self.assertRaises(KeyError):
            service.status("no-such-job")
        with self.assertRaises(KeyError):
            service.cancel("no-such-job")


class CommitFlowTests(ServiceFixture, unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_run_commits_and_reports_summary(self):
        service, job_id = self.start()
        job = await self.wait_terminal(service, job_id)
        self.assertEqual(job["status"], "committed")
        self.assertTrue(job["result"]["confirmed"])
        self.assertEqual(job["result"]["committed_revision"], 8)
        self.assertFalse(job["result"]["dry_run"])
        summary = job["result"]["proposal"]
        self.assertAlmostEqual(summary["added_delay_ms"]["left_low"], 3.0, delta=0.3)
        self.assertAlmostEqual(summary["added_delay_ms"]["left_high"], 0.0, delta=0.3)
        self.assertEqual(job["side"], "left")
        # Progress is observable while running; terminal keeps the last note.
        self.assertIn("commit", job["message"].lower())

    async def test_result_is_json_safe_and_holds_no_waveforms(self):
        import json
        service, job_id = self.start()
        job = await self.wait_terminal(service, job_id)
        text = json.dumps(job, allow_nan=False)
        self.assertNotIn("impulse_response", text)
        self.assertIn("added_delay_ms", text)

    async def test_unconfirmed_run_never_commits(self):
        self.second_arrivals = (500, 548)
        service, job_id = self.start()
        job = await self.wait_terminal(service, job_id)
        self.assertEqual(job["status"], "unconfirmed")
        self.assertFalse(job["result"]["confirmed"])
        self.assertIsNone(job["result"]["committed_revision"])
        self.assertFalse(self.session.committed)

    async def test_dry_run_confirms_without_committing(self):
        service, job_id = self.start(dry_run=True)
        job = await self.wait_terminal(service, job_id)
        self.assertEqual(job["status"], "trial-done")
        self.assertTrue(job["result"]["confirmed"])
        self.assertTrue(job["result"]["dry_run"])
        self.assertIsNone(job["result"]["committed_revision"])
        self.assertFalse(self.session.committed)
        self.assertIn("restore", self.session.calls)

    async def test_acquire_failure_fails_the_job_loudly(self):
        self.acquire_queue = [RuntimeError("mic unplugged")]
        service, job_id = self.start()
        job = await self.wait_terminal(service, job_id)
        self.assertEqual(job["status"], "failed")
        self.assertIn("mic unplugged", job["error"])
        self.assertFalse(self.session.committed)

    async def test_stale_live_target_fails_before_any_job_exists(self):
        self.live_revision_bump = 1
        service = self.service()
        with self.assertRaisesRegex(ValueError, "stale"):
            service.start(side="left", input_id="mic", reference_input_channel="2",
                          reference_id="r", microphone_position_id="m")
        self.assertEqual(service.jobs(), [])
        self.assertEqual(self.acquire_calls, [])

    async def test_worker_side_stale_live_target_fails_the_job(self):
        # start() sees a fresh target (freeze call 1); the worker's
        # re-frozen target (call 2) disagrees: the job fails loudly.
        self.stale_from_freeze_call = 2
        service, job_id = self.start()
        job = await self.wait_terminal(service, job_id)
        self.assertEqual(job["status"], "failed")
        self.assertIn("stale", job["error"].lower())
        self.assertFalse(self.session.committed)

    async def test_second_start_while_running_is_rejected(self):
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        original = self.acquire

        async def paused(alignment, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            await asyncio.to_thread(release.wait, 10)
            return await original(alignment, **kwargs)

        service = self.service(acquire=paused)
        _, first = self.start(service)
        try:
            async with asyncio.timeout(10):
                await entered.wait()
            with self.assertRaisesRegex(RuntimeError, "already running"):
                service.start(side="right", input_id="mic", reference_input_channel="2",
                              reference_id="r", microphone_position_id="m")
        finally:
            release.set()
        job = await self.wait_terminal(service, first)
        self.assertEqual(job["status"], "committed")

    async def test_cancel_during_acquire_cancels_the_job(self):
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        original = self.acquire

        async def paused(alignment, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            await asyncio.to_thread(release.wait, 10)
            return await original(alignment, **kwargs)

        service = self.service(acquire=paused)
        _, job_id = self.start(service)
        try:
            async with asyncio.timeout(10):
                await entered.wait()
            service.cancel(job_id)
            job = await self.wait_terminal(service, job_id)
        finally:
            release.set()
        self.assertEqual(job["status"], "cancelled")
        self.assertFalse(self.session.committed)

    async def test_cancel_after_terminal_is_harmless(self):
        service, job_id = self.start()
        await self.wait_terminal(service, job_id)
        service.cancel(job_id)
        self.assertEqual(service.status(job_id)["status"], "committed")


if __name__ == "__main__":
    unittest.main()
