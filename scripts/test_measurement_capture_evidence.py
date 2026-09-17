#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Full IR transport must pair the selected attempt and outlive worker cleanup."""

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from measurement.capture_evidence import CaptureEvidence
from measurement.signal import generate_log_sweep
from measurement.store import MeasurementStore


class CaptureEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="capture-evidence-")
        self.addCleanup(directory.cleanup)
        home = Path(directory.name)
        environment = patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(home / "config"), "XDG_STATE_HOME": str(home / "state"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.store = MeasurementStore(home=home)
        self.store._discover_capture_inputs = lambda: [{
            "id": "mic", "label": "Mic", "node_name": "capture_1", "node_serial": "123",
            "channels": 2, "sample_rate": 48000, "available": True,
        }]
        self.store._measurement_inputs_with_sample_rate = lambda inputs: inputs
        self.store._try_raise_mic_for_low_capture = lambda *args, **kwargs: False
        self.store._capture_policy._try_raise_mic = lambda *args, **kwargs: False
        self.store._capture_policy._should_keep_electrical_reference = lambda *args: False
        self.store._capture_policy._retry_sleep = lambda _: None
        self.store._capture_policy._cancel_aware_sleep = lambda *args: None
        self.attempts = 0
        self.events = []
        self.after_attempt = lambda analysis: None
        self.target = {"bank_id": "left_low", "roles": ["left_low", "left_high"],
                       "measured_roles": ["left_low"], "revision": 4, "legacy": False}
        self.store.measurement_target_provider = lambda *args: deepcopy(self.target)

        async def apply_mask(mask):
            self.events.append(("mask", mask))

        async def clear_mask(mask):
            self.events.append(("clear", mask))

        self.store.output_mask_apply = apply_mask
        self.store.output_mask_clear = clear_mask
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
            self.attempts += 1
            rate, playback = self.store._load_wav_array(self.store.playbacks_dir / f"{job_id}.wav")
            reference = playback[:, 0]
            delay = 240 * self.attempts
            mic = np.zeros_like(reference)
            mic[delay:] = reference[:-delay] * 0.5
            capture = np.column_stack([mic, reference] if self.is_electrical else [reference, mic])
            self.store._write_wav(Path(command[-1]), capture, rate)
        return SimpleNamespace(returncode=0, communicate=lambda **kw: ("", ""), poll=lambda: 0)

    async def start(self, owner=None, **overrides):
        kwargs = dict(input_id="mic", channel="left", reference_input_channel="2",
                      measurement_bank="left_low", sweep_profile={
                          "sweep_seconds": 0.68, "lead_in_seconds": 0.34, "tail_seconds": 0.18,
                      })
        if owner is not None:
            kwargs["capture_evidence"] = owner
        kwargs.update(overrides)
        return await self.store.start_measurement(**kwargs)

    async def complete(self, owner=None, **kwargs):
        job = await self.start(owner, **kwargs)
        await self.store._job_tasks[job["id"]]
        return self.store.get_job(job["id"])

    async def test_real_capture_delivers_full_ir_only_after_reference_evaluation(self):
        owner = CaptureEvidence()
        observed = []

        def inspect_candidate(analysis):
            observed.append(analysis["reference_path"]["timing_status"])
            with self.assertRaises(RuntimeError):
                owner.take()

        self.after_attempt = inspect_candidate
        job = await self.complete(owner)
        self.assertEqual(job["status"], "completed", job)
        evidence = owner.take()
        self.assertEqual(observed, ["electrical-reference-candidate"])
        reference = evidence["analysis"]["reference_path"]
        self.assertEqual(reference["stability"], "stable")
        self.assertTrue(reference["electrical_reference_used"])
        self.assertEqual(evidence["time_reference"], "deconvolved-sweep-origin")
        timing = evidence["analysis"]["impulse_response"]
        # The existing direct detector chooses a threshold crossing just before
        # the peak. The independently injected delay is peak-to-reference.
        self.assertEqual(timing["peak_index"] - timing["reference_peak_index"], 240)
        self.assertEqual(int(np.argmax(np.abs(evidence["impulse_response"]))), timing["peak_index"])
        self.assertGreater(len(evidence["impulse_response"]), 48000)
        self.assertEqual(evidence["measurement_target"], self.target)
        self.assertEqual(evidence["capture"]["microphone_node"], "capture_1")
        self.assertEqual(evidence["capture"]["electrical_reference_input_channel"], 2)
        self.assertEqual(evidence["capture"]["routing_diagnostics"]["link_diagnostics"]["links"], ["input_1", "input_2"])
        # Transport records observed input provenance; it cannot attest physical
        # upstream wiring or an unmoved microphone from an area label.
        self.assertNotIn("reference_id", evidence)
        self.assertNotIn("microphone_position_id", evidence)
        with self.assertRaises(RuntimeError):
            owner.take()

    async def test_retry_selects_its_own_ir_and_metadata(self):
        self.store._capture_policy._try_raise_mic = lambda analysis, **kw: kw["attempt_index"] == 0
        owner = CaptureEvidence()
        job = await self.complete(owner)
        self.assertEqual(job["status"], "completed", job)
        evidence = owner.take()
        self.assertEqual(self.attempts, 2)
        timing = evidence["analysis"]["impulse_response"]
        self.assertEqual(timing["peak_index"] - timing["reference_peak_index"], 480)
        self.assertEqual(int(np.argmax(np.abs(evidence["impulse_response"]))), timing["peak_index"])

    async def test_fallback_keeps_final_ir_and_rejected_reference_provenance(self):
        def reject_reference(analysis):
            if analysis["reference_path"]["capture_mode"] == "electrical-input":
                analysis["reference_path"]["clipped"] = True

        self.after_attempt = reject_reference
        owner = CaptureEvidence()
        job = await self.complete(owner)
        self.assertEqual(job["status"], "completed", job)
        evidence = owner.take()
        self.assertEqual(self.attempts, 2)
        self.assertTrue(evidence["analysis"]["reference_path"]["electrical_reference_fallback"])
        self.assertFalse(evidence["analysis"]["reference_path"]["electrical_reference_used"])
        self.assertIsNone(evidence["capture"]["electrical_reference_input_channel"])
        self.assertEqual(evidence["capture"]["reference_node"], "fxroute_dsp_sink.monitor")
        timing = evidence["analysis"]["impulse_response"]
        self.assertEqual(timing["peak_index"] - timing["reference_peak_index"], 480)

    async def test_no_evidence_leaks_to_job_result_or_disk(self):
        owner = CaptureEvidence()
        job = await self.complete(owner)
        self.assertEqual(job["status"], "completed", job)
        serialized = json.dumps(job, allow_nan=False)
        self.assertNotIn("deconvolved-sweep-origin", serialized)
        self.assertNotIn("capture_evidence", serialized)
        for path in self.store.job_records_dir.glob("*.json"):
            self.assertNotIn("deconvolved-sweep-origin", path.read_text())
        evidence = owner.take()
        job["result"]["analysis"]["reference_path"]["stability"] = "mutated"
        self.assertEqual(evidence["analysis"]["reference_path"]["stability"], "stable")

    async def test_terminal_status_is_not_cleanup_completion(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def clear_mask(mask):
            entered.set()
            await release.wait()
            self.events.append(("clear", mask))

        self.store.output_mask_clear = clear_mask
        owner = CaptureEvidence()
        job = await self.start(owner)
        try:
            await asyncio.wait_for(entered.wait(), 10)
            self.assertEqual(self.store.get_job(job["id"])["status"], "completed")
            with self.assertRaises(RuntimeError):
                owner.take()
        finally:
            release.set()
            await self.store.drain_job(job["id"])
        self.assertEqual(self.events, [("mask", 2), ("clear", 2)])
        self.assertGreater(owner.take()["impulse_response"].size, 48000)

    async def test_finished_task_can_be_consumed_before_done_callback_runs(self):
        cleanup_entered = asyncio.Event()

        async def clear_mask(mask):
            cleanup_entered.set()

        self.store.output_mask_clear = clear_mask
        owner = CaptureEvidence()
        job = await self.start(owner)
        async with asyncio.timeout(10):
            await cleanup_entered.wait()
        task = self.store._job_tasks[job["id"]]
        self.assertTrue(task.done())
        self.assertEqual(self.store.get_job(job["id"])["status"], "completed")
        await task
        await self.store.drain_job(job["id"])
        self.assertGreater(owner.take()["impulse_response"].size, 48000)
        # A late done callback must never republish already consumed evidence.
        await asyncio.sleep(0)
        with self.assertRaises(RuntimeError):
            owner.take()

    async def test_cancel_after_analysis_discards_ir(self):
        self.after_attempt = lambda analysis: self.store._cancelled_jobs.update(self.store._jobs)
        owner = CaptureEvidence()
        job = await self.complete(owner)
        self.assertEqual(job["status"], "cancelled")
        with self.assertRaises(RuntimeError):
            owner.take()
        self.assertEqual(self.events[-1], ("clear", 2))

    async def test_task_cancellation_waits_for_worker_with_received_ir(self):
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def pause_after_analysis(analysis):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(10):
                raise RuntimeError("test worker was not released")

        self.after_attempt = pause_after_analysis
        owner = CaptureEvidence()
        job = await self.start(owner)
        task = self.store._job_tasks[job["id"]]
        try:
            async with asyncio.timeout(10):
                await entered.wait()
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                with self.assertRaises(RuntimeError):
                    owner.take()
        finally:
            release.set()
            await task
        self.assertEqual(self.store.get_job(job["id"])["status"], "cancelled")
        with self.assertRaises(RuntimeError):
            owner.take()

    async def test_task_cancellation_during_cleanup_discards_selected_ir(self):
        entered = asyncio.Event()

        async def clear_mask(mask):
            entered.set()
            await asyncio.Event().wait()

        self.store.output_mask_clear = clear_mask
        owner = CaptureEvidence()
        job = await self.start(owner)
        task = self.store._job_tasks[job["id"]]
        async with asyncio.timeout(10):
            await entered.wait()
        self.assertEqual(self.store.get_job(job["id"])["status"], "completed")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaises(RuntimeError):
            owner.take()

    async def test_failure_after_analysis_discards_ir(self):
        def fail(*args, **kwargs):
            raise RuntimeError("result construction failed")

        self.store._persistence._build_measurement_from_analysis = fail
        owner = CaptureEvidence()
        job = await self.complete(owner)
        self.assertEqual(job["status"], "failed")
        with self.assertRaises(RuntimeError):
            owner.take()

    async def test_final_attempt_without_ir_cannot_reuse_previous_attempt(self):
        self.store._capture_policy._try_raise_mic = lambda analysis, **kw: kw["attempt_index"] == 0
        execute = self.store._host_capture_runner.execute

        def lose_second_ir(**kwargs):
            if self.attempts:
                kwargs.pop("timing_ir_receiver", None)
            return execute(**kwargs)

        self.store._host_capture_runner.execute = lose_second_ir
        owner = CaptureEvidence()
        job = await self.complete(owner)
        self.assertEqual(job["status"], "failed")
        self.assertIn("no matching full-resolution IR", job["error"]["detail"])
        self.assertEqual(self.attempts, 2)
        with self.assertRaises(RuntimeError):
            owner.take()

    async def test_replaced_analysis_cannot_borrow_attempt_ir(self):
        execute = self.store._host_capture_runner.execute

        def replace_analysis(**kwargs):
            analysis, capture, playback = execute(**kwargs)
            return deepcopy(analysis), capture, playback

        self.store._host_capture_runner.execute = replace_analysis
        owner = CaptureEvidence()
        job = await self.complete(owner)
        self.assertEqual(job["status"], "failed")
        self.assertIn("no matching full-resolution IR", job["error"]["detail"])
        with self.assertRaises(RuntimeError):
            owner.take()

    async def test_tolerated_reference_is_not_upgraded_by_transport(self):
        self.after_attempt = lambda analysis: analysis["clock"].update(end_score=0.8)
        self.store._capture_policy._should_keep_electrical_reference = lambda *args: True
        owner = CaptureEvidence()
        job = await self.complete(owner)
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(owner.take()["analysis"]["reference_path"]["stability"], "dsp-end-anchor-tolerated")

    async def test_cleanup_task_failure_discards_selected_evidence(self):
        def fail_cleanup(job_id):
            raise OSError("cleanup failed")

        self.store._job_runner._cleanup_job = fail_cleanup
        owner = CaptureEvidence()
        job = await self.start(owner)
        with self.assertRaisesRegex(OSError, "cleanup failed"):
            await self.store._job_tasks[job["id"]]
        with self.assertRaises(RuntimeError):
            owner.take()

    async def test_registration_failure_does_not_leave_evidence_accessible(self):
        owner = CaptureEvidence()
        with patch.object(self.store._persistence, "_persist_job", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                await self.start(owner)
        self.assertEqual(self.store._job_tasks, {})
        with self.assertRaises(RuntimeError):
            owner.take()

    async def test_cancellation_before_worker_start_has_no_evidence(self):
        owner = CaptureEvidence()
        job = await self.start(owner)
        task = self.store._job_tasks[job["id"]]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.attempts, 0)
        with self.assertRaises(RuntimeError):
            owner.take()

    async def test_owner_cannot_be_reused_for_another_job(self):
        owner = CaptureEvidence()
        job = await self.complete(owner)
        self.assertEqual(job["status"], "completed", job)
        with self.assertRaises(ValueError):
            await self.start(owner)
        self.assertEqual(len(self.store._jobs), 1)
        self.assertGreater(owner.take()["impulse_response"].size, 48000)

    async def test_unsupported_scope_or_pair_fails_before_capture(self):
        for kwargs in ({"measurement_scope": "raw_helper"}, {"measurement_role": "direct"},
                       {"capture_evidence": {}}, {"capture_evidence": lambda *args: None}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                await self.start(CaptureEvidence(), **kwargs)
        self.assertEqual(self.store._jobs, {})
        self.assertEqual(self.attempts, 0)

    async def test_legacy_capture_needs_no_receiver(self):
        job = await self.complete()
        self.assertEqual(job["status"], "completed", job)
        json.dumps(job, allow_nan=False)

    def test_analyzer_delivers_exact_unshifted_samples_without_changing_result(self):
        rate = 48000
        sweep = generate_log_sweep(rate, 0.68, 10, 22000, peak_scale=0.8)
        reference = np.concatenate([np.zeros(100), sweep, np.zeros(8640)])
        mic = np.zeros_like(reference)
        mic[4900], mic[5380], mic[35000] = 0.5, 0.25, -0.0625
        path = self.store.captures_dir / "exact.wav"
        self.store._write_wav(path, np.column_stack([mic, reference]), rate)
        _, decoded = self.store._load_wav_array(path)
        options = dict(expected_sample_rate=rate, channel="left", reference_sweep=sweep,
                       inverse_sweep=np.array([1.0]), calibration_curve=None,
                       reference_channel_index=1, analysis_channel_index=0,
                       timing_override={"alignment_samples": 100, "observed_sweep_samples": sweep.size,
                                        "start_score": 1.0, "end_score": 1.0})
        baseline = self.store._analyzer._analyze_sweep_capture(path, **options)
        observed = []
        result = self.store._analyzer._analyze_sweep_capture(
            path, **options, timing_ir_receiver=lambda ir, analysis: observed.append((ir.copy(), analysis)))
        self.assertEqual(result, baseline)
        self.assertEqual(len(observed), 1)
        np.testing.assert_allclose(observed[0][0], decoded[100:, 0], rtol=0, atol=1e-12)
        self.assertIs(observed[0][1], result)
        self.assertEqual(int(np.argmax(np.abs(observed[0][0]))), 4800)

    def test_analyzer_quality_failure_does_not_deliver_ir(self):
        from measurement.store import CaptureQualityError

        # Real analyzer rejects a clipped microphone before publishing a buffer.
        rate = 48000
        sweep = generate_log_sweep(rate, 0.68, 10, 22000, peak_scale=0.8)
        reference = np.concatenate([np.zeros(100), sweep, np.zeros(8640)])
        mic = np.zeros_like(reference)
        mic[4900] = 1.0
        path = self.store.captures_dir / "clipped.wav"
        self.store._write_wav(path, np.column_stack([mic, reference]), rate)
        delivered = []
        with self.assertRaises(CaptureQualityError):
            self.store._analyzer._analyze_sweep_capture(
                path, expected_sample_rate=rate, channel="left", reference_sweep=sweep,
                inverse_sweep=np.array([1.0]), calibration_curve=None,
                reference_channel_index=1, analysis_channel_index=0,
                timing_override={"alignment_samples": 100, "observed_sweep_samples": sweep.size,
                                 "start_score": 1.0, "end_score": 1.0},
                timing_ir_receiver=lambda *args: delivered.append(args),
            )
        self.assertEqual(delivered, [])


if __name__ == "__main__":
    unittest.main()
