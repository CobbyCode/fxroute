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

from audio.output_state import default_output_state, set_mode_routing, switch_mode, set_crossover
from measurement.speaker_acquisition import acquire_speaker_captures
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
            capture = np.column_stack([mic, reference] if self.is_electrical else [reference, mic])
            self.store._write_wav(Path(command[-1]), capture, rate)
        return SimpleNamespace(returncode=0, communicate=lambda **kw: ("", ""), poll=lambda: 0)

    async def acquire(self, **overrides):
        options = dict(input_id="mic", reference_input_channel="2",
                       reference_id=REFERENCE_ID, microphone_position_id=POSITION_ID,
                       sweep_profile={"sweep_seconds": 0.68, "lead_in_seconds": 0.34,
                                      "tail_seconds": 0.18})
        options.update(overrides)
        return await acquire_speaker_captures(self.store, self.alignment, **options)

    async def test_serial_ways_propose_start_relative_delays(self):
        result = await self.acquire()
        captures = result["captures"]
        self.assertEqual([capture["role"] for capture in captures], ["left_low", "left_high"])
        self.assertEqual(self.captures_started, 2)
        self.assertEqual(len(self.store._jobs), 2)
        for capture, request in zip(captures, self.requests):
            self.assertEqual(capture["measurement_target"], request["measurement_target"])
            self.assertEqual(capture["reference_id"], REFERENCE_ID)
            self.assertEqual(capture["microphone_position_id"], POSITION_ID)
            self.assertEqual(capture["reference_tap"], REFERENCE_TAP_INGRESS)
            self.assertEqual(capture["time_reference"], "deconvolved-sweep-origin")
            self.assertGreater(capture["impulse_response"].size, RATE)
        # Each way is isolated by its own frozen canonical mask, low to high.
        self.assertEqual(self.masks, [request["output_mask"] for request in self.requests])
        self.assertNotEqual(self.masks[0], self.masks[1])
        provenance = result["provenance"]
        self.assertEqual(provenance["microphone_node"], "capture_1")
        self.assertEqual(provenance["microphone_serial"], "serial-9")
        self.assertEqual(provenance["electrical_reference_channel"], 2)
        # The electrical reference rides the same input as the microphone, so
        # the observed reference node is that shared input, not a monitor.
        self.assertEqual(provenance["reference_node"], "capture_1")
        self.assertEqual(provenance["sample_rate_hz"], RATE)
        self.assertEqual(len(provenance["job_ids"]), 2)
        proposal = self.alignment.propose(captures, live_target=live_global_target(self.state))
        # Injected 240-sample acoustic offset between the ways at 48 kHz.
        self.assertAlmostEqual(proposal["added_delay_ms"]["left_low"], 5.0, delta=0.3)
        self.assertAlmostEqual(proposal["added_delay_ms"]["left_high"], 0.0, delta=0.3)
        candidate = proposal["candidate_state"]
        self.assertAlmostEqual(
            candidate["modes"]["stereo-sub"]["processing"]["left_low"]["alignment_ms"], 5.0, delta=0.3)
        self.assertEqual(len(proposal["overlap_checks"]), 1)

    async def test_invalid_identities_or_missing_reference_fail_before_capture(self):
        for options in ({"reference_id": ""}, {"reference_id": "  "},
                        {"microphone_position_id": ""}, {"reference_input_channel": ""}):
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
        def fail(*args, **kwargs):
            raise RuntimeError("result construction failed")

        self.store._persistence._build_measurement_from_analysis = fail
        with self.assertRaisesRegex(RuntimeError, "left_low"):
            await self.acquire()
        self.assertEqual(self.captures_started, 1)

    async def test_provenance_carries_session_binding(self):
        result = await self.acquire()
        self.assertEqual(result["provenance"]["reference_id"], REFERENCE_ID)
        self.assertEqual(result["provenance"]["microphone_position_id"], POSITION_ID)

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

    async def test_tolerated_reference_is_rejected(self):
        self.after_attempt = lambda analysis: analysis["clock"].update(end_score=0.8)
        self.store._capture_policy._should_keep_electrical_reference = lambda *args: True
        with self.assertRaisesRegex(RuntimeError, "electrical reference"):
            await self.acquire()
        self.assertEqual(self.captures_started, 1)
        self.assertEqual(len(self.store._jobs), 1)

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


if __name__ == "__main__":
    unittest.main()
