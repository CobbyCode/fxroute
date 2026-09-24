#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""AutoSub sweeps of service jobs stage, pre-arm and verify through the owner.

Legacy jobs keep the persisted-config sync path untouched. Service jobs
(jobs carrying ``output_state_context``) must instead:

* stage the funnel candidate as a complete role-keyed proposal through the
  registered owner — never persist or sync legacy mode state,
* pre-arm with ``owner.ensure_ready(rate)`` instead of the orchestration
  measurement-sweep sync,
* predict peaks from the staged compiled layout (the persisted legacy
  overview still shows the frozen start and would model the wrong graph),
* hand the staged native layout/mode/fingerprint to the child sweep so the
  routing comparison validates the graph that actually runs,
* mute Main-only references with the context's role-derived sub mask.

The pure funnel-args-to-proposal adapter is covered directly as well.
"""

import asyncio
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from audio.output_service import OutputService, OutputServiceDeps  # noqa: E402
from audio.output_state import default_output_state, set_crossover, set_mode_routing, switch_mode
from audio.output_state_store import OutputStateStore  # noqa: E402
from dsp.manager import DSPManager  # noqa: E402
from dsp.runtime import DSPRuntimeConfig, PlannedSyncTarget  # noqa: E402

import audio.samplerate as samplerate_module  # noqa: E402
import measurement.autosub.measurement as funnel  # noqa: E402
import measurement.autosub.jobs as autosub_jobs  # noqa: E402
import measurement.session as measurement_session  # noqa: E402
from measurement.autosub import deps as autosub_deps  # noqa: E402
from measurement.autosub.candidate_session import AutoSubCandidateSession  # noqa: E402
from measurement.autosub.roles import autosub_scan_knobs, sub_mute_indices  # noqa: E402

PROFILE = {"sweep_start_hz": 20.0, "sweep_end_hz": 600.0, "sweep_seconds": 0.25}
RATE = 48000
PORTS = [f"playback_AUX{i}" for i in range(4)]


class HardwareBoundary:
    """Owner-side fake: render-only targets, real fingerprint tracking."""

    def __init__(self, service, context):
        self.service = service
        self.context = context
        self.target = None
        self.active = True
        self.gain = 0.0
        self.transitions = []
        self.fail_after_reads = None
        self.reads = 0

    def build_target(self, plan, *, fingerprint):
        rate = plan["sample_rate_hz"]
        layout = self.service.compile_layout(plan)
        config = DSPRuntimeConfig.from_plan(
            plan, layout=layout, output_key="dev", sample_rate_hz=rate,
            hardware_ports=PORTS, plan_fingerprint=fingerprint)
        return PlannedSyncTarget(config, config and self.service.manager.compile_engine_text(
            layout, preset_name=plan["global"]["preset"], sample_rate_hz=rate,
            extras_override=plan["global"]["extras"]))

    async def readback(self):
        self.reads += 1
        fingerprint = self.target.config.plan_fingerprint
        if self.fail_after_reads is not None and self.reads > self.fail_after_reads:
            fingerprint = "tampered-fingerprint"
        return {"active": self.active, "output_gain_db": self.gain, "config": {
            "plan_fingerprint": fingerprint, "sample_rate": RATE}}

    async def guarded_stage(self, new, *, previous, guard_db, apply_candidate,
                            apply_previous, before_ramp, before_rollback_ramp,
                            ramp_target_db, rollback_ramp_target_db):
        self.transitions.append(new)
        self.gain = guard_db
        try:
            apply_candidate()
            self.target = new
            await before_ramp()
            self.gain = ramp_target_db
        except BaseException:
            apply_previous()
            self.target = previous
            await before_rollback_ramp()
            self.gain = rollback_ramp_target_db
            raise


class FakeMeasureRuntime:
    """Funnel-side fake: snapshots, peaks and exact mute without audio."""

    def __init__(self):
        self.mute_calls = []
        self.muted = False
        self.captured_prediction = None
        self.captured_sink_gain = 1.0
        self.muted_keys = set()

    def snapshot(self):
        return {"active": True, "output_gain_db": 0.0, "exact_sub_mute": self.muted}

    async def reset_output_peaks(self):
        return None

    async def read_output_peaks(self):
        assert self.captured_prediction is not None, "meter read before any prediction"
        # The native meter reads engine peaks (pre-sink); the comparison
        # folds the sink gain in, so the fake reports the prediction with
        # the sink factored back out.
        linear = self.captured_prediction["linear"]
        return {key: (0.0 if key in self.muted_keys else value / self.captured_sink_gain)
                for key, value in linear.items()}

    async def set_exact_sub_mute(self, enabled, *, mask=None):
        self.mute_calls.append((bool(enabled), mask))
        self.muted = bool(enabled)
        return False


class FakeStore:
    def __init__(self):
        self.starts = []

    async def start_measurement(self, **kwargs):
        self.starts.append(copy.deepcopy(kwargs))
        return {"id": "sweep-1"}

    def get_job(self, job_id):
        assert job_id == "sweep-1"
        return {"status": "completed", "result": {"measurement": {
            "traces": [{"kind": "sweep-response",
                        "points": [[50.0, 0.0], [100.0, 0.0]]}],
            "analysis": {"alignment_samples": 1000, "sample_rate": RATE},
            "channel": "left"}}}

    async def drain_job(self, job_id):
        assert job_id == "sweep-1"

    def cancel_job(self, job_id):
        pass


def explode(message):
    async def _async(*args, **kwargs):
        raise AssertionError(message)

    def _sync(*args, **kwargs):
        raise AssertionError(message)

    return _async, _sync


class OwnerPrearmTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="owner-prearm-")
        self.addCleanup(directory.cleanup)
        self.manager = DSPManager(home=Path(directory.name) / "dsp")
        self.store = OutputStateStore(Path(directory.name) / "output-state.json")
        self.service = OutputService(OutputServiceDeps(
            store=self.store, preset_loader=self.manager.preset_store.read,
            resolve_ir=lambda name: (_ for _ in ()).throw(AssertionError(name)),
            measurement_active=lambda: True))
        self.service.manager = self.manager
        state = switch_mode(set_mode_routing(default_output_state(), "stereo-sub", "dev", ["main_l", "main_r", "sub1", "sub1"]), "stereo-sub")
        state["modes"]["stereo-sub"]["processing"]["sub1"]["alignment_ms"] = 2.0
        state["modes"]["stereo-sub"]["processing"]["sub1"]["level_db"] = -3.0
        self.start = self.service.commit(state, expected_revision=0)
        self.context = {"mode": "stereo", "revision": self.start["revision"],
                        "output_key": "dev", "channels": 4,
                        "optimizer_path": "single-sub",
                        "sub_role_map": {"sub1": "sub1"}, "sub_mute_mask": 4}
        self.hardware = HardwareBoundary(self.service, self.context)
        initial = self.target_for(self.start)
        self.hardware.target = initial
        self.owner = AutoSubCandidateSession(
            service=self.service, start_state=self.start,
            build_target=self.hardware.build_target,
            guarded_stage=self.hardware.guarded_stage,
            readback=self.hardware.readback, output_key="dev", channels=4)
        self.owner.activate(RATE)
        self.runtime = FakeMeasureRuntime()
        self.measure_store = FakeStore()
        autosub_deps.configure_dependencies(autosub_deps.AutoSubDependencies(
            get_dsp_runtime=lambda: self.runtime,
            get_measurement_store=lambda: self.measure_store,
            get_measurement_session=lambda: None,
            get_dsp_manager=lambda: self.manager,
            get_output_service=lambda: self.service,
            create_candidate_session=lambda **kwargs: self.fail("unexpected owner creation")))
        autosub_deps.register_candidate_owner("job-1", self.owner)
        self.addCleanup(autosub_deps.drop_candidate_owner, "job-1")
        self.addCleanup(setattr, autosub_deps, "_autosub_deps", None)
        self.patchers = []
        # Legacy persist/load/overview/sync entry points are deleted; the
        # service sweep stages through the owner with no legacy fallback.
        self.patch(measurement_session, "_sync_dsp_runtime_for_measurement_sweep",
                   explode("orchestration sync in service sweep")[0])
        self.patch(funnel, "_auto_sub_fresh_master_percent", self.master_percent)
        real_predict = autosub_jobs._predict_auto_sub_stage_peaks

        async def spy_predict(**kwargs):
            result = await real_predict(**kwargs)
            self.runtime.captured_prediction = copy.deepcopy(result)
            self.runtime.captured_sink_gain = kwargs.get("sink_gain", 1.0)
            if self.mute_next:
                self.runtime.muted_keys = set(
                    f"output_{index + 1}" for index in sub_mute_indices(self.mute_next))
            return result

        self.mute_next = None
        self.patch(funnel, "_predict_auto_sub_stage_peaks", spy_predict)

    def patch(self, obj, name, value):
        patcher = patch.object(obj, name, value)
        patcher.start()
        self.patchers.append(patcher)
        self.addCleanup(patcher.stop)

    async def master_percent(self):
        return 70

    def target_for(self, state):
        plan = self.service.compile_plan(state, sample_rate_hz=RATE,
                                         output_key="dev", channels=4)
        return self.hardware.build_target(plan, fingerprint=self.service.fingerprint_plan(plan))

    def service_job(self):
        return {"id": "job-1", "status": "preparing", "cancel_requested": False,
                "output_state_context": dict(self.context),
                "playback_gain": {"linear": 1.0},
                "reference_channels": {"left": "", "right": ""},
                "current_sweep_id": ""}

    async def sweep(self, job, **overrides):
        args = dict(delay_ms=5.0, job=job, candidate_index=1, total=1, stage="coarse",
                    fc=80, input_id="mic", channel="left", mic_input_channel="1",
                    reference_input_channel="", calibration_ref="",
                    calibration_filename=None, calibration_bytes=None,
                    auto_sub_sweep_profile=PROFILE, auto_sub_rate=RATE,
                    original_level=-3.0, original_polarity="normal",
                    original_highpass=True, output_mode="subwoofer-2.1",
                    original_config_snapshot={"mode": "subwoofer-2.1"})
        args.update(overrides)
        return await funnel._measure_auto_sub_candidate(**args)

    async def test_service_capture_drains_before_exact_mute_restore(self):
        job = self.service_job()
        self.mute_next = self.context["sub_mute_mask"]
        events = []
        real_mute = self.runtime.set_exact_sub_mute

        async def drain(job_id):
            self.assertEqual(job_id, "sweep-1")
            self.assertTrue(self.runtime.muted)
            events.append("drained")

        async def mute(enabled, *, mask=None):
            if not enabled:
                events.append("unmuted")
            return await real_mute(enabled, mask=mask)

        self.measure_store.drain_job = drain
        self.runtime.set_exact_sub_mute = mute
        await self.sweep(job, exact_sub_mute=True)
        self.assertEqual(events, ["drained", "unmuted"])

    async def test_drain_error_after_finished_child_still_restores_exact_mute(self):
        # A deferred drain error (e.g. cancel-request persist failure) must
        # not skip the funnel's exact-mute restoration: the child cleanup
        # completes, the mute is restored, and only then does the error
        # surface to fail the run.
        job = self.service_job()
        self.mute_next = self.context["sub_mute_mask"]

        async def drain(job_id):
            raise OSError("disk full while persisting cancellation")

        self.measure_store.drain_job = drain
        with self.assertRaises(OSError):
            await self.sweep(job, exact_sub_mute=True)
        self.assertEqual(self.runtime.mute_calls[-1], (False, self.context["sub_mute_mask"]))
        self.assertFalse(self.runtime.muted)

    async def test_cancelled_service_capture_keeps_mute_until_drain_finishes(self):
        job = self.service_job()
        self.mute_next = self.context["sub_mute_mask"]
        draining = asyncio.Event()
        release = asyncio.Event()
        events = []
        real_start = self.measure_store.start_measurement

        async def start(**kwargs):
            child = await real_start(**kwargs)
            job["cancel_requested"] = True
            return child

        async def drain(job_id):
            draining.set()
            await release.wait()
            events.append("drained")

        self.measure_store.start_measurement = start
        self.measure_store.drain_job = drain
        task = asyncio.create_task(self.sweep(job, exact_sub_mute=True))
        try:
            await asyncio.wait_for(draining.wait(), 2)
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertTrue(self.runtime.muted)
            self.assertEqual(events, [])
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(events, ["drained"])
        self.assertFalse(self.runtime.muted)
        self.assertTrue(task.cancelled())

    async def test_service_sweep_stages_proposal_and_prearms_via_owner(self):
        job = self.service_job()
        result = await self.sweep(job)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.hardware.transitions), 1)
        self.assertTrue(job["_auto_sub_last_config_ok"])
        self.assertTrue(job["_auto_sub_last_prearm_ok"])
        self.assertEqual(len(self.measure_store.starts), 1)
        child = self.measure_store.starts[0]
        self.assertEqual(len(child["expected_native_layout"]), 3)
        self.assertEqual(child["expected_native_output_mode"], "stereo-sub")
        self.assertEqual(child["expected_plan_fingerprint"],
                         result["stage_output_peaks"]["predicted"]["plan_fingerprint"])
        self.assertEqual(result["stage_output_peaks"]["predicted"]["model"],
                         "compiled-layout-v1")
        self.assertFalse(result["stage_output_peaks"]["relevant_mismatch"])

    async def test_repeated_candidate_reuses_staged_graph(self):
        job = self.service_job()
        first = await self.sweep(job)
        second = await self.sweep(job)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["status"], "completed")
        self.assertEqual(len(self.hardware.transitions), 1)
        self.assertEqual(len(self.measure_store.starts), 2)
        for child in self.measure_store.starts:
            self.assertIn("expected_plan_fingerprint", child)

    async def test_owner_prearm_mismatch_fails_sweep_without_audio(self):
        self.hardware.fail_after_reads = 1
        job = self.service_job()
        result = await self.sweep(job, delay_ms=2.0)
        self.assertEqual(result["status"], "pre_arm_failed")
        self.assertFalse(job["_auto_sub_last_prearm_ok"])
        self.assertEqual(self.measure_store.starts, [])

    async def test_revision_drift_reports_config_failed_without_legacy_fallback(self):
        drifted = self.service.load()
        drifted["modes"]["stereo-sub"]["processing"]["sub1"]["alignment_ms"] = 9.0
        self.service.commit(drifted, expected_revision=drifted["revision"])
        job = self.service_job()
        result = await self.sweep(job)
        self.assertEqual(result["status"], "config_failed")
        self.assertIn(job["id"], autosub_deps._AUTO_SUB_CANDIDATE_OWNERS)
        self.assertEqual(self.service.load()["revision"], self.start["revision"] + 1)
        self.assertEqual(self.measure_store.starts, [])

    async def test_service_main_reference_mutes_subs_via_context_mask(self):
        job = self.service_job()
        self.mute_next = self.context["sub_mute_mask"]
        result = await self.sweep(job, delay_ms=2.0, stage="main_reference",
                                  exact_sub_mute=True)
        self.assertEqual(result["status"], "completed")
        self.assertIn((True, self.context["sub_mute_mask"]), self.runtime.mute_calls)
        self.assertEqual(
            result["stage_output_peaks"]["predicted"]["dbfs"]["output_3"], -240.0)

    # NOTE (backend-v2 migration): test_legacy_job_keeps_orchestration_sync_and_unstaged_sweep
    # pinned the deleted legacy funnel path (persist/sync/prearm through the
    # mode file). The funnel now requires a service job; service coverage is
    # the surrounding sweep tests in this file.

class ScanKnobsTests(unittest.TestCase):
    def start_state(self, roles=("sub1",), mode="stereo-sub"):
        from audio.output_state import set_crossover, switch_mode
        state = set_crossover(default_output_state(), "stereo-sub", mode != "stereo-sub")
        state = set_mode_routing(state, "stereo-sub", "dev", ["main_l", "main_r", *roles]
                                 if mode == "stereo-sub" else
                                 [*(f"left_{way}" for way in ("low", "high")),
                                  *(f"right_{way}" for way in ("low", "high")), *roles])
        return switch_mode(state, "stereo-sub")

    def knobs(self, state, role_map, **overrides):
        args = dict(output_key="dev", channels=4, sub_role_map=role_map,
                    delay_ms=5.0, sub1_alignment_ms=None, sub2_alignment_ms=None,
                    active_subs=tuple(role_map), sub1_polarity=None, sub2_polarity=None,
                    crossover_hz=80, main_highpass_enabled=True,
                    original_level=-3.0, original_polarity="normal",
                    original_config_snapshot=None)
        args.update(overrides)
        return autosub_scan_knobs(state, **args)

    def test_single_sub_slot_uses_funnel_level_and_polarity(self):
        state = self.start_state()
        state["modes"]["stereo-sub"]["processing"]["sub1"].update(
            alignment_ms=2.0, level_db=-3.0, polarity="invert")
        knobs = self.knobs(state, {"sub1": "sub1"},
                           original_level=2.0, original_polarity="invert")
        self.assertEqual(knobs["sub_delays"], {"sub1": 5.0})
        self.assertEqual(knobs["sub_levels"], {"sub1": 2.0})
        self.assertEqual(knobs["sub_polarities"], {"sub1": "invert"})
        self.assertEqual(knobs["bass"], {"frequency_hz": 80, "main_highpass_enabled": True})

    def test_dual_mono_uses_snapshot_levels_and_minus_80(self):
        state = self.start_state(roles=("sub1", "sub2"))
        snapshot = {"subwoofers": {
            "sub1": {"level_db": 1.5, "alignment_ms": 1.0, "polarity": "invert"},
            "sub2": {"level_db": 2.5, "alignment_ms": 3.0, "polarity": "normal"},
        }}
        knobs = self.knobs(state, {"sub1": "sub1", "sub2": "sub2"},
                           original_config_snapshot=snapshot,
                           sub1_alignment_ms=7.0, sub2_alignment_ms=9.0,
                           active_subs=("sub1",), sub2_polarity="normal")
        self.assertEqual(knobs["sub_delays"], {"sub1": 7.0, "sub2": 9.0})
        self.assertEqual(knobs["sub_levels"], {"sub1": 1.5, "sub2": -80.0})
        self.assertEqual(knobs["sub_polarities"], {"sub1": "invert", "sub2": "normal"})

    def test_strict_polarity_words_and_clamped_delay(self):
        state = self.start_state()
        knobs = self.knobs(state, {"sub1": "sub1"}, delay_ms=100.0,
                           original_polarity="invert")
        self.assertEqual(knobs["sub_delays"], {"sub1": 40.0})
        self.assertEqual(knobs["sub_polarities"], {"sub1": "invert"})
        with self.assertRaises(ValueError):
            self.knobs(state, {"sub1": "sub1"}, original_polarity="INVERT")

    def test_unknown_slot_role_or_sub_rejected(self):
        state = self.start_state()
        with self.assertRaises(ValueError):
            self.knobs(state, {"sub1": "sub9"})
        with self.assertRaises(ValueError):
            self.knobs(state, {"sub1": "sub1"}, active_subs=("sub3",))
        with self.assertRaises(ValueError):
            self.knobs(state, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
