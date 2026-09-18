#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Authoritative AutoSub entry, with real state/rendering and no audio processes."""

import asyncio
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from audio.output_service import OutputService, OutputServiceDeps
from audio.output_state import default_output_state, set_crossover, set_mode_routing, switch_mode
from audio.output_state_store import OutputStateStore, StateConflictError
from dsp.manager import DSPManager
from fastapi import HTTPException
from measurement.autosub import deps
from measurement.autosub import jobs
from measurement.autosub import candidates
from measurement.autosub.runners import optimize, optimize_22, optimize_22_stereo
from measurement.autosub.runners import start


class CaptureStore:
    def resolve_capture_input_id(self, input_id, input_key=""):
        return input_id

    def has_active_measurement_job(self):
        return False


class RuntimeBoundary:
    def __init__(self):
        self.state = {"active": True, "helper_pid": 123, "output_gain_db": -12,
                      "config": None}
        self.links_valid = True
        self.on_verify = None
        self.transitions = []

    def snapshot(self):
        return copy.deepcopy(self.state)

    async def verify(self):
        if self.on_verify:
            self.on_verify()
        return self.links_valid

    async def guarded_rebuild_rendered(self, target, **kwargs):
        self.transitions.append(target)
        raise AssertionError("Start and activation must not change the graph")


class AutoSubServiceStartTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="autosub-start-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "output-state.json"
        self.manager = DSPManager(home=Path(directory.name) / "home")
        self.store = OutputStateStore(self.path)
        self.service = OutputService(OutputServiceDeps(
            store=self.store, preset_loader=self.manager.preset_store.read,
            resolve_ir=lambda name: self.fail(f"Unexpected IR: {name}"),
            measurement_active=lambda: True))
        self.runtime = RuntimeBoundary()
        self.session = SimpleNamespace(capture_entry_epoch=lambda: 42,
                                       measurement_rate=96000,
                                       active_auto_sub_job_id=None)
        self.overview = {
            "selected_output": {"key": "dev", "channels": 4, "active_rate": 44100},
            # Deliberately incompatible legacy configuration: only the device
            # fields may be consumed by authoritative AutoSub entry.
            "output_mode": {"mode": "stereo", "crossover_frequency_hz": 150,
                            "hardware_playback_ports": [f"playback_AUX{i}" for i in range(4)]}}
        self.owners = []
        self.dispatched = []
        self.patch(deps, "_autosub_deps", deps.AutoSubDependencies(
            get_dsp_runtime=lambda: self.runtime,
            get_measurement_store=CaptureStore,
            get_measurement_session=lambda: self.session,
            get_dsp_manager=lambda: self.manager,
            get_output_service=lambda: self.service,
            create_candidate_session=self.create_owner))
        self.patch(main.runtime, "dsp_runtime", self.runtime)
        self.patch(main, "dsp_manager", self.manager)
        self.patch(start, "get_audio_output_overview", lambda: copy.deepcopy(self.overview))
        self.patch(start, "_capture_auto_sub_playback_gain", lambda: {"linear": 1.0})
        self.patch(start, "_auto_sub_lock", asyncio.Lock())
        self.rollout_patch = self.patch(start, "_AUTO_SUB_SERVICE_INTEGRATION_READY", True)
        self.patch(start, "_start_auto_sub_worker", self.capture_worker)
        self.patch(main.samplerate, "_load_audio_output_mode",
                   lambda: self.fail("Authoritative start read legacy persistence"))
        for name in ("_run_auto_sub_optimize", "_run_auto_sub_22_optimize",
                     "_run_auto_sub_22_stereo_optimize"):
            async def runner(_name=name, **kwargs):
                self.dispatched.append((_name, kwargs))
            self.patch(start, name, runner)
        self.workers = []
        deps._AUTO_SUB_JOBS.clear()
        deps._AUTO_SUB_CANDIDATE_OWNERS.clear()
        self.addCleanup(deps._AUTO_SUB_JOBS.clear)
        self.addCleanup(deps._AUTO_SUB_CANDIDATE_OWNERS.clear)

    def patch(self, obj, name, value, **kwargs):
        patcher = patch.object(obj, name, value, **kwargs)
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher

    def seed(self, roles, *, mode="stereo-sub", channels=None):
        from audio.output_state import set_crossover
        crossover_enabled = mode == "crossover" or any(r.startswith(("left_", "right_")) for r in roles)
        target = "stereo-sub"
        state = set_mode_routing(set_crossover(default_output_state(), target, crossover_enabled), target, "dev", roles)
        state = switch_mode(state, target)
        active = state["modes"][target]
        active["bass_management"] = {"frequency_hz": 90, "main_highpass_enabled": False}
        for role, delay, level, polarity in (("sub_l", 3, -4, "invert"),
                                             ("sub_r", -7, -8, "normal"),
                                             ("sub1", 11, -12, "invert"),
                                             ("sub2", -15, -16, "normal")):
            if role in active["processing"]:
                active["processing"][role].update(
                    alignment_ms=delay, level_db=level, polarity=polarity)
        if crossover_enabled:
            for role, processing in active["processing"].items():
                if not role.startswith(("left_", "right_")):
                    continue
                if role.endswith("_low"):
                    processing["lowpass"] = {"family": "linkwitz-riley", "slope_db_oct": 24,
                                              "frequency_hz": 2000}
                elif role.endswith("_high"):
                    processing["highpass"] = {"family": "linkwitz-riley", "slope_db_oct": 24,
                                               "frequency_hz": 2000}
        count = channels if channels is not None else len(roles)
        self.overview["selected_output"]["channels"] = count
        self.overview["output_mode"]["hardware_playback_ports"] = [f"playback_AUX{i}" for i in range(count)]
        return self.store.commit(state, expected_revision=0)

    def create_owner(self, **kwargs):
        self.assertTrue(hasattr(main, "_create_auto_sub_candidate_session"),
                        "Production candidate-session composition is missing")
        owner = main._create_auto_sub_candidate_session(**kwargs)
        self.owners.append(owner)
        return owner

    def capture_worker(self, coro):
        self.workers.append(coro)
        self.addCleanup(coro.close)

    async def request(self, **overrides):
        args = dict(input_id="mic", input_key="", channel="left", mic_input_channel="1",
                    reference_input_channel="", reference_input_channel_left="2",
                    reference_input_channel_right="3", calibration_ref="",
                    target_curve_snapshot="", calibration_file=None)
        args.update(overrides)
        return await start.start_auto_sub_optimize(**args)

    def assert_no_start_leak(self):
        self.assertEqual(deps._AUTO_SUB_JOBS, {})
        self.assertEqual(deps._AUTO_SUB_CANDIDATE_OWNERS, {})
        self.assertFalse(start._auto_sub_lock.locked())
        self.assertEqual(self.workers, [])

    async def test_single_sub_fanout_uses_authoritative_knobs_and_inert_owner(self):
        self.seed(["sub_l", "main_r", "main_l", "sub_l"])
        before = self.path.read_bytes()
        with patch.object(self.service, "load", wraps=self.service.load) as load:
            response = await self.request()
        self.assertEqual(load.call_count, 1)
        job = response["job"]
        self.assertEqual(job["mode"], "subwoofer-2.1")
        self.assertEqual(job["output_state_context"], {
            "mode": "stereo-sub", "revision": 1, "output_key": "dev", "channels": 4,
            "optimizer_path": "single-sub", "sub_role_map": {"sub1": "sub_l"},
            "sub_mute_mask": 4})
        self.assertEqual(job["original_config_snapshot"]["subwoofer"], {
            "crossover_frequency_hz": 90, "main_highpass_enabled": False,
            "sub_alignment_ms": 3, "sub_level_db": -4, "sub_polarity": "invert"})
        self.assertEqual(job["original_alignment_ms"], 3)
        self.assertEqual(job["crossover_hz"], 90)
        self.assertIs(deps._candidate_owner(job["id"]), self.owners[0])
        json.dumps(job, allow_nan=False)
        await self.workers[0]
        name, kwargs = self.dispatched[0]
        self.assertEqual(name, "_run_auto_sub_optimize")
        self.assertEqual(kwargs["entry_epoch"], 42)
        self.assertEqual(kwargs["original_level"], -4)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.runtime.transitions, [])

    async def test_mixed_sub_roles_select_dual_mono_in_canonical_not_port_order(self):
        self.seed(["sub1", "main_r", "sub_r", "main_l"])
        job = (await self.request())["job"]
        self.assertEqual(job["output_state_context"]["sub_role_map"],
                         {"sub1": "sub_r", "sub2": "sub1"})
        self.assertEqual(job["mode"], "subwoofer-2.2")
        self.assertEqual(job["original_sub1_alignment_ms"], -7)
        self.assertEqual(job["original_sub2_alignment_ms"], 11)
        self.assertEqual(job["original_config_snapshot"]["subwoofers"], {
            "sub1": {"alignment_ms": -7, "level_db": -8, "polarity": "normal"},
            "sub2": {"alignment_ms": 11, "level_db": -12, "polarity": "invert"}})
        await self.workers[0]
        self.assertEqual(self.dispatched[0][0], "_run_auto_sub_22_optimize")

    async def test_crossover_stereo_subs_keep_sides_and_all_main_ways(self):
        self.seed(["sub_r", "right_high", "left_low", "sub_l", "right_low", "left_high"],
                  mode="crossover")
        job = (await self.request())["job"]
        context = job["output_state_context"]
        self.assertEqual(context["mode"], "stereo-sub")
        self.assertEqual(context["sub_role_map"], {"sub1": "sub_l", "sub2": "sub_r"})
        self.assertEqual(context["sub_mute_mask"], 48)
        self.assertEqual(job["scan_delays"]["left_sub"][4], 3)
        self.assertEqual(job["scan_delays"]["right_sub"][4], -7)
        await self.workers[0]
        self.assertEqual(self.dispatched[0][0], "_run_auto_sub_22_stereo_optimize")

    async def test_three_port_single_sub_is_supported(self):
        self.seed(["main_l", "main_r", "sub2"])
        job = (await self.request())["job"]
        self.assertEqual(job["output_state_context"]["channels"], 3)
        self.assertEqual(job["output_state_context"]["sub_role_map"], {"sub1": "sub2"})

    async def test_suspended_output_uses_device_channel_map_before_entry(self):
        self.seed(["main_l", "main_r", "sub1", "sub1"])
        output = self.overview["output_mode"]
        output["hardware_playback_ports_from_channel_map"] = output["hardware_playback_ports"]
        output["hardware_playback_ports"] = []
        job = (await self.request())["job"]
        self.session.active_auto_sub_job_id = job["id"]
        deps.activate_candidate_owner(job, self.session)
        plan = self.service.compile_plan(self.service.load(), output_key="dev", channels=4,
                                         sample_rate_hz=96000)
        self.runtime.state["config"] = {
            "sample_rate": 96000, "plan_fingerprint": self.service.fingerprint_plan(plan),
            "output_key": "dev", "hardware_ports": [f"playback_AUX{i}" for i in range(4)]}
        await self.owners[0].ensure_ready(96000)
        self.runtime.links_valid = False
        with self.assertRaises(RuntimeError):
            await self.owners[0].ensure_ready(96000)
        self.assertEqual(self.runtime.transitions, [])

    async def test_unknown_or_insufficient_ports_return_intentional_api_error(self):
        self.seed(["main_l", "main_r", "sub1", "sub1"])
        output = self.overview["output_mode"]
        for live, fallback in (([], []), ([], ["playback_AUX0", "playback_AUX1"]),
                               (["playback_AUX0"], [f"playback_AUX{i}" for i in range(4)])):
            with self.subTest(live=live, fallback=fallback):
                output["hardware_playback_ports"] = live
                output["hardware_playback_ports_from_channel_map"] = fallback
                with self.assertRaises(HTTPException) as raised:
                    await self.request()
                self.assertEqual(raised.exception.status_code, 400)
                self.assertIn("playback ports", str(raised.exception.detail))
                self.assert_no_start_leak()

    async def test_no_subs_or_truncated_sub_port_rejected_before_job_creation(self):
        self.seed(["main_l", "main_r", "sub1"], channels=2)
        with self.assertRaises(HTTPException) as raised:
            await self.request()
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("routed subwoofer", str(raised.exception.detail))
        self.assert_no_start_leak()

    async def test_incomplete_speaker_routing_rejected(self):
        self.seed(["left_low", "right_low", "sub1"], mode="crossover")
        with self.assertRaises(HTTPException) as raised:
            await self.request()
        self.assertEqual(raised.exception.status_code, 400)
        self.assert_no_start_leak()

    async def test_unknown_device_capacity_rejected(self):
        self.seed(["main_l", "main_r", "sub1"])
        self.overview["selected_output"].pop("channels")
        with self.assertRaises(HTTPException) as raised:
            await self.request()
        self.assertEqual(raised.exception.status_code, 400)
        self.assert_no_start_leak()

    async def test_supported_topology_is_open_in_production(self):
        self.seed(["main_l", "main_r", "sub1"])
        # Production default must be open after the rollout flip. An
        # accidentally closed gate must fail this behavioral test rather
        # than being patched open here.
        self.rollout_patch.stop()
        self.assertTrue(start._AUTO_SUB_SERVICE_INTEGRATION_READY,
                        "Production AutoSub rollout gate must be open")
        job = (await self.request())["job"]
        self.assertIn("output_state_context", job)
        self.assertIn(job["id"], deps._AUTO_SUB_JOBS)
        self.assertEqual(len(self.owners), 1)

    async def test_factory_failure_does_not_leave_job_or_lock(self):
        self.seed(["main_l", "main_r", "sub1"])
        with patch.object(start, "create_candidate_session", side_effect=RuntimeError("no runtime"), create=True):
            with self.assertRaisesRegex(RuntimeError, "no runtime"):
                await self.request()
        self.assert_no_start_leak()

    async def test_worker_scheduling_failure_removes_owner_and_job(self):
        self.seed(["main_l", "main_r", "sub1"])
        with patch.object(start, "_start_auto_sub_worker", side_effect=RuntimeError("scheduler closed")):
            with self.assertRaisesRegex(RuntimeError, "scheduler closed"):
                await self.request()
        self.assert_no_start_leak()

    async def activated_owner(self):
        self.seed(["main_l", "main_r", "sub1"])
        job = (await self.request())["job"]
        self.assertTrue(hasattr(deps, "activate_candidate_owner"), "Rate-bound entry hook is missing")
        self.session.active_auto_sub_job_id = job["id"]
        rate = deps.activate_candidate_owner(job, self.session)
        self.assertEqual(rate, 96000)
        plan = self.service.compile_plan(self.service.load(), output_key="dev", channels=3,
                                         sample_rate_hz=96000)
        self.runtime.state["config"] = {
            "sample_rate": 96000, "plan_fingerprint": self.service.fingerprint_plan(plan),
            "output_key": "dev", "hardware_ports": [f"playback_AUX{i}" for i in range(3)]}
        return self.owners[0]

    async def test_activation_uses_registered_session_rate_and_render_only_target(self):
        owner = await self.activated_owner()
        await owner.ensure_ready(96000)
        self.assertEqual(self.runtime.transitions, [])
        self.assertEqual(self.service.load()["revision"], 1)

    async def test_activation_requires_session_ownership(self):
        self.seed(["main_l", "main_r", "sub1"])
        job = (await self.request())["job"]
        self.assertTrue(hasattr(deps, "activate_candidate_owner"), "Rate-bound entry hook is missing")
        with self.assertRaisesRegex(RuntimeError, "ownership"):
            deps.activate_candidate_owner(job, self.session)

    async def test_revision_drift_during_entry_is_not_rebased(self):
        self.seed(["main_l", "main_r", "sub1"])
        job = (await self.request())["job"]
        self.assertTrue(hasattr(deps, "activate_candidate_owner"), "Rate-bound entry hook is missing")
        self.service.commit(self.service.load(), expected_revision=1)
        self.session.active_auto_sub_job_id = job["id"]
        with self.assertRaises(StateConflictError):
            deps.activate_candidate_owner(job, self.session)
        self.assertEqual(self.runtime.transitions, [])

    async def test_readiness_requires_real_link_verification(self):
        owner = await self.activated_owner()
        self.runtime.links_valid = False
        with self.assertRaises(RuntimeError):
            await owner.ensure_ready(96000)

    async def test_readback_rejects_process_replacement_during_link_verification(self):
        owner = await self.activated_owner()
        self.runtime.on_verify = lambda: self.runtime.state.update(helper_pid=456)
        with self.assertRaises(RuntimeError):
            await owner.ensure_ready(96000)

    async def test_readback_rejects_runtime_replacement(self):
        owner = await self.activated_owner()
        with patch.object(main.runtime, "dsp_runtime", RuntimeBoundary()):
            with self.assertRaises(RuntimeError):
                await owner.ensure_ready(96000)


class RunnerOwnerEntryTests(unittest.IsolatedAsyncioTestCase):
    async def exercise_entry(self, module, *, activation_error=None, cancel_entry=False):
        events = []
        job_id = "entry-job"
        job = {"id": job_id, "status": "preparing", "cancel_requested": False,
               "output_state_context": {"revision": 1}}
        session = SimpleNamespace(active_auto_sub_job_id=None, measurement_rate=96000)

        async def register(job_id, *, entry_epoch):
            self.assertEqual(entry_epoch, 42)
            events.append("entry")
            session.active_auto_sub_job_id = job_id
            job["cancel_requested"] = cancel_entry

        async def unregister(job_id):
            events.append("unregister")
            self.assertIn("restore", events)
            session.active_auto_sub_job_id = None

        def activate(rate):
            events.append(("activate", rate))
            self.assertEqual(session.active_auto_sub_job_id, job_id)
            if activation_error:
                raise activation_error

        async def restore():
            events.append("restore")

        async def capture(**kwargs):
            events.append(("capture", kwargs["auto_sub_rate"]))
            raise RuntimeError("stop before audio IO")

        session.register_auto_sub = register
        session.unregister_auto_sub = unregister
        owner = SimpleNamespace(activate=activate, restore=restore, committed=False)
        deps.register_candidate_owner(job_id, owner)
        self.addCleanup(deps.drop_candidate_owner, job_id)
        deps._AUTO_SUB_JOBS[job_id] = job
        self.addCleanup(deps._AUTO_SUB_JOBS.pop, job_id, None)
        lock = asyncio.Lock()
        await lock.acquire()
        dependency_set = deps.AutoSubDependencies(
            get_dsp_runtime=lambda: None, get_measurement_store=lambda: None,
            get_measurement_session=lambda: session, get_dsp_manager=lambda: None)
        snapshot = {"mode": "subwoofer-2.1", "crossover_frequency_hz": 90,
                    "main_highpass_enabled": True,
                    "subwoofer": {"sub_alignment_ms": 0, "sub_level_db": 0,
                                  "sub_polarity": "normal"},
                    "subwoofers": {"sub1": {}, "sub2": {}}}
        args = dict(job_id=job_id, input_id="mic", mic_input_channel="1",
                    reference_input_channel="", calibration_ref="",
                    calibration_filename=None, calibration_bytes=None, fc=90,
                    original_config_snapshot=snapshot, entry_epoch=42)
        if module is optimize:
            runner = module._run_auto_sub_optimize
            args.update(channel="left", scan_delays=[0], current_alignment=0,
                        original_polarity="normal", original_level=0, original_highpass=True)
        elif module is optimize_22:
            runner = module._run_auto_sub_22_optimize
            args.update(sub1_scan_delays=[0], sub2_scan_delays=[0], fine_step_ms=1)
        else:
            runner = module._run_auto_sub_22_stereo_optimize
            args.update(left_scan_delays=[0], right_scan_delays=[0])
        with patch.object(deps, "_autosub_deps", dependency_set), \
                patch.object(module, "_auto_sub_lock", lock), \
                patch.object(jobs, "_auto_sub_lock", lock), \
                patch.object(module, "_capture_auto_sub_main_references", capture), \
                patch.object(main.samplerate, "set_audio_output_mode",
                             side_effect=AssertionError("Service entry wrote legacy persistence")), \
                patch.object(candidates, "set_audio_output_mode",
                             side_effect=AssertionError("Service restore wrote legacy persistence")), \
                patch("measurement.session._resolve_measurement_start_sample_rate",
                      side_effect=AssertionError("Re-resolved the registered measurement rate")):
            await runner(**args)
        for task in list(deps._AUTO_SUB_CLEANUP_TASKS):
            task.cancel()
        await asyncio.gather(*deps._AUTO_SUB_CLEANUP_TASKS, return_exceptions=True)
        deps._AUTO_SUB_CLEANUP_TASKS.clear()
        self.assertEqual(events[:2], ["entry", ("activate", 96000)])
        self.assertNotIn(job_id, deps._AUTO_SUB_CANDIDATE_OWNERS)
        self.assertFalse(lock.locked())
        self.assertIsNone(session.active_auto_sub_job_id)
        self.assertLess(events.index("restore"), events.index("unregister"))
        if activation_error or cancel_entry:
            self.assertNotIn(("capture", 96000), events)
        else:
            self.assertIn(("capture", 96000), events)
        return job

    async def test_all_runners_activate_after_entry_and_keep_registered_rate(self):
        for module in (optimize, optimize_22, optimize_22_stereo):
            with self.subTest(runner=module.__name__):
                await self.exercise_entry(module)

    async def test_stale_activation_never_restores_through_legacy_setter(self):
        for module in (optimize, optimize_22, optimize_22_stereo):
            with self.subTest(runner=module.__name__):
                job = await self.exercise_entry(module, activation_error=StateConflictError("stale"))
                self.assertEqual(job["status"], "failed")

    async def test_cancellation_during_entry_releases_owner_before_session(self):
        for module in (optimize, optimize_22, optimize_22_stereo):
            with self.subTest(runner=module.__name__):
                job = await self.exercise_entry(module, cancel_entry=True)
                self.assertEqual(job["status"], "cancelled")


class OwnerEntryCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        for task in list(deps._AUTO_SUB_CLEANUP_TASKS):
            task.cancel()
        await asyncio.gather(*deps._AUTO_SUB_CLEANUP_TASKS, return_exceptions=True)
        deps._AUTO_SUB_CLEANUP_TASKS.clear()
        deps._AUTO_SUB_CANDIDATE_OWNERS.clear()

    async def test_repeated_cancellation_cannot_release_entry_during_owner_restore(self):
        entered_restore, finish_restore = asyncio.Event(), asyncio.Event()
        events = []
        job = {"id": "cancel-entry", "status": "failed", "output_state_context": {}}
        lock = asyncio.Lock()
        await lock.acquire()

        async def restore():
            entered_restore.set()
            await finish_restore.wait()
            events.append("restored")

        async def unregister(job_id):
            events.append("unregistered")

        deps.register_candidate_owner(job["id"], SimpleNamespace(committed=False, restore=restore))
        with patch.object(jobs, "_measurement_session", lambda: SimpleNamespace(unregister_auto_sub=unregister)), \
                patch.object(jobs, "_auto_sub_lock", lock), \
                patch.object(jobs, "_persist_auto_sub_job_snapshot"):
            task = asyncio.create_task(jobs._finish_auto_sub_worker(job, job["id"]))
            try:
                await asyncio.wait_for(entered_restore.wait(), 1)
                for _ in range(2):
                    task.cancel()
                    await asyncio.sleep(0)
                self.assertTrue(lock.locked())
                self.assertIn(job["id"], deps._AUTO_SUB_CANDIDATE_OWNERS)
                self.assertEqual(events, [])
            finally:
                finish_restore.set()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(events, ["restored", "unregistered"])
        self.assertFalse(lock.locked())
        self.assertNotIn(job["id"], deps._AUTO_SUB_CANDIDATE_OWNERS)

    async def test_committed_owner_never_restores_start_during_cleanup(self):
        job = {"id": "committed-entry", "status": "completed", "output_state_context": {}}

        async def restore():
            self.fail("Committed owner restored frozen start")

        deps.register_candidate_owner(job["id"], SimpleNamespace(committed=True, restore=restore))
        with patch.object(jobs, "_measurement_session", lambda: None), \
                patch.object(jobs, "_auto_sub_lock", asyncio.Lock()), \
                patch.object(jobs, "_persist_auto_sub_job_snapshot"):
            await jobs._finish_auto_sub_worker(job, job["id"])
        self.assertNotIn(job["id"], deps._AUTO_SUB_CANDIDATE_OWNERS)
        self.assertEqual(job["status"], "completed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
