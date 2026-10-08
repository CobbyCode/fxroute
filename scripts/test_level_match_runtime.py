#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Untrimmed logical-speaker captures preserve the committed graph and ownership."""

import asyncio
import copy
import math
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import MethodType

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.output_service import MeasurementActiveError, OutputService, OutputServiceDeps
from audio.output_state import set_output_processing
from audio.output_state_store import OutputStateStore
from dsp.runtime import CommandResult, DSPRuntime, DSPRuntimeConfig, PipeWireLink, PlannedSyncTarget
from measurement import level_match_api as api
from measurement.session import MeasurementSampleRateSession
import test_level_match as fixtures

mode_state = fixtures.mode_state


class MemoryRuntime(DSPRuntime):
    """Real guarded transitions with in-memory native I/O and compiled layouts."""

    def __init__(self, target):
        self.target = target
        self._output_gain_db = -7.0
        self.engine_gain = -7.0
        self.active = True
        self.stop_error = False
        self.restore_error = False
        self.original = target.config.plan_fingerprint
        self._lock = asyncio.Lock()
        self._measurement_scope_lock = asyncio.Lock()
        self._control_socket = object()
        self._process = None
        self._links = []

    @property
    def gain(self):
        return self._output_gain_db

    @gain.setter
    def gain(self, value):
        self._output_gain_db = value

    def snapshot(self):
        config = self.target.config
        return {"active": self.active, "helper_pid": 17 if self.active else None, "output_gain_db": self.gain,
                "config": {"sample_rate": config.sample_rate, "output_mode": config.output_mode,
                           "output_key": config.output_key, "hardware_ports": list(config.hardware_ports),
                           "layout": [dict(row) for row in config.layout],
                           "plan_fingerprint": config.plan_fingerprint}}

    async def verify(self):
        return self.active

    async def read_output_gain_db(self):
        self.gain = self.engine_gain
        return self.engine_gain

    async def set_output_gain_db(self, value):
        self.engine_gain = self.gain = value
        return value

    async def stop(self):
        if self.stop_error:
            raise RuntimeError("stop refused")
        self.active = False

    def _can_hot_update(self, config):
        return True

    async def _run_sync(self, config, text, **kwargs):
        if self.restore_error and config.plan_fingerprint == self.original:
            raise RuntimeError("restore refused")
        self.target = PlannedSyncTarget(config=config, text=text)


class UntrimmedRuntimeTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.JobTests.setUp
    configure = fixtures.JobTests.configure
    run_job = fixtures.JobTests.run_job

    def wire(self, *, mode="stereo-sub", failure=False, unequal=True):
        from measurement.level_match_runtime import build_level_match_scope

        roles = ["left_low", "right_low", "left_high", "right_high", "sub1"]
        if mode == "surround":
            roles += ["center", "rear_l", "rear_r"]
        state = mode_state(mode, roles, crossover=True)
        if unequal:
            state = set_output_processing(state, mode, "left_high", level_cal_db={"bank": -12.0})
        state = set_output_processing(state, mode, "sub1", level_cal_db={"global": -4.0, "bank": -3.0})
        self.configure(state, {})
        self.session = MeasurementSampleRateSession()
        self.session.active = True
        state_store = OutputStateStore(Path(self.directory.name) / "output-state.json")
        state_store.commit(state, expected_revision=0)
        self.state = state_store.load()
        self.store.state = self.state
        self.service = OutputService(OutputServiceDeps(
            store=state_store, preset_loader=lambda name: {"chain": []}, resolve_ir=lambda name: {},
            measurement_active=lambda: self.session.has_active_jobs))
        self.ports = [f"playback_AUX{index}" for index in range(len(roles))]

        def build_target(plan, *, output_key, hardware_ports, fingerprint):
            config = DSPRuntimeConfig.from_plan(
                plan, layout=self.service.compile_layout(plan), output_key=output_key,
                sample_rate_hz=48000, hardware_ports=hardware_ports, plan_fingerprint=fingerprint)
            return PlannedSyncTarget(config=config, text="unused native boundary")

        plan = self.service.compile_plan(self.state, output_key="A", channels=len(roles), sample_rate_hz=48000)
        self.native = MemoryRuntime(build_target(plan, output_key="A", hardware_ports=self.ports,
                                                fingerprint=self.service.fingerprint_plan(plan)))
        self.original = copy.deepcopy(self.native.snapshot())
        self.persisted = state_store.path.read_bytes()

        async def prepare(rate, **kwargs):
            self.assertTrue(self.session.has_active_jobs)

        scope = build_level_match_scope(
            output_service=self.service, get_session=lambda: self.session,
            get_native_runtime=lambda: self.native, build_target=build_target, prepare_measurement=prepare)
        api.configure_level_match(replace(api._deps, get_session=lambda: self.session,
                                         get_output_state=self.service.load, capture_scope=scope))

        def observed(job_id):
            call = self.store.started[int(job_id.split("-")[1]) - 1]
            measured = call["frozen_target"]["measured_roles"]
            layout = self.native.snapshot()["config"]["layout"]
            gains = [row["gain_db"] for row in layout if row["name"] in measured]
            # Equal energy in two non-overlapping ways: combined level is not mean dB.
            level = -20.0 + 10 * math.log10(sum(10 ** (gain / 10) for gain in gains) / len(gains))
            return {"status": "completed", "result": {
                "analysis": {"sample_rate": 48000, "normalized_by_db": level,
                             "impulse_response": {"timing_status": "ambiguous" if failure else "valid"}},
                "capture": {"microphone_node": "mic", "mic_input_channel": 1}}}

        self.store.get_job = observed
        self.drained = []

        async def drain(job_id):
            self.assertTrue(self.session.has_active_jobs)
            self.drained.append(job_id)

        self.store.drain_job = drain
        return state_store

    async def test_unequal_legacy_way_trims_measure_the_actual_untrimmed_response(self):
        state_store = self.wire()
        job = await self.run_job("global")
        self.assertEqual(job["status"], "completed", job["error"])
        self.assertEqual(job["result"]["values"], {
            role: {"global": 0.0, "bank": 0.0}
            for role in ("left_low", "left_high", "right_low", "right_high")})
        self.assertEqual(self.native.snapshot(), self.original)
        self.assertEqual(state_store.path.read_bytes(), self.persisted)
        self.assertFalse(self.session.has_active_jobs)
        self.assertEqual(len(self.drained), 2)
        for call in self.store.started:
            self.assertEqual(call["expected_plan_fingerprint"],
                             call["frozen_target"]["processing_fingerprint"])
            sub = next(row for row in call["expected_native_layout"] if row["name"] == "sub1")
            self.assertEqual(sub["gain_db"], -7.0)

    async def test_surround_front_ways_neutralize_only_the_front_owned_slot(self):
        self.wire(mode="surround")
        # Front matching owns Global, not a manually retained bank calibration.
        self.state = set_output_processing(self.state, "surround", "left_high", level_cal_db={"global": -12.0, "bank": 0.0})
        self.service._deps.store.commit(self.state, expected_revision=self.state["revision"])
        self.state = self.service.load()
        self.store.state = self.state
        plan = self.service.compile_plan(self.state, output_key="A", channels=8, sample_rate_hz=48000)
        self.native.target = PlannedSyncTarget(config=DSPRuntimeConfig.from_plan(
            plan, layout=self.service.compile_layout(plan), output_key="A", sample_rate_hz=48000,
            hardware_ports=self.ports, plan_fingerprint=self.service.fingerprint_plan(plan)), text="unused")
        self.native.original = self.native.target.config.plan_fingerprint
        original = copy.deepcopy(self.native.snapshot())
        job = await self.run_job("low")
        self.assertEqual(job["status"], "completed", job["error"])
        self.assertEqual(job["result"]["values"], {
            role: {"global": 0.0} for role in ("left_low", "left_high", "right_low", "right_high")})
        self.assertEqual(self.native.snapshot(), original)

    async def test_bad_capture_restores_without_persisting_a_partial_table(self):
        state_store = self.wire(failure=True)
        job = await self.run_job("all")
        self.assertEqual(job["status"], "failed")
        self.assertIn("did not reach", job["error"])
        self.assertEqual(self.table.entries("stereo-sub"), {})
        self.assertEqual(self.native.snapshot(), self.original)
        self.assertEqual(state_store.path.read_bytes(), self.persisted)
        self.assertFalse(self.session.has_active_jobs)

    async def test_restore_failure_never_reports_or_stores_success(self):
        self.wire()
        self.native.restore_error = True
        job = await self.run_job("global")
        self.assertEqual(job["status"], "failed")
        self.assertIn("restore", job["error"])
        self.assertEqual(self.table.entries("stereo-sub"), {})
        self.assertFalse(self.native.snapshot()["active"])
        self.assertFalse(self.session.has_active_jobs)

    async def test_failed_restore_and_stop_keep_ownership_until_recovery(self):
        self.wire()
        self.native.restore_error = self.native.stop_error = True
        job = await self.run_job("global")
        self.assertEqual(job["status"], "failed")
        self.assertTrue(self.session.has_active_jobs)
        with self.assertRaises(MeasurementActiveError):
            self.service.apply(lambda state: state, expected_revision=self.state["revision"])
        self.assertEqual(self.table.entries("stereo-sub"), {})

    async def test_unreaped_production_helper_keeps_the_recovery_latch(self):
        self.wire()

        class StubbornProcess:
            pid = 4321
            returncode = None

            def terminate(self):
                pass

            def kill(self):
                pass

            async def wait(self):
                raise asyncio.TimeoutError()

        async def run(args):
            return CommandResult(1, "", "disconnect failed")

        production = DSPRuntime(object(), command_runner=run)
        child = production._process = StubbornProcess()
        production._links = [PipeWireLink("fxroute-dsp:output_1", "hw:playback_FL")]

        async def stop():
            await production.stop_verified()
            self.native.active = False

        self.native.stop_verified = stop
        self.native.restore_error = True
        job = await self.run_job("global")
        self.assertEqual(job["status"], "failed")
        self.assertIn("exit could not be confirmed", job["error"])
        self.assertTrue(self.session.has_active_jobs)
        self.assertIs(production._process, child)
        self.assertEqual(production.snapshot()["helper_pid"], 4321)
        self.assertEqual(self.table.entries("stereo-sub"), {})

    async def test_common_trim_run_holds_ownership_between_takes(self):
        self.wire(unequal=False)
        original = self.store.start_measurement
        edit_blocked = []

        async def start(**kwargs):
            try:
                self.service.apply(
                    lambda state: set_output_processing(state, "stereo-sub", "right_low", level_cal_db={"bank": -6}),
                    expected_revision=self.service.load()["revision"])
            except MeasurementActiveError:
                edit_blocked.append(True)
            return await original(**kwargs)

        self.store.start_measurement = start
        job = await self.run_job("global")
        self.assertEqual(job["status"], "completed", job["error"])
        self.assertEqual(edit_blocked, [True, True])
        self.assertEqual(self.service.load()["revision"], self.state["revision"])

    async def test_common_trim_run_waits_for_child_cleanup_before_success(self):
        self.wire(unequal=False)
        entered = asyncio.Event()
        gate = asyncio.Event()

        async def drain(job_id):
            entered.set()
            await gate.wait()

        self.store.drain_job = drain
        await api.start_level_match(input_id="mic", input_key="", mic_input_channel="1",
                                    calibration_ref="", bank_id="global")
        await asyncio.wait_for(entered.wait(), 1)
        self.assertEqual(api._slot.job["status"], "running")
        self.assertTrue(self.session.has_active_jobs)
        self.assertEqual(self.table.entries("stereo-sub"), {})
        gate.set()
        await api._slot.task
        self.assertEqual(api._slot.job["status"], "completed", api._slot.job["error"])
        self.assertFalse(self.session.has_active_jobs)

    async def test_real_child_cleanup_precedes_parent_success(self):
        from measurement.job_runner import MeasurementJobRunner
        from measurement.store import MeasurementStore

        self.wire(unequal=False)
        original_start = self.store.start_measurement
        observed = self.store.get_job
        jobs = {}
        drain_entered = asyncio.Event()
        gate = asyncio.Event()

        async def apply_mask(mask):
            return 0

        async def clear_mask(mask):
            await gate.wait()

        runner = MeasurementJobRunner(
            get_job=lambda job_id: jobs[job_id], persist_job=lambda job: None,
            public_result=lambda result: result, cleanup_job=lambda job_id: None,
            retain_history=lambda: None, utc_now=lambda: "1",
            is_terminal=lambda status: status in ("completed", "failed", "cancelled"),
            output_mask_apply=apply_mask, output_mask_clear=clear_mask)
        self.store._job_tasks = runner.tasks
        real_drain = MethodType(MeasurementStore.drain_job, self.store)

        async def start(**kwargs):
            started = await original_start(**kwargs)
            job_id = started["id"]
            result = observed(job_id)["result"]
            child = jobs[job_id] = {"id": job_id, "status": "queued", "measurement_scope": "active_chain", "output_mask": 1}
            runner.start(job_id, child, lambda current: result)
            return started

        async def drain(job_id):
            drain_entered.set()
            await real_drain(job_id)

        self.store.start_measurement = start
        self.store.get_job = lambda job_id: copy.deepcopy(jobs[job_id])
        self.store.drain_job = drain
        await api.start_level_match(input_id="mic", input_key="", mic_input_channel="1",
                                    calibration_ref="", bank_id="global")
        await asyncio.wait_for(drain_entered.wait(), 2)
        self.assertEqual(jobs["take-1"]["status"], "completed")
        self.assertFalse(runner.tasks["take-1"].done())
        self.assertEqual(api._slot.job["status"], "running")
        self.assertTrue(self.session.has_active_jobs)
        self.assertEqual(self.table.entries("stereo-sub"), {})
        gate.set()
        await api._slot.task
        self.assertEqual(api._slot.job["status"], "completed", api._slot.job["error"])
        self.assertTrue(all(task.done() for task in runner.tasks.values()))
        self.assertFalse(self.session.has_active_jobs)

    async def test_rest_cancel_during_final_restore_does_not_store_or_complete(self):
        self.wire()
        entered = asyncio.Event()
        gate = asyncio.Event()
        original = self.native.guarded_rebuild_rendered

        async def transition(new, **kwargs):
            if new.config.plan_fingerprint == self.native.original:
                entered.set()
                await gate.wait()
            await original(new, **kwargs)

        self.native.guarded_rebuild_rendered = transition
        started = await api.start_level_match(input_id="mic", input_key="", mic_input_channel="1",
                                               calibration_ref="", bank_id="global")
        await entered.wait()
        await api.cancel_level_match_job(started["job"]["id"])
        gate.set()
        await api._slot.task
        self.assertEqual(api._slot.job["status"], "cancelled")
        self.assertEqual(self.table.entries("stereo-sub"), {})
        self.assertEqual(self.native.snapshot(), self.original)
        self.assertFalse(self.session.has_active_jobs)

    async def test_engine_gain_not_cached_gain_is_restored(self):
        self.wire()
        self.native.engine_gain = -10.0
        job = await self.run_job("global")
        self.assertEqual(job["status"], "completed", job["error"])
        self.assertEqual(self.native.engine_gain, -10.0)
        self.assertEqual(self.native.snapshot()["output_gain_db"], -10.0)

    async def test_revision_change_after_teardown_cannot_store_a_stale_table(self):
        self.wire(unequal=False)
        unregister = self.session.unregister_speaker_job

        async def release(job_id):
            await unregister(job_id)
            self.service.apply(
                lambda state: set_output_processing(state, "stereo-sub", "right_low", level_db=-6),
                expected_revision=self.service.load()["revision"])

        self.session.unregister_speaker_job = release
        job = await self.run_job("global")
        self.assertEqual(job["status"], "failed")
        self.assertEqual(self.table.entries("stereo-sub"), {})

    async def test_close_invalidates_a_start_queued_before_worker_entry(self):
        self.wire()

        async def release():
            self.session.active = False

        async def enter(rate):
            self.session.active = True
            return self.session.generation

        self.session._release = release
        self.session._start_locked = enter
        await api.start_level_match(input_id="mic", input_key="", mic_input_channel="1",
                                    calibration_ref="", bank_id="global")
        await self.session.request_close()
        await api._slot.task
        self.assertNotEqual(api._slot.job["status"], "completed")
        self.assertEqual(self.store.started, [])
        self.assertEqual(self.table.entries("stereo-sub"), {})
        self.assertFalse(self.session.active)

    async def test_existing_measurement_cannot_be_overwritten(self):
        self.wire()
        await self.session.register_manual_job("other")
        job = await self.run_job("global")
        self.assertEqual(job["status"], "failed")
        self.assertEqual(self.store.started, [])
        self.assertEqual(self.native.snapshot(), self.original)
        self.assertEqual(self.session.active_manual_job_ids, {"other"})

    async def test_cancellation_drains_capture_before_restoring_and_unlocking(self):
        self.wire()
        entered = asyncio.Event()
        gate = asyncio.Event()
        original = self.store.start_measurement

        async def start(**kwargs):
            result = await original(**kwargs)
            entered.set()
            return result

        def pending(job_id):
            return {"status": "running"}

        async def drain(job_id):
            self.assertTrue(self.session.has_active_jobs)
            await gate.wait()
            self.drained.append(job_id)

        self.store.start_measurement = start
        self.store.get_job = pending
        self.store.drain_job = drain
        await api.start_level_match(input_id="mic", input_key="", mic_input_channel="1",
                                    calibration_ref="", bank_id="global")
        await entered.wait()
        api._slot.task.cancel()
        await asyncio.sleep(0)
        self.assertTrue(self.session.has_active_jobs)
        self.assertNotEqual(self.native.snapshot()["config"], self.original["config"])
        gate.set()
        await api._slot.task
        self.assertEqual(api._slot.job["status"], "cancelled")
        self.assertEqual(self.native.snapshot(), self.original)
        self.assertFalse(self.session.has_active_jobs)
        self.assertEqual(len(self.drained), 1)


if __name__ == "__main__":
    unittest.main()
