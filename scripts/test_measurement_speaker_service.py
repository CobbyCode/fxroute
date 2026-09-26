#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Speaker service owns one alignment job at a time and never leaks full-rate IRs."""

import asyncio
import os
import threading
from copy import deepcopy
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import speaker_take_test_support as takes
from audio.output_state import default_output_state, set_mode_routing, switch_mode, set_crossover
from measurement.speaker_align import SpeakerAlignment
from measurement.speaker_service import (
    SpeakerAlignBusyError,
    SpeakerAlignService,
    SpeakerAlignStaleError,
    _summarize_proposal,
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


def lowpass_kernel(cutoff):
    offsets = np.arange(-256, 257, dtype=float)
    sigma = np.sqrt(2 * np.log(2)) * RATE / (2 * np.pi * cutoff)
    kernel = np.exp(-0.5 * (offsets / sigma) ** 2)
    return kernel / np.sum(kernel)


def planning_for(alignment, arrivals):
    """One shared planning take at the given per-way sample offsets."""
    roles = [request["role"] for request in alignment.capture_requests()]
    return takes.planning_document(
        alignment, {role: arrivals[index] * 1000.0 / RATE for index, role in enumerate(roles)})


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


def take_measurement(name):
    """A store-shaped normal measurement of one shared take."""
    return {
        "id": f"sweep-{name}", "name": f"Current sweep {name}", "channel": "left",
        "measurement_kind": "sweep-response-v3",
        "traces": [{"kind": "sweep-response", "label": f"Current sweep {name} · trusted",
                    "role": "trusted", "points": [[20.0, -3.0], [20000.0, -6.0]]}],
        "review_traces": [{"kind": "sweep-response-review", "label": f"Current sweep {name} · raw",
                           "role": "raw-review", "points": [[20.0, -4.0], [20000.0, -7.0]]}],
        "analysis": {"impulse_response": {
            "arrival_ms": 2.5,
            "preview": {"schema": "fxroute.ir-preview.v1", "window_ms": [-2.0, 30.0],
                        "points": [[round(-2.0 + index * 0.064, 3), 0.0] for index in range(500)]},
        }},
    }


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

    def measurement_context(self):
        return {}

    async def confirm_and_commit(self, *, confirm, proposal,
                                 live_target, cancel_requested=None, **options):
        from measurement.speaker_apply import verify_confirmation
        self.calls.append("confirm_and_commit")
        # The trial measures through the same injected boundary the service
        # passed in, so a hanging verification take is visible.
        await self.stage_candidate(proposal["candidate_state"])
        confirmation = await confirm()
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
        self.confirm_calls = []
        self.confirm_queue = []
        self.verification_provenance = {"job_ids": ["verify-1"]}
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
        return {"captures": captures_for(alignment, self.first_arrivals),
                "planning": planning_for(alignment, self.first_arrivals),
                "provenance": {"job_ids": [f"job-{len(self.acquire_calls)}"]},
                "measurement": take_measurement("planning")}

    def confirmation_document(self, alignment):
        """A proposal-shaped verification document; the DSP is tested elsewhere."""
        request = alignment.verification_request()
        return {
            "start_revision": request["measurement_target"]["revision"],
            "processing_fingerprint": request["measurement_target"]["processing_fingerprint"],
            "arrival_ms": {role: float(self.second_arrivals[index]) * 1000.0 / RATE
                           for index, role in enumerate(request["roles"])},
            "way_levels_db": {role: 0.0 for role in request["roles"]},
        }

    async def confirm(self, alignment, **kwargs):
        self.confirm_calls.append(deepcopy(kwargs))
        if self.confirm_queue:
            behaviour = self.confirm_queue.pop(0)
            if isinstance(behaviour, BaseException):
                raise behaviour
            return behaviour
        return {"confirmation": self.confirmation_document(alignment),
                "provenance": dict(self.verification_provenance),
                "measurement": take_measurement("verification")}

    def service(self, **overrides):
        options = dict(get_state=lambda: deepcopy(self.state), describe=self.describe,
                       acquire=self.acquire, confirm=self.confirm,
                       create_session=lambda start_state, **ctx: self.session,
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

    def test_non_global_selection_fails_before_job_exists(self):
        self.state["modes"]["stereo-sub"]["selected_bank"] = "left_low"
        service = self.service()
        with self.assertRaisesRegex(ValueError, "Global"):
            service.start(side="left", input_id="mic", reference_input_channel="2",
                          reference_id="r", microphone_position_id="m")
        self.assertEqual(service.jobs(), [])

    def test_missing_electrical_reference_fails_before_any_sweep(self):
        # Verification only admits the electrical reference: without one the
        # run must stop before its planning sweep, not after verification.
        service = self.service()
        for channels in ({"reference_input_channel": ""},
                         {"reference_input_channel": None},
                         {"reference_input_channel": " ", "reference_input_channel_left": "",
                          "reference_input_channel_right": None}):
            with self.assertRaisesRegex(ValueError, "electrical reference"):
                service.start(side="left", input_id="mic", reference_id="r",
                              microphone_position_id="m", **channels)
        self.assertEqual(service.jobs(), [])
        self.assertEqual(self.acquire_calls, [])
        self.assertFalse(service.active)

    def test_reference_on_the_microphone_channel_fails_before_any_sweep(self):
        # The store would drop a reference that is the microphone channel and
        # the take would fail only after its planning sweep; judge the
        # resolved channel indexes, not the raw fields.
        service = self.service()
        for side, channels in (
                ("left", {"reference_input_channel": "1"}),
                ("left", {"mic_input_channel": "", "reference_input_channel": "01"}),
                ("left", {"mic_input_channel": 3, "reference_input_channel": " 3 "}),
                ("left", {"reference_input_channel": "",
                          "reference_input_channel_left": "1", "reference_input_channel_right": "8"}),
                ("right", {"reference_input_channel": "7",
                           "reference_input_channel_left": "7", "reference_input_channel_right": "1"})):
            with self.subTest(side=side, channels=channels):
                with self.assertRaisesRegex(ValueError, "the microphone input"):
                    service.start(side=side, input_id="mic", reference_id="r",
                                  microphone_position_id="m", **channels)
        self.assertEqual(service.jobs(), [])
        self.assertEqual(self.acquire_calls, [])
        self.assertFalse(service.active)

    def test_non_crossover_state_fails_before_job_exists(self):
        self.state["active_mode"] = "stereo"
        service = self.service()
        with self.assertRaises(ValueError):
            service.start(side="left", input_id="mic", reference_input_channel="2",
                          reference_id="r", microphone_position_id="m")
        self.assertEqual(service.jobs(), [])

    def test_blank_input_id_fails_before_job_exists(self):
        service = self.service()
        with self.assertRaisesRegex(ValueError, "input"):
            service.start(side="left", input_id="  ", reference_input_channel="2",
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
        self.assertEqual(job["result"]["check"]["before_spread_ms"], 3.0)
        self.assertEqual(job["result"]["check"]["after_arrival_ms"],
                         {"left_low": 500 / 48, "left_high": 500 / 48})
        # Progress is observable while running; terminal keeps the last note.
        self.assertIn("commit", job["message"].lower())

    async def test_result_is_json_safe_and_holds_no_waveforms(self):
        import json
        service, job_id = self.start()
        job = await self.wait_terminal(service, job_id)
        json.dumps(job, allow_nan=False)
        # IR data travels only as the takes' bounded diagnostic previews; the
        # alignment evidence itself stays plain floats.
        result = dict(job["result"])
        measurements = result.pop("measurements")
        self.assertNotIn("impulse_response", json.dumps(result))
        self.assertIn("added_delay_ms", json.dumps(result))
        for measurement in measurements.values():
            impulse = measurement["analysis"]["impulse_response"]
            self.assertLessEqual(len(impulse["preview"]["points"]), 500)
            self.assertNotIn("samples", impulse)

    async def test_result_carries_before_and_after_as_normal_measurements(self):
        service, job_id = self.start()
        job = await self.wait_terminal(service, job_id)
        before = job["result"]["measurements"]["before"]
        after = job["result"]["measurements"]["after"]
        self.assertEqual(before["id"], "sweep-planning")
        self.assertEqual(after["id"], "sweep-verification")
        self.assertEqual(before["name"], "Speaker Align Left · Before (planning)")
        self.assertEqual(after["name"], "Speaker Align Left · After (verification)")
        self.assertEqual(before["speaker_align_take"], {"side": "left", "take": "before"})
        self.assertEqual(after["speaker_align_take"], {"side": "left", "take": "after"})
        self.assertEqual(before["traces"][0]["label"], "Speaker Align Left · Before (planning) · trusted")
        self.assertEqual(after["review_traces"][0]["label"],
                         "Speaker Align Left · After (verification) · raw/full-band review")
        self.assertEqual(before["measurement_kind"], "sweep-response-v3")

    async def test_takes_share_the_reference_way_time_base(self):
        # The planning take puts the high way 3 ms after the low way; the
        # verification take renders the low way with its 3 ms delay applied.
        async def confirm(alignment, **kwargs):
            result = await self.confirm(alignment, **kwargs)
            result["confirmation"] = takes.confirmation_document(
                alignment, {"left_low": 5.0, "left_high": 5.0})
            return result

        service, job_id = self.start(self.service(confirm=confirm))
        job = await self.wait_terminal(service, job_id)
        self.assertEqual(job["status"], "committed")
        result = job["result"]
        reference = result["proposal"]["reference_role"]
        self.assertEqual(reference, "left_high")
        timelines = {take: result["measurements"][take]["analysis"]["speaker_align_timeline"]
                     for take in ("before", "after")}
        for timeline in timelines.values():
            self.assertEqual(timeline["schema"], "fxroute.speaker-align-timeline.v1")
            self.assertEqual(timeline["time_origin"], "reference-way-arrival")
            self.assertEqual(timeline["reference_role"], reference)
            self.assertEqual(timeline["arrival_ms"][reference], 0.0)
            self.assertEqual(list(timeline["ways"]), ["left_low", "left_high"])
            for role, points in timeline["ways"].items():
                peak_ms = max(points, key=lambda point: abs(point[1]))[0]
                self.assertAlmostEqual(peak_ms, timeline["arrival_ms"][role], places=3)
        # Before: every way at minus its planned delay. After: the residual.
        for role, delay in result["proposal"]["added_delay_ms"].items():
            self.assertAlmostEqual(timelines["before"]["arrival_ms"][role], -delay, places=5)
        after_arrivals = result["check"]["after_arrival_ms"]
        for role, arrival in after_arrivals.items():
            self.assertAlmostEqual(timelines["after"]["arrival_ms"][role],
                                   arrival - after_arrivals[reference], places=5)
        after_spread = (max(timelines["after"]["arrival_ms"].values())
                        - min(timelines["after"]["arrival_ms"].values()))
        self.assertAlmostEqual(after_spread, result["check"]["max_residual_ms"], places=5)
        self.assertAlmostEqual(timelines["before"]["arrival_ms"]["left_low"], -3.0, delta=0.05)
        self.assertLess(abs(timelines["after"]["arrival_ms"]["left_low"]), 0.05)

    async def test_take_without_band_evidence_has_no_timeline(self):
        service, job_id = self.start()
        job = await self.wait_terminal(service, job_id)
        measurements = job["result"]["measurements"]
        self.assertIn("speaker_align_timeline", measurements["before"]["analysis"])
        self.assertNotIn("speaker_align_timeline", measurements["after"]["analysis"])

    async def test_unconfirmed_and_trial_runs_keep_both_takes(self):
        self.second_arrivals = (500, 548)
        service, job_id = self.start()
        job = await self.wait_terminal(service, job_id)
        self.assertEqual(job["status"], "unconfirmed")
        self.assertEqual(set(job["result"]["measurements"]), {"before", "after"})
        self.second_arrivals = (500, 500)
        service, job_id = self.start(dry_run=True)
        job = await self.wait_terminal(service, job_id)
        self.assertEqual(job["status"], "trial-done")
        self.assertEqual(set(job["result"]["measurements"]), {"before", "after"})

    async def test_unsafe_take_measurement_is_dropped_without_failing(self):
        planning = take_measurement("planning")
        planning["analysis"]["impulse_response"]["arrival_ms"] = float("nan")

        async def acquire(alignment, **kwargs):
            result = await self.acquire(alignment, **kwargs)
            result["measurement"] = planning
            return result

        service, job_id = self.start(self.service(acquire=acquire))
        with self.assertLogs("measurement.speaker_service", level="WARNING"):
            job = await self.wait_terminal(service, job_id)
        self.assertEqual(job["status"], "committed")
        self.assertEqual(set(job["result"]["measurements"]), {"after"})

    async def test_take_without_measurement_is_omitted(self):
        async def acquire(alignment, **kwargs):
            result = await self.acquire(alignment, **kwargs)
            result.pop("measurement")
            return result

        service, job_id = self.start(self.service(acquire=acquire))
        job = await self.wait_terminal(service, job_id)
        self.assertEqual(job["status"], "committed")
        self.assertEqual(set(job["result"]["measurements"]), {"after"})

    async def test_unconfirmed_run_never_commits(self):
        self.second_arrivals = (500, 548)
        service, job_id = self.start()
        job = await self.wait_terminal(service, job_id)
        self.assertEqual(job["status"], "unconfirmed")
        self.assertFalse(job["result"]["confirmed"])
        self.assertIsNone(job["result"]["committed_revision"])
        self.assertFalse(self.session.committed)

    async def test_per_side_reference_channels_reach_acquire(self):
        # Scarlett loopbacks 7 (left) / 8 (right): the right run must record
        # the right loopback, never the shared/left one.
        service, job_id = self.start(side="right", reference_input_channel="7",
                                     reference_input_channel_left="7",
                                     reference_input_channel_right="8",
                                     dry_run=True)
        params = service.status(job_id)["params"]
        self.assertEqual(params["reference_input_channel"], "7")
        self.assertEqual(params["reference_input_channel_left"], "7")
        self.assertEqual(params["reference_input_channel_right"], "8")
        job = await self.wait_terminal(service, job_id)
        self.assertEqual(job["status"], "trial-done")
        acquired = self.acquire_calls[0]
        self.assertEqual(acquired["reference_input_channel"], "7")
        self.assertEqual(acquired["reference_input_channel_left"], "7")
        self.assertEqual(acquired["reference_input_channel_right"], "8")
        verified = self.confirm_calls[0]
        self.assertEqual(verified["reference_input_channel"], "7")
        self.assertEqual(verified["reference_input_channel_left"], "7")
        self.assertEqual(verified["reference_input_channel_right"], "8")

    async def test_per_side_electrical_reference_alone_is_accepted(self):
        service, job_id = self.start(reference_input_channel="",
                                     reference_input_channel_left="7", dry_run=True)
        job = await self.wait_terminal(service, job_id)
        self.assertEqual(job["status"], "trial-done", job)

    async def test_one_sided_reference_serves_the_other_side(self):
        # A reference configured for one side only is recorded for either
        # side, like a shared one; the take's evidence decides whether it
        # carries the sweep.
        service, job_id = self.start(side="right", reference_input_channel="",
                                     reference_input_channel_left="7", dry_run=True)
        job = await self.wait_terminal(service, job_id)
        self.assertEqual(job["status"], "trial-done", job)
        self.assertEqual(self.acquire_calls[0]["reference_input_channel_left"], "7")

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

    async def test_planning_gate_failure_keeps_the_planning_evidence(self):
        # A take whose ways cannot be told apart fails the plan; the job keeps
        # the arrivals and margins it failed on, never a waveform.
        def acquire_unseparable(alignment, **kwargs):
            planning = planning_for(alignment, self.first_arrivals)
            planning["way_isolation_db"] = {"left_low": 4.5, "left_high": None}
            provenance = {"job_ids": ["job-1"],
                          "planning": {key: value for key, value in planning.items() if key != "bands"}}
            return {"captures": captures_for(alignment, self.first_arrivals),
                    "planning": planning, "provenance": provenance}

        async def acquire(alignment, **kwargs):
            return acquire_unseparable(alignment, **kwargs)

        service, job_id = self.start(self.service(acquire=acquire))
        job = await self.wait_terminal(service, job_id)
        self.assertEqual(job["status"], "failed")
        self.assertIn("cannot separate the ways", job["error"])
        evidence = job["result"]["planning"]
        self.assertEqual(set(evidence), {"arrival_ms", "way_isolation_db", "way_levels_db"})
        self.assertEqual(evidence["way_isolation_db"], {"left_low": 4.5, "left_high": None})
        self.assertAlmostEqual(evidence["arrival_ms"]["left_high"] - evidence["arrival_ms"]["left_low"],
                               144 * 1000.0 / RATE, delta=0.05)
        self.assertFalse(self.session.committed)

    async def test_failure_before_planning_carries_no_result(self):
        self.acquire_queue = [RuntimeError("mic unplugged")]
        service, job_id = self.start()
        job = await self.wait_terminal(service, job_id)
        self.assertIsNone(job["result"])

    async def test_stale_live_target_fails_before_any_job_exists(self):
        self.live_revision_bump = 1
        service = self.service()
        with self.assertRaises(SpeakerAlignStaleError):
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

    def good_result(self):
        alignment = SpeakerAlignment(
            self.state, side="left", output_key="dev", channels=6, sample_rate_hz=RATE,
            fingerprint="frozen-plan", reference_id="interface:input-2:upstream",
            microphone_position_id="seat-1-fixed")
        return {"captures": captures_for(alignment, (96, 240)),
                "planning": planning_for(alignment, (96, 240)),
                "provenance": {"job_ids": ["job-1"]}}

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
            with self.assertRaises(SpeakerAlignBusyError):
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
        await service.wait_for(job_id)
        service.cancel(job_id)
        self.assertEqual(service.status(job_id)["status"], "committed")

    async def test_cancel_survives_concurrent_retention_eviction(self):
        # Cross-thread cancel contract: a retention eviction can land between
        # cancel()'s two guarded reads (first read validated the running job,
        # task.cancel() delivers, the worker finishes and _finish evicts the
        # record while the cancelling thread is between the two blocks). The
        # second read must return the first read's record, not raise KeyError.
        # The eviction is injected into task.cancel(), which runs exactly
        # between the two guard blocks, so the interleaving is deterministic.
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
            jobs_during = service._jobs

            class EvictingTask:
                def cancel(self_inner):
                    service._jobs = {
                        key: value for key, value in jobs_during.items()
                        if key != job_id}

            real_task = service._tasks[job_id]
            service._tasks[job_id] = EvictingTask()
            record = service.cancel(job_id)
            self.assertEqual(record["id"], job_id)
            self.assertEqual(record["status"], "cancelling")
            # Restore the record and the real task so the paused worker can
            # finish its cancellation path like a raced run would.
            service._jobs[job_id] = jobs_during[job_id]
            service._tasks[job_id] = real_task
        finally:
            release.set()
        job = await self.wait_terminal(service, job_id)
        self.assertEqual(job["status"], "cancelled")
        self.assertFalse(self.session.committed)

    async def test_post_terminal_start_succeeds_with_new_id(self):
        service, first = self.start()
        await service.wait_for(first)
        _, second = self.start(service)
        self.assertNotEqual(first, second)
        job = await service.wait_for(second)
        self.assertEqual(job["status"], "committed")

    async def test_terminal_jobs_are_retained_bounded(self):
        service = self.service()
        service.max_retained_jobs = 2
        ids = []
        for _ in range(3):
            _, job_id = self.start(service)
            ids.append(job_id)
            await service.wait_for(job_id)
        remaining = [job["id"] for job in service.jobs()]
        self.assertEqual(remaining, ids[1:])
        with self.assertRaises(KeyError):
            service.status(ids[0])

    async def test_status_copies_are_detached_and_internal_free(self):
        service, job_id = self.start()
        await service.wait_for(job_id)
        first = service.status(job_id)
        self.assertNotIn("internal", first)
        self.assertNotIn("cancel_requested", first)
        first["result"]["proposal"]["added_delay_ms"]["left_low"] = 999.0
        second = service.status(job_id)
        self.assertNotEqual(
            second["result"]["proposal"]["added_delay_ms"]["left_low"], 999.0)

    async def test_dry_run_unconfirmed_reports_trial_without_commit(self):
        self.second_arrivals = (500, 548)
        service, job_id = self.start(dry_run=True)
        job = await service.wait_for(job_id)
        self.assertEqual(job["status"], "trial-done")
        self.assertFalse(job["result"]["confirmed"])
        self.assertIsNone(job["result"]["committed_revision"])
        self.assertFalse(self.session.committed)

    async def test_dry_run_never_touches_commit(self):
        service, job_id = self.start(dry_run=True)
        job = await service.wait_for(job_id)
        self.assertEqual(job["status"], "trial-done")
        self.assertNotIn("confirm_and_commit", self.session.calls)

    async def test_commit_failure_fails_the_job(self):
        self.commit_fails = True
        service, job_id = self.start()
        job = await service.wait_for(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertIn("commit failed", job["error"])

    async def test_verification_failure_fails_the_job(self):
        self.confirm_queue = [RuntimeError("verification sweep lost")]
        service, job_id = self.start()
        job = await service.wait_for(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertIn("verification sweep lost", job["error"])
        self.assertFalse(self.session.committed)

    async def test_verification_input_chain_change_fails_the_job(self):
        self.verification_provenance = {"job_ids": ["verify-1"], "mic_channel": 2}
        service, job_id = self.start()
        job = await service.wait_for(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertIn("input chain changed", job["error"])
        self.assertFalse(self.session.committed)

    async def test_worker_rejects_rebased_state_without_sweep(self):
        calls = {"n": 0}
        base_get_state = lambda: deepcopy(self.state)

        def counting_get_state():
            calls["n"] += 1
            state = base_get_state()
            if calls["n"] > 1:
                state["revision"] += 1
            return state

        service = self.service(get_state=counting_get_state)
        _, job_id = self.start(service)
        job = await service.wait_for(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertIn("stale", job["error"].lower())
        self.assertEqual(self.acquire_calls, [])

    async def test_cancel_probe_reaches_propose(self):
        seen = {}
        import measurement.speaker_service as service_module
        real_alignment = service_module.SpeakerAlignment
        fixture = self

        class RecordingAlignment(real_alignment):
            def propose(self, captures, **kwargs):
                seen.update(kwargs)
                return super().propose(captures, **kwargs)

        service_module.SpeakerAlignment = RecordingAlignment
        try:
            service, job_id = self.start()
            await service.wait_for(job_id)
        finally:
            service_module.SpeakerAlignment = real_alignment
        self.assertIn("cancel_requested", seen)
        self.assertTrue(callable(seen["cancel_requested"]))

    def test_summarizer_rejects_malformed_proposal_loudly(self):
        with self.assertRaises(KeyError):
            _summarize_proposal({})


if __name__ == "__main__":
    unittest.main()
