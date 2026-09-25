#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Serial Speaker Align acquisition must bind frozen requests to common input."""

import asyncio
import os
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import speaker_take_test_support as takes
from audio.output_state import default_output_state, set_mode_routing, switch_mode, set_crossover
from measurement.speaker_acquisition import acquire_speaker_captures, verify_speaker_alignment
from measurement.speaker_align import SpeakerAlignment
from measurement.store import MeasurementStore
from measurement.target import REFERENCE_TAP_INGRESS, freeze_measurement_target

RATE = 48000
REFERENCE_ID = "interface:input-2:upstream"
POSITION_ID = "seat-1-fixed"


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
    return state, len(routes)


def alignment_for(state):
    return SpeakerAlignment(
        state, side="left", output_key="dev", channels=6, sample_rate_hz=RATE,
        fingerprint="frozen-plan", reference_id=REFERENCE_ID,
        microphone_position_id=POSITION_ID,
    )


def planning_for(alignment, arrivals):
    """A shared planning take whose ways carry the given sample offsets.

    The capture fake below renders one acoustic event per take (the sweep at
    one delay), so a shared take in this harness cannot separate two ways: the
    way geometry a proposal plans from is supplied here instead. The delay
    planning itself is covered end to end in ``test_speaker_align``.
    """
    roles = [request["role"] for request in alignment.capture_requests()]
    return takes.planning_document(
        alignment, {role: arrivals[index] * 1000.0 / RATE for index, role in enumerate(roles)})


def live_global_target(state):
    return freeze_measurement_target(
        state, bank_id="global", output_key="dev", channels=6,
        sample_rate_hz=RATE, fingerprint="frozen-plan",
    )


class SpeakerAcquisitionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="speaker-acquire-")
        self.addCleanup(directory.cleanup)
        home = Path(directory.name)
        environment = patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(home / "config"), "XDG_STATE_HOME": str(home / "state"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.state = crossover_state()[0]
        self.alignment = alignment_for(self.state)
        self.requests = self.alignment.capture_requests()
        self.store = MeasurementStore(home=home)
        self.store.measurement_target_provider = lambda bank, rate: freeze_measurement_target(
            self.state, bank_id=bank, output_key="dev", channels=6,
            sample_rate_hz=rate, fingerprint="frozen-plan",
        )
        self.node_name = "capture_1"
        self.store._discover_capture_inputs = lambda: [{
            "id": "mic", "label": "Mic", "node_name": self.node_name, "node_serial": "serial-9",
            "channels": 2, "sample_rate": RATE, "available": True,
        }]
        self.store._measurement_inputs_with_sample_rate = lambda inputs: inputs
        self.store._try_raise_mic_for_low_capture = lambda *args, **kwargs: False
        self.store._capture_policy._try_raise_mic = lambda *args, **kwargs: False
        self.store._capture_policy._should_keep_electrical_reference = lambda *args: False
        self.store._capture_policy._retry_sleep = lambda _: None
        self.store._capture_policy._cancel_aware_sleep = lambda *args: None
        self.captures_started = 0
        self.masks = []
        self.after_attempt = lambda analysis: None
        self.is_electrical = False
        # Which 1-based input channel carries the electrical reference in each
        # fake take.  The default mirrors the legacy 2-channel harness layout.
        self.er_carrier_for_capture = lambda _index: 2
        # Only hardware-facing calls are replaced: real host/analyzer, retry
        # policy, store, persistence and task/mask lifecycle remain in the path.
        routing = self.store._routing
        routing._resolve_playback_target = lambda **kw: {"target_name": "fxroute_dsp_sink", "target_label": "DSP"}
        routing._resolve_host_reference_capture = lambda **kw: {
            "source_node_name": "fxroute_dsp_sink.monitor", "channel_label": "reference",
        }
        routing._build_measurement_playback_route = lambda *args, **kw: {"route": "test"}
        routing._build_measurement_play_command = lambda **kw: ["pw-play"]
        routing._build_pre_sweep_state_snapshot = lambda **kw: {}
        routing._cleanup_fxroute_links = lambda **kw: None
        routing._cleanup_measurement_playback_links = lambda **kw: None
        routing._link_capture_channels_to_record_stream = lambda **kw: {"links": ["input_1", "input_2"]}
        routing._link_host_reference_capture = lambda **kw: {"links": ["monitor", "mic"]}
        routing._lookup_pipewire_audio_node = lambda node: {"id": 1, "name": node}
        self.store._pw_record_supports_option = lambda _: True
        self.store._snapshot_fxroute_21_helper_processes = lambda _: {}
        self.store._monitor_capture_input_level = lambda *args: None
        self.store._start_job_process = self.spawn

        async def apply_mask(mask):
            self.masks.append(mask)

        async def clear_mask(mask):
            pass

        self.store.output_mask_apply = apply_mask
        self.store.output_mask_clear = clear_mask
        sleeping = patch("measurement.host_capture.time.sleep", lambda _: None)
        sleeping.start()
        self.addCleanup(sleeping.stop)
        diagnostics = patch("measurement.host_capture._detailed_measurement_diagnostics_enabled", return_value=False)
        diagnostics.start()
        self.addCleanup(diagnostics.stop)
        host_execute = self.store._host_capture_runner.execute

        def execute(**kwargs):
            self.is_electrical = kwargs["electrical_reference_channel_index"] is not None
            result = host_execute(**kwargs)
            self.after_attempt(result[0])
            return result

        self.store._host_capture_runner.execute = execute
        self.base_execute = execute

    @staticmethod
    def record_channel_count(command):
        args = list(command)
        return int(args[args.index("--channels") + 1]) if "--channels" in args else 2

    def spawn(self, job_id, command):
        if command[0] == "pw-record":
            self.captures_started += 1
            rate, playback = self.store._load_wav_array(self.store.playbacks_dir / f"{job_id}.wav")
            reference = playback[:, 0]
            # Every serial way sees a different acoustic delay; the adapter
            # must keep each way's own arrival instead of averaging them.
            delay = 240 * self.captures_started
            mic = np.zeros_like(reference)
            mic[delay:] = reference[:-delay] * 0.5
            if self.is_electrical:
                # One take over every configured loopback candidate; only the
                # carrier channel receives the reference signal.
                channel_count = self.record_channel_count(command)
                carrier_input = int(self.er_carrier_for_capture(self.captures_started))
                capture = np.zeros((reference.size, channel_count))
                capture[:, 0] = mic
                if 1 <= carrier_input <= channel_count:
                    capture[:, carrier_input - 1] = reference
            else:
                capture = np.column_stack([reference, mic])
            self.store._write_wav(Path(command[-1]), capture, rate)
        return SimpleNamespace(returncode=0, communicate=lambda **kw: ("", ""), poll=lambda: 0)

    def use_scarlett_input(self):
        """Report an 18-channel capture input so 8+ loopback channels exist."""
        self.store._discover_capture_inputs = lambda: [{
            "id": "mic", "label": "Mic", "node_name": self.node_name, "node_serial": "serial-9",
            "channels": 18, "sample_rate": RATE, "available": True,
        }]

    async def acquire(self, **overrides):
        options = dict(input_id="mic", reference_input_channel="2",
                       reference_id=REFERENCE_ID, microphone_position_id=POSITION_ID,
                       sweep_profile={"sweep_seconds": 0.68, "lead_in_seconds": 0.34,
                                      "tail_seconds": 0.18})
        options.update(overrides)
        return await acquire_speaker_captures(self.store, self.alignment, **options)

    async def verify(self, **overrides):
        options = dict(input_id="mic", mic_input_channel="1", reference_input_channel="2",
                       reference_id=REFERENCE_ID, microphone_position_id=POSITION_ID,
                       sweep_profile={"sweep_seconds": 0.68, "lead_in_seconds": 0.34,
                                      "tail_seconds": 0.18})
        options.update(overrides)
        return await verify_speaker_alignment(self.store, self.alignment, **options)

    async def test_serial_ways_propose_start_relative_delays(self):
        result = await self.acquire()
        captures = result["captures"]
        self.assertEqual([capture["role"] for capture in captures], ["left_low", "left_high"])
        # One shared planning take first, then one take per way.
        self.assertEqual(self.captures_started, 3)
        self.assertEqual(len(self.store._jobs), 3)
        for capture, request in zip(captures, self.requests):
            self.assertEqual(capture["measurement_target"], request["measurement_target"])
            self.assertEqual(capture["reference_id"], REFERENCE_ID)
            self.assertEqual(capture["microphone_position_id"], POSITION_ID)
            self.assertEqual(capture["reference_tap"], REFERENCE_TAP_INGRESS)
            self.assertEqual(capture["time_reference"], "deconvolved-sweep-origin")
            self.assertGreater(capture["impulse_response"].size, RATE)
        # The planning take plays the whole side, then each way is isolated by
        # its own frozen canonical mask, low to high.
        planning_mask = self.alignment.planning_request()["output_mask"]
        self.assertEqual(self.masks, [planning_mask] + [r["output_mask"] for r in self.requests])
        self.assertNotEqual(self.masks[0], self.masks[1])
        self.assertNotEqual(self.masks[1], self.masks[2])
        provenance = result["provenance"]
        self.assertEqual(provenance["microphone_node"], "capture_1")
        self.assertEqual(provenance["microphone_serial"], "serial-9")
        self.assertEqual(provenance["electrical_reference_channel"], 2)
        # The electrical reference rides the same input as the microphone, so
        # the observed reference node is that shared input, not a monitor.
        self.assertEqual(provenance["reference_node"], "capture_1")
        self.assertEqual(provenance["sample_rate_hz"], RATE)
        self.assertEqual(len(provenance["job_ids"]), 3)
        # The shared planning take comes back as the store's normal
        # measurement of that take: the Before view with its IR preview.
        planning_job = self.store.get_job(provenance["job_ids"][0])
        measurement = result["measurement"]
        self.assertEqual(measurement, planning_job["result"]["measurement"])
        self.assertEqual(measurement["channel"], "left")
        self.assertTrue(measurement["traces"][0]["points"])
        self.assertTrue(measurement["analysis"]["impulse_response"]["preview"]["points"])
        proposal = self.alignment.propose(
            captures, planning=planning_for(self.alignment, (96, 336)),
            live_target=live_global_target(self.state))
        # 240-sample acoustic offset between the ways at 48 kHz.
        self.assertAlmostEqual(proposal["added_delay_ms"]["left_low"], 5.0, delta=0.3)
        self.assertAlmostEqual(proposal["added_delay_ms"]["left_high"], 0.0, delta=0.3)
        candidate = proposal["candidate_state"]
        self.assertAlmostEqual(
            candidate["modes"]["stereo-sub"]["processing"]["left_low"]["alignment_ms"], 5.0, delta=0.3)
        self.assertEqual(proposal["reference_role"], "left_high")

    async def test_verification_runs_one_shared_take_for_the_whole_side(self):
        self.use_scarlett_input()
        self.er_carrier_for_capture = lambda _index: 7
        progress = []
        result = await self.verify(
            reference_input_channel="7", reference_input_channel_left="7",
            reference_input_channel_right="8",
            on_progress=lambda role, index, count: progress.append((role, index, count)))
        # One sweep for the side, not one per way, and it plays exactly the
        # side's ways: every other routed role stays muted.
        self.assertEqual(self.captures_started, 1)
        self.assertEqual(len(self.store._jobs), 1)
        request = self.alignment.verification_request()
        self.assertEqual(self.masks, [request["output_mask"]])
        self.assertEqual(progress, [("left_ways", 1, 1)])
        confirmation = result["confirmation"]
        self.assertEqual(set(confirmation["arrival_ms"]), {"left_low", "left_high"})
        self.assertEqual(confirmation["start_revision"], 7)
        self.assertEqual(confirmation["processing_fingerprint"], "frozen-plan")
        self.assertIn("way_isolation_db", confirmation)
        self.assertIn("way_levels_db", confirmation)
        provenance = result["provenance"]
        self.assertEqual(provenance["reference_id"], REFERENCE_ID)
        self.assertEqual(provenance["microphone_position_id"], POSITION_ID)
        self.assertEqual(provenance["electrical_reference_channel"], 7)
        self.assertEqual(provenance["reference_node"], "capture_1")
        self.assertEqual(len(provenance["job_ids"]), 1)
        # The verification take is the After view: the store's normal
        # measurement of that one take.
        verification_job = self.store.get_job(provenance["job_ids"][0])
        self.assertEqual(result["measurement"], verification_job["result"]["measurement"])
        self.assertTrue(result["measurement"]["analysis"]["impulse_response"]["preview"]["points"])

    async def test_verification_target_is_the_narrowed_side_plan(self):
        self.use_scarlett_input()
        self.er_carrier_for_capture = lambda _index: 7
        seen = []
        original = self.store.start_measurement

        async def spy(**kwargs):
            seen.append(kwargs)
            return await original(**kwargs)

        self.store.start_measurement = spy
        try:
            await self.verify(reference_input_channel="7",
                              reference_input_channel_left="7",
                              reference_input_channel_right="8")
        finally:
            self.store.start_measurement = original
        self.assertEqual(len(seen), 1)
        request = self.alignment.verification_request()
        self.assertEqual(seen[0]["frozen_target"], request["measurement_target"])
        self.assertEqual(seen[0]["channel"], "left")
        self.assertEqual(seen[0]["mic_input_channel"], "1")
        self.assertEqual(seen[0]["reference_candidate_channels"], ["7", "8"])
        self.assertIsInstance(seen[0]["capture_evidence"], object)

    async def test_verification_reference_must_be_electrical(self):
        self.use_scarlett_input()
        # No loopback carries the reference: the store's host-timing fallback
        # must fail this take before any confirmation.
        self.er_carrier_for_capture = lambda _index: 0
        with self.assertRaisesRegex(RuntimeError, "electrical reference"):
            await self.verify(reference_input_channel="7",
                              reference_input_channel_left="7",
                              reference_input_channel_right="8")
        self.assertEqual(self.captures_started, 2)
        self.assertEqual(len(self.store._jobs), 1)

    async def test_verification_identities_must_match_the_frozen_requests(self):
        for options in ({"reference_id": "other:input"},
                        {"microphone_position_id": "other-seat"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                await self.verify(**options)
        self.assertEqual(self.captures_started, 0)
        self.assertEqual(self.store._jobs, {})

    async def test_host_reference_is_captured_in_the_same_stream_as_input_one(self):
        result = await self.acquire(reference_input_channel="")
        self.assertEqual(result["provenance"]["reference_node"], REFERENCE_TAP_INGRESS)
        self.assertIsNone(result["provenance"]["electrical_reference_channel"])
        proposal = self.alignment.propose(
            result["captures"], planning=planning_for(self.alignment, (96, 336)),
            live_target=live_global_target(self.state))
        self.assertAlmostEqual(proposal["added_delay_ms"]["left_low"], 5.0, delta=0.3)

    async def test_staged_candidate_context_reaches_every_capture_preflight(self):
        from dsp.processing_plan import compile_processing_plan
        # The candidate differs from the persisted head only by the trial delay.
        candidate = deepcopy(self.state)
        candidate["modes"]["stereo-sub"]["processing"]["left_low"]["alignment_ms"] = 5
        from dsp.native_config import layout_from_plan
        plan = compile_processing_plan(candidate, output_key="dev", channels=6, sample_rate_hz=RATE,
                                       preset_loader=lambda name: {"chain": []})
        expected = {"expected_native_layout": layout_from_plan(plan, resolve_ir=lambda name: None),
                    "expected_native_output_mode": "stereo-sub",
                    "expected_plan_fingerprint": "candidate-plan"}
        contexts = []
        self.store._routing._build_pre_sweep_state_snapshot = lambda **kw: contexts.append(kw["playback_route"]) or {}
        self.store._routing._build_measurement_playback_route = lambda *args, **kw: {"route": "test", **kw}
        await self.acquire(expected_native_context=expected)
        self.assertEqual(len(contexts), 3)
        for context in contexts:
            self.assertEqual(context["expected_plan_fingerprint"], "candidate-plan")
            self.assertEqual(context["expected_native_layout"], expected["expected_native_layout"])

    async def test_invalid_identities_or_missing_reference_fail_before_capture(self):
        for options in ({"reference_id": ""}, {"reference_id": "  "},
                         {"microphone_position_id": ""}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                await self.acquire(**options)
        self.assertEqual(self.captures_started, 0)
        self.assertEqual(self.store._jobs, {})

    async def test_missing_target_provider_fails_before_capture(self):
        self.store.measurement_target_provider = None
        with self.assertRaisesRegex(ValueError, "target provider"):
            await self.acquire()
        self.assertEqual(self.captures_started, 0)

    async def test_stale_revision_between_plan_and_capture_fails_before_capture(self):
        self.state["revision"] = 8
        with self.assertRaisesRegex(ValueError, "stale"):
            await self.acquire()
        self.assertEqual(self.captures_started, 0)
        # Registered, then cancelled before its worker ran: fail fast spends
        # no sweep and leaks no worker task.
        self.assertEqual(len(self.store._jobs), 1)
        job_id = next(iter(self.store._jobs))
        self.assertEqual(self.store.get_job(job_id)["status"], "cancelled")
        self.assertTrue(self.store._job_tasks[job_id].done())

    async def test_changed_microphone_input_between_ways_fails_before_second_capture(self):
        checks = {"n": 0}

        def cancel_requested():
            checks["n"] += 1
            if checks["n"] == 2:
                self.node_name = "capture_2"
                self.store.invalidate_capture_inputs_cache()
            return False

        with self.assertRaisesRegex(RuntimeError, "microphone"):
            await self.acquire(cancel_requested=cancel_requested)
        self.assertEqual(self.captures_started, 1)
        # The second job was registered, then cancelled before its worker ran:
        # no second sweep, no leaked worker task.
        self.assertEqual(len(self.store._jobs), 2)
        by_status: dict[str, list[str]] = {}
        for job_id in self.store._jobs:
            by_status.setdefault(self.store.get_job(job_id)["status"], []).append(job_id)
        self.assertEqual(len(by_status.get("completed", [])), 1)
        self.assertEqual(len(by_status.get("cancelled", [])), 1)
        self.assertTrue(self.store._job_tasks[by_status["cancelled"][0]].done())

    async def test_fallback_reference_fails_fast_without_laundering(self):
        def reject_reference(analysis):
            if analysis["reference_path"]["capture_mode"] == "electrical-input":
                analysis["reference_path"]["clipped"] = True

        self.after_attempt = reject_reference
        with self.assertRaisesRegex(RuntimeError, "electrical reference"):
            await self.acquire()
        # ER attempt plus its host-timing fallback ran; the second way never started.
        self.assertEqual(self.captures_started, 2)
        self.assertEqual(len(self.store._jobs), 1)

    async def test_cancellation_between_ways_starts_no_further_capture(self):
        checks = {"n": 0}

        def cancel_requested():
            checks["n"] += 1
            return checks["n"] > 1

        with self.assertRaises(asyncio.CancelledError):
            await self.acquire(cancel_requested=cancel_requested)
        self.assertEqual(self.captures_started, 1)
        self.assertEqual(len(self.store._jobs), 1)

    async def test_in_flight_cancellation_propagates_without_partial_result(self):
        self.after_attempt = lambda analysis: self.store._cancelled_jobs.update(self.store._jobs)
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            await self.acquire()
        self.assertEqual(self.captures_started, 1)

    async def test_failed_way_reports_its_job_error(self):
        original = self.store._persistence._build_measurement_from_analysis
        calls = {"n": 0}

        def fail(*args, **kwargs):
            # The shared planning take runs first; the failing take is a way's.
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("result construction failed")
            return original(*args, **kwargs)

        self.store._persistence._build_measurement_from_analysis = fail
        with self.assertRaisesRegex(RuntimeError, "left_low"):
            await self.acquire()
        self.assertEqual(self.captures_started, 2)

    async def test_provenance_carries_session_binding(self):
        result = await self.acquire()
        self.assertEqual(result["provenance"]["reference_id"], REFERENCE_ID)
        self.assertEqual(result["provenance"]["microphone_position_id"], POSITION_ID)

    async def test_per_way_loopback_carrier_is_admitted_from_one_take(self):
        # Input 7 carries the low way, input 8 the high way.  Both are recorded
        # in the same take per way and the admitted channel follows the capture
        # evidence instead of the speaker side, without a second sweep.
        self.use_scarlett_input()
        self.er_carrier_for_capture = lambda index: 7 if index <= 2 else 8
        result = await self.acquire(reference_input_channel="7",
                                    reference_input_channel_left="7",
                                    reference_input_channel_right="8")
        self.assertEqual(self.captures_started, 3)
        captures = result["captures"]
        self.assertEqual([capture["role"] for capture in captures], ["left_low", "left_high"])
        self.assertEqual(
            [capture["analysis"]["reference_path"]["electrical_reference_input_channel"]
             for capture in captures], [7, 8])
        for capture in captures:
            reference = capture["analysis"]["reference_path"]
            self.assertTrue(reference["electrical_reference_used"])
            self.assertEqual(
                [item["input_channel"] for item in reference["electrical_reference_candidates"]],
                [7, 8])
            self.assertNotIn("electrical_reference_fallback", reference)
        # The configured candidate set stays identical between ways; the admitted
        # channel is per-way evidence and never an attestation.  Each real channel
        # is kept for its own role.
        provenance = result["provenance"]
        self.assertEqual(tuple(provenance["electrical_reference_candidates"]), (7, 8))
        self.assertEqual(provenance["electrical_reference_channels_by_role"],
                         {"left_low": 7, "left_high": 8})
        proposal = self.alignment.propose(
            captures, planning=planning_for(self.alignment, (96, 336)),
            live_target=live_global_target(self.state))
        self.assertAlmostEqual(proposal["added_delay_ms"]["left_low"], 5.0, delta=0.3)

    async def test_silent_first_candidate_uses_the_second_in_the_same_take(self):
        self.use_scarlett_input()
        self.er_carrier_for_capture = lambda index: 8
        result = await self.acquire(reference_input_channel="7",
                                    reference_input_channel_left="7",
                                    reference_input_channel_right="8")
        self.assertEqual(self.captures_started, 3)
        self.assertEqual(
            [capture["analysis"]["reference_path"]["electrical_reference_input_channel"]
             for capture in result["captures"]], [8, 8])
        self.assertEqual(result["provenance"]["electrical_reference_channels_by_role"],
                         {"left_low": 8, "left_high": 8})

    async def test_take_without_any_usable_candidate_keeps_the_host_fallback(self):
        self.use_scarlett_input()
        self.er_carrier_for_capture = lambda index: 0
        with self.assertRaisesRegex(RuntimeError, "electrical reference"):
            await self.acquire(reference_input_channel="7",
                               reference_input_channel_left="7",
                               reference_input_channel_right="8")
        # The rejected reference take plus its host-timing fallback, first way only.
        self.assertEqual(self.captures_started, 2)
        self.assertEqual(len(self.store._jobs), 1)

    async def test_incomplete_capture_input_fails_closed_and_drained(self):
        original = self.store.start_measurement

        async def strip_input(**kwargs):
            job = await original(**kwargs)
            job.pop("input", None)
            job.pop("input_channels", None)
            return job

        self.store.start_measurement = strip_input
        with self.assertRaisesRegex(ValueError, "incomplete"):
            await self.acquire()
        self.assertEqual(self.captures_started, 0)
        self.assertEqual(len(self.store._jobs), 1)
        job_id = next(iter(self.store._jobs))
        self.assertEqual(self.store.get_job(job_id)["status"], "cancelled")
        self.assertTrue(self.store._job_tasks[job_id].done())

    async def test_tolerated_reference_is_accepted_without_upgrading_its_mark(self):
        # The store keeps a marginal end anchor in the active 2.2 path and marks
        # the reference "dsp-end-anchor-tolerated".  Acquisition must follow that
        # verdict instead of demanding stability == "stable", and must not
        # rewrite the mark it received.
        self.after_attempt = lambda analysis: analysis["clock"].update(end_score=0.8)
        self.store._capture_policy._should_keep_electrical_reference = lambda *args: True
        result = await self.acquire()
        self.assertEqual(self.captures_started, 3)
        self.assertEqual(len(self.store._jobs), 3)
        for capture in result["captures"]:
            reference = capture["analysis"]["reference_path"]
            self.assertTrue(reference["electrical_reference_used"])
            self.assertEqual(reference["timing_status"], "electrical-reference")
            self.assertEqual(reference["stability"], "dsp-end-anchor-tolerated")
            self.assertNotIn("electrical_reference_fallback", reference)

    async def test_acoustic_only_capture_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "electrical reference"):
            await self.acquire(reference_input_channel="1")
        self.assertEqual(self.captures_started, 1)
        self.assertEqual(len(self.store._jobs), 1)

    async def test_changed_observed_reference_between_ways_fails(self):
        calls = {"n": 0}
        base_execute = self.store._host_capture_runner.execute

        def mutate(**kwargs):
            calls["n"] += 1
            analysis, capture, playback = base_execute(**kwargs)
            if calls["n"] == 2:
                capture = dict(capture)
                capture["reference_node"] = "other-monitor"
            return analysis, capture, playback

        self.store._host_capture_runner.execute = mutate
        with self.assertRaisesRegex(RuntimeError, "reference_node"):
            await self.acquire()
        self.assertEqual(self.captures_started, 2)
        self.assertEqual(len(self.store._jobs), 2)

    async def test_outer_cancellation_drains_in_flight_way(self):
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def pause_after_analysis(analysis):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(10):
                raise RuntimeError("test worker was not released")

        self.after_attempt = pause_after_analysis
        worker = asyncio.create_task(self.acquire())
        try:
            async with asyncio.timeout(10):
                await entered.wait()
            worker.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await worker
        finally:
            release.set()
            await asyncio.gather(worker, return_exceptions=True)
        self.assertEqual(self.captures_started, 1)
        self.assertEqual(len(self.store._jobs), 1)
        job_id = next(iter(self.store._jobs))
        self.assertTrue(self.store._job_tasks[job_id].done())

    def activate_calibration(self, name, high_offset_db):
        text = f"20 0.0\n1000 0.0\n20000 {high_offset_db}\n".encode()
        return self.store._file_store.resolve_calibration_meta(
            calibration_filename=name, calibration_bytes=text)

    async def test_side_take_carries_the_applied_microphone_calibration(self):
        meta = self.activate_calibration("mic.txt", 3.0)
        seen = []
        planning = self.alignment.planning
        self.alignment.planning = lambda take: (seen.append(take), planning(take))[1]
        result = await self.acquire()
        curve = seen[0]["calibration_curve"]
        self.assertEqual(curve["frequencies_hz"], [20.0, 1000.0, 20000.0])
        self.assertEqual(curve["offsets_db"], [0.0, 0.0, 3.0])
        self.assertEqual(result["provenance"]["microphone_calibration"], meta["path"])

    async def test_side_take_without_calibration_carries_none(self):
        seen = []
        planning = self.alignment.planning
        self.alignment.planning = lambda take: (seen.append(take), planning(take))[1]
        result = await self.acquire()
        self.assertIsNone(seen[0]["calibration_curve"])
        self.assertEqual(result["provenance"]["microphone_calibration"], "")

    async def test_changed_microphone_calibration_between_takes_fails(self):
        self.activate_calibration("first.txt", 3.0)
        calls = {"n": 0}

        def switch(analysis):
            calls["n"] += 1
            if calls["n"] == 1:
                self.activate_calibration("second.txt", 1.0)

        self.after_attempt = switch
        with self.assertRaisesRegex(RuntimeError, "microphone_calibration"):
            await self.acquire()
        self.assertEqual(self.captures_started, 2)

    def boost_on_capture(self, index):
        calls = {"n": 0}
        base_execute = self.store._host_capture_runner.execute

        def boosted(**kwargs):
            calls["n"] += 1
            analysis, capture, playback = base_execute(**kwargs)
            if calls["n"] == index:
                capture = dict(capture)
                capture["mic_auto_boosted"] = True
            return analysis, capture, playback

        self.store._host_capture_runner.execute = boosted

    async def test_raised_microphone_gain_after_the_first_way_fails(self):
        # Planning take, first way, then the second way raises the gain: the
        # first way was captured quieter, so the way levels cannot compare.
        self.boost_on_capture(3)
        with self.assertRaisesRegex(RuntimeError, "microphone input volume was raised"):
            await self.acquire()

    async def test_raised_microphone_gain_before_the_way_levels_is_accepted(self):
        # A raise during the planning take or the first way reaches every
        # later way take as well: all way levels share one input gain.
        for index in (1, 2):
            with self.subTest(capture=index):
                self.captures_started = 0
                self.store._host_capture_runner.execute = self.base_execute
                self.boost_on_capture(index)
                result = await self.acquire()
                self.assertEqual(len(result["captures"]), 2)

    async def test_missing_observed_field_fails_closed(self):
        base_execute = self.store._host_capture_runner.execute

        def drop(**kwargs):
            analysis, capture, playback = base_execute(**kwargs)
            capture = dict(capture)
            capture.pop("reference_node", None)
            return analysis, capture, playback

        self.store._host_capture_runner.execute = drop
        with self.assertRaisesRegex(RuntimeError, "missing reference_node"):
            await self.acquire()
        self.assertEqual(self.captures_started, 1)
        self.assertEqual(len(self.store._jobs), 1)

    async def test_mismatched_session_identities_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "reference_id"):
            await self.acquire(reference_id="other:input")
        with self.assertRaisesRegex(ValueError, "microphone_position_id"):
            await self.acquire(microphone_position_id="other-seat")
        self.assertEqual(self.captures_started, 0)

    async def test_per_side_reference_channels_reach_start_measurement(self):
        # Scarlett loopbacks 7 (left) / 8 (right) must arrive untouched at
        # the capture registration of every way.
        self.use_scarlett_input()
        self.er_carrier_for_capture = lambda _index: 7
        seen = []
        original = self.store.start_measurement

        async def spy(**kwargs):
            seen.append({key: kwargs.get(key) for key in (
                "reference_input_channel", "reference_input_channel_left",
                "reference_input_channel_right")})
            return await original(**kwargs)

        self.store.start_measurement = spy
        try:
            await self.acquire(reference_input_channel="7",
                               reference_input_channel_left="7",
                               reference_input_channel_right="8")
        finally:
            self.store.start_measurement = original
        self.assertEqual(len(seen), 3)
        for entry in seen:
            self.assertEqual(entry, {"reference_input_channel": "7",
                                     "reference_input_channel_left": "7",
                                     "reference_input_channel_right": "8"})


if __name__ == "__main__":
    unittest.main()
