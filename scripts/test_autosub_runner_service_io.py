#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Every AutoSub candidate IO boundary runs through the owner for service jobs.

Legacy jobs keep the persisted-config path untouched. Service jobs (carrying
``output_state_context``) must never persist, sync or read back legacy mode
state anywhere in the three optimize runners:

* the pure adapters translate legacy runner shapes into complete role-keyed
  proposal knobs (scan args, explicit slot maps, legacy apply triplets),
* ``_auto_sub_apply_candidate`` stages through the owner for service jobs
  (False only for recoverable translation/render failures; revision drift
  and unrestorable rollback raise run-fatal),
* every direct runner persist+sync site stages the retained state instead,
  bare re-syncs are skipped (the graph is already staged), and the derived
  delay diagnostics are built explicitly instead of read from stale legacy
  persistence,
* inactive subs park at -80 dB through the same authoritative validation,
* full runners complete with every legacy setter patched to raise.

Scoring, polarity, gain and confirmation math are steered with the same
mock style as the existing final-path suites; the assertions target the IO
boundaries (staged transitions, zero legacy calls, restored end state, no
uncommitted revision drift).
"""

import asyncio
import copy
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import audio.samplerate as samplerate_module  # noqa: E402
from audio.output_service import OutputService, OutputServiceDeps  # noqa: E402
from audio.output_state import default_output_state, set_crossover, set_mode_routing, switch_mode
from audio.output_state_store import OutputStateStore, StateConflictError  # noqa: E402
from dsp.manager import DSPManager  # noqa: E402
from dsp.runtime import DSPRuntimeConfig, PlannedSyncTarget  # noqa: E402

import measurement.autosub.candidates as autosub_candidates  # noqa: E402
import measurement.autosub.jobs as autosub_jobs  # noqa: E402
from measurement.autosub import deps as autosub_deps  # noqa: E402
from measurement.autosub.candidate_session import AutoSubCandidateSession  # noqa: E402
from measurement.autosub.roles import (  # noqa: E402
    autosub_apply_knobs,
    autosub_explicit_knobs,
    autosub_scan_knobs,
)
from measurement.autosub.runners import (  # noqa: E402
    optimize as runner_21,
    optimize_22 as runner_22,
    optimize_22_stereo as runner_stereo,
)

RATE = 48000
PORTS = [f"playback_AUX{i}" for i in range(4)]
FREQS = [20.0, 25.0, 31.5, 40.0, 50.0, 63.0, 80.0, 100.0,
         125.0, 160.0, 200.0, 250.0, 315.0, 400.0, 500.0, 640.0]


def _points(value_db=0.0):
    return [[frequency, value_db] for frequency in FREQS]


def _dipped_points():
    points = _points()
    points[7][1] = -12.0
    return points


def _dip_by_shape(points, lo, hi):
    """15 dB where the crafted notch fixture is present, else 5 dB."""
    try:
        deep = any(float(db) == -12.0 for _, db in (points or []))
    except (TypeError, ValueError):
        deep = False
    return 15.0 if deep else 5.0


def _seed_state(service, roles):
    from audio.output_state import switch_mode
    state = switch_mode(set_mode_routing(default_output_state(), "stereo-sub", "dev",
                             ["main_l", "main_r", *roles]), "stereo-sub")
    return service.commit(state, expected_revision=0)


class ServiceHarness:
    """Real output service plus a real owner on a fake hardware boundary."""

    def __init__(self, testcase, roles, *, activate=True):
        directory = tempfile.TemporaryDirectory(prefix="runner-service-io-")
        testcase.addCleanup(directory.cleanup)
        self.manager = DSPManager(home=Path(directory.name) / "dsp")
        self.service = OutputService(OutputServiceDeps(
            store=OutputStateStore(Path(directory.name) / "output-state.json"),
            preset_loader=self.manager.preset_store.read,
            resolve_ir=lambda name: (_ for _ in ()).throw(AssertionError(name)),
            measurement_active=lambda: True))
        self.start = _seed_state(self.service, roles)
        self.transitions = []
        self.stage_calls = []
        self.reads = 0
        plan = self.service.compile_plan(self.start, output_key="dev", channels=4,
                                         sample_rate_hz=RATE)
        self.target = self.build_target(
            plan, fingerprint=self.service.fingerprint_plan(plan))
        self.start_fingerprint = self.target.config.plan_fingerprint
        real_stage = None
        owner = AutoSubCandidateSession(
            service=self.service, start_state=self.start,
            build_target=self.build_target, guarded_stage=self.guarded_stage,
            readback=self.readback, output_key="dev", channels=4)
        real_stage = owner.stage

        async def counting_stage(proposal):
            self.stage_calls.append(proposal)
            return await real_stage(proposal)

        owner.stage = counting_stage
        if activate:
            owner.activate(RATE)
        self.owner = owner

    def build_target(self, plan, *, fingerprint):
        rate = plan["sample_rate_hz"]
        layout = self.service.compile_layout(plan)
        config = DSPRuntimeConfig.from_plan(
            plan, layout=layout, output_key="dev", sample_rate_hz=rate,
            hardware_ports=PORTS, plan_fingerprint=fingerprint)
        text = self.manager.compile_engine_text(
            layout, preset_name=plan["global"]["preset"], sample_rate_hz=rate,
            extras_override=plan["global"]["extras"])
        return PlannedSyncTarget(config, text)

    async def readback(self):
        self.reads += 1
        return {"active": True, "output_gain_db": 0.0, "config": {
            "plan_fingerprint": self.target.config.plan_fingerprint,
            "sample_rate": RATE}}

    async def guarded_stage(self, new, *, previous, guard_db, apply_candidate,
                            apply_previous, before_ramp, before_rollback_ramp,
                            ramp_target_db, rollback_ramp_target_db):
        self.transitions.append(new)
        try:
            apply_candidate()
            self.target = new
            await before_ramp()
        except BaseException:
            apply_previous()
            self.target = previous
            await before_rollback_ramp()
            raise


def _legacy_21_snapshot():
    return {
        "mode": "subwoofer-2.1",
        "crossover_frequency_hz": 80,
        "main_highpass_enabled": True,
        "subwoofer": {
            "crossover_frequency_hz": 80,
            "main_highpass_enabled": True,
            "sub_alignment_ms": 0.0,
            "sub_level_db": 0.0,
            "sub_polarity": "normal",
        },
    }


def _legacy_22_snapshot(mode="subwoofer-2.2"):
    return {
        "mode": mode,
        "crossover_frequency_hz": 80,
        "main_highpass_enabled": True,
        "subwoofers": {
            "sub1": {"level_db": 0.0, "alignment_ms": 0.0, "polarity": "normal"},
            "sub2": {"level_db": 0.0, "alignment_ms": 0.0, "polarity": "normal"},
        },
    }


class ScanKnobsSnapshotTests(unittest.TestCase):
    """Funnel scan args mirror the legacy per-mode value sources.

    Single-sub candidates carry their level/polarity in the funnel args;
    dual-sub candidates carry them in the snapshot (with -80 dB for
    inactive slots) — exactly like the legacy candidate builders the
    adapter replaces.
    """

    def start_state(self):
        state = switch_mode(set_mode_routing(default_output_state(), "stereo-sub", "dev", ["main_l", "main_r", "sub1", "sub2"]), "stereo-sub")
        processing = state["modes"]["stereo-sub"]["processing"]
        processing["sub1"].update(alignment_ms=1.0, level_db=-2.0, polarity="normal")
        processing["sub2"].update(alignment_ms=3.0, level_db=-4.0, polarity="invert")
        return state

    def snapshot(self):
        return {"subwoofers": {
            "sub1": {"level_db": 1.5, "alignment_ms": 1.0, "polarity": "invert"},
            "sub2": {"level_db": 2.5, "alignment_ms": 3.0, "polarity": "normal"},
        }}

    def knobs(self, state, role_map, snapshot=None, **overrides):
        args = dict(output_key="dev", channels=4, sub_role_map=role_map,
                    delay_ms=5.0, sub1_alignment_ms=None, sub2_alignment_ms=None,
                    active_subs=tuple(role_map), sub1_polarity=None, sub2_polarity=None,
                    crossover_hz=80, main_highpass_enabled=True,
                    original_level=-3.0, original_polarity="normal",
                    original_config_snapshot=snapshot)
        args.update(overrides)
        return autosub_scan_knobs(state, **args)

    def test_single_sub_uses_funnel_level_and_polarity(self):
        state = switch_mode(set_mode_routing(default_output_state(), "stereo-sub", "dev", ["main_l", "main_r", "sub1"]), "stereo-sub")
        knobs = self.knobs(state, {"sub1": "sub1"}, original_level=2.0,
                           original_polarity="invert")
        self.assertEqual(knobs["sub_delays"], {"sub1": 5.0})
        self.assertEqual(knobs["sub_levels"], {"sub1": 2.0})
        self.assertEqual(knobs["sub_polarities"], {"sub1": "invert"})

    def test_dual_sub_uses_snapshot_levels_and_polarities(self):
        state = self.start_state()
        knobs = self.knobs(state, {"sub1": "sub1", "sub2": "sub2"},
                           snapshot=self.snapshot(),
                           sub1_alignment_ms=7.0, sub2_alignment_ms=9.0,
                           active_subs=("sub1", "sub2"),
                           original_level=99.0, original_polarity="invert")
        self.assertEqual(knobs["sub_delays"], {"sub1": 7.0, "sub2": 9.0})
        self.assertEqual(knobs["sub_levels"], {"sub1": 1.5, "sub2": 2.5})
        self.assertEqual(knobs["sub_polarities"], {"sub1": "invert", "sub2": "normal"})

    def test_dual_sub_parks_inactive_slot_at_minus_80(self):
        state = self.start_state()
        knobs = self.knobs(state, {"sub1": "sub1", "sub2": "sub2"},
                           snapshot=self.snapshot(), active_subs=("sub2",))
        self.assertEqual(knobs["sub_levels"], {"sub1": -80.0, "sub2": 2.5})

    def test_explicit_dual_polarity_overrides_snapshot(self):
        state = self.start_state()
        knobs = self.knobs(state, {"sub1": "sub1", "sub2": "sub2"},
                           snapshot=self.snapshot(), active_subs=("sub1", "sub2"),
                           sub1_polarity="normal", sub2_polarity="invert")
        self.assertEqual(knobs["sub_polarities"], {"sub1": "normal", "sub2": "invert"})


class ExplicitKnobsTests(unittest.TestCase):
    def start_state(self):
        return switch_mode(set_mode_routing(default_output_state(), "stereo-sub", "dev", ["main_l", "main_r", "sub1", "sub2"]), "stereo-sub")

    def knobs(self, role_map, delays, levels, polarities, **overrides):
        args = dict(output_key="dev", channels=4, sub_role_map=role_map,
                    slot_delays=delays, slot_levels=levels, slot_polarities=polarities,
                    crossover_hz=80, main_highpass_enabled=True)
        args.update(overrides)
        return autosub_explicit_knobs(self.start_state(), **args)

    def test_explicit_maps_and_clamps(self):
        knobs = self.knobs({"sub1": "sub2", "sub2": "sub1"},
                           {"sub1": 100.0, "sub2": -50.0},
                           {"sub1": -80.0, "sub2": 3.5},
                           {"sub1": "invert", "sub2": "normal"})
        self.assertEqual(knobs["sub_delays"], {"sub2": 40.0, "sub1": -40.0})
        self.assertEqual(knobs["sub_levels"], {"sub2": -80.0, "sub1": 3.5})
        self.assertEqual(knobs["sub_polarities"], {"sub2": "invert", "sub1": "normal"})
        self.assertEqual(knobs["bass"], {"frequency_hz": 80, "main_highpass_enabled": True})

    def test_rejects_unknown_words_and_bad_numbers(self):
        good = ({"sub1": 1.0}, {"sub1": 0.0}, {"sub1": "normal"})
        with self.assertRaises(ValueError):
            self.knobs({"sub1": "sub1"}, *good[:2], {"sub1": "INVERT"})
        with self.assertRaises(ValueError):
            self.knobs({"sub1": "sub1"}, {"sub1": float("nan")}, *good[1:])
        with self.assertRaises(ValueError):
            self.knobs({"sub1": "sub1"}, *good[:1], {"sub1": float("inf")}, good[2])

    def test_rejects_coverage_mismatch(self):
        delays, levels, polarities = {"sub1": 1.0}, {"sub1": 0.0}, {"sub1": "normal"}
        with self.assertRaises(ValueError):
            self.knobs({"sub1": "sub9"}, delays, levels, polarities)
        with self.assertRaises(ValueError):
            self.knobs({"sub1": "sub1", "sub2": "sub2"}, delays, levels, polarities)
        with self.assertRaises(ValueError):
            self.knobs({"sub1": "sub1"}, {}, levels, polarities)
        with self.assertRaises(ValueError):
            self.knobs({}, delays, levels, polarities)


class LegacyTranslatorTests(unittest.TestCase):
    def start_state(self, roles=("sub1", "sub2")):
        from audio.output_state import switch_mode
        return switch_mode(set_mode_routing(default_output_state(), "stereo-sub", "dev",
                                ["main_l", "main_r", *roles]), "stereo-sub")

    def test_single_shape_maps_retained_triplet(self):
        knobs = autosub_apply_knobs(
            self.start_state(("sub1",)), output_key="dev", channels=4,
            sub_role_map={"sub1": "sub1"},
            global_config={"crossover_frequency_hz": 90, "sub_alignment_ms": -3.12,
                           "sub_level_db": 2.0, "sub_polarity": "invert",
                           "main_highpass_enabled": False},
            subwoofers_config=None)
        self.assertEqual(knobs["sub_delays"], {"sub1": -3.12})
        self.assertEqual(knobs["sub_levels"], {"sub1": 2.0})
        self.assertEqual(knobs["sub_polarities"], {"sub1": "invert"})
        self.assertEqual(knobs["bass"], {"frequency_hz": 90, "main_highpass_enabled": False})

    def test_dual_shape_maps_both_slots_and_minus_80(self):
        knobs = autosub_apply_knobs(
            self.start_state(), output_key="dev", channels=4,
            sub_role_map={"sub1": "sub1", "sub2": "sub2"},
            global_config={"crossover_frequency_hz": 80, "main_highpass_enabled": True},
            subwoofers_config={
                "sub1": {"level_db": 1.0, "alignment_ms": -3.12, "polarity": "invert"},
                "sub2": {"level_db": -80.0, "alignment_ms": 0.5, "polarity": "normal"},
            })
        self.assertEqual(knobs["sub_delays"], {"sub1": -3.12, "sub2": 0.5})
        self.assertEqual(knobs["sub_levels"], {"sub1": 1.0, "sub2": -80.0})
        self.assertEqual(knobs["sub_polarities"], {"sub1": "invert", "sub2": "normal"})

    def test_rejects_malformed_legacy_shapes(self):
        state = self.start_state()
        with self.assertRaises(ValueError):
            autosub_apply_knobs(state, output_key="dev", channels=4,
                                sub_role_map={"sub1": "sub1"},
                                global_config={"crossover_frequency_hz": 80},
                                subwoofers_config=None)
        with self.assertRaises(ValueError):
            autosub_apply_knobs(state, output_key="dev", channels=4,
                                sub_role_map={"sub1": "sub1", "sub2": "sub2"},
                                global_config={"crossover_frequency_hz": 80,
                                               "main_highpass_enabled": True},
                                subwoofers_config={"sub1": {"level_db": 0.0,
                                                            "alignment_ms": 0.0,
                                                            "polarity": "normal"}})
        with self.assertRaises(ValueError):
            autosub_apply_knobs(state, output_key="dev", channels=4,
                                sub_role_map={"sub1": "sub1"},
                                global_config={"crossover_frequency_hz": 80,
                                               "main_highpass_enabled": True,
                                               "sub_alignment_ms": 0.0,
                                               "sub_level_db": 0.0,
                                               "sub_polarity": "sideways"},
                                subwoofers_config=None)


class ApplyCandidateOwnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.harness = ServiceHarness(self, ("sub1",))
        self.service = self.harness.service
        self.owner = self.harness.owner
        autosub_deps.configure_dependencies(autosub_deps.AutoSubDependencies(
            get_dsp_runtime=lambda: None,
            get_measurement_store=lambda: None,
            get_measurement_session=lambda: None,
            get_dsp_manager=lambda: None,
            get_output_service=lambda: self.service,
            create_candidate_session=lambda **kwargs: self.fail("unexpected owner creation")))
        self.job = {"id": "apply-job", "status": "running", "output_state_context": {
            "mode": "stereo", "revision": self.harness.start["revision"],
            "output_key": "dev", "channels": 4, "optimizer_path": "single-sub",
            "sub_role_map": {"sub1": "sub1"}, "sub_mute_mask": 4}}
        autosub_deps.register_candidate_owner("apply-job", self.owner)
        self.addCleanup(autosub_deps.drop_candidate_owner, "apply-job")
        self.addCleanup(setattr, autosub_deps, "_autosub_deps", None)
        # Legacy persist entry points are deleted; the service apply stages
        # through the owner with no legacy fallback to guard.

    async def test_service_apply_stages_without_persisting(self):
        before = self.service.load()["revision"]
        applied = await autosub_candidates._auto_sub_apply_candidate(
            global_config={"crossover_frequency_hz": 80, "sub_alignment_ms": 4.5,
                           "sub_level_db": 1.5, "sub_polarity": "invert",
                           "main_highpass_enabled": True},
            subwoofers_config=None, job=self.job)
        self.assertTrue(applied)
        self.assertEqual(len(self.harness.transitions), 1)
        self.assertEqual(len(self.harness.stage_calls), 1)
        self.assertEqual(self.service.load()["revision"], before)

    async def test_translation_failure_returns_false_without_staging(self):
        applied = await autosub_candidates._auto_sub_apply_candidate(
            global_config={"crossover_frequency_hz": 80},
            subwoofers_config=None, job=self.job)
        self.assertFalse(applied)
        self.assertEqual(self.harness.transitions, [])
        self.assertEqual(self.harness.stage_calls, [])

    async def test_revision_drift_raises_run_fatal(self):
        drifted = self.service.load()
        drifted["modes"]["stereo-sub"]["processing"]["sub1"]["alignment_ms"] = 9.0
        self.service.commit(drifted, expected_revision=drifted["revision"])
        with self.assertRaises(StateConflictError):
            await autosub_candidates._auto_sub_apply_candidate(
                global_config={"crossover_frequency_hz": 80, "sub_alignment_ms": 4.5,
                               "sub_level_db": 1.5, "sub_polarity": "invert",
                               "main_highpass_enabled": True},
                subwoofers_config=None, job=self.job)
        self.assertEqual(self.harness.transitions, [])


def _explode(message):
    def _raise(*args, **kwargs):
        raise AssertionError(message)
    return _raise


def _runner_common_patches(stack, runner, runtime):
    # Legacy persistence entry points are deleted (backend-v2 migration):
    # there is nothing left to patch-raising. The service path stages
    # exclusively through the owner; this helper keeps the shared
    # measurement-rate pin for all three runner suites.
    stack.enter_context(patch("measurement.session._resolve_measurement_start_sample_rate", return_value=RATE))


class RunnerRuntime:
    """Runner-side fake whose sync proves it is never called for service jobs."""

    async def sync(self, overview):
        raise AssertionError("legacy runtime sync in service run")

    def snapshot(self):
        return {}


class RunnerSession:
    def __init__(self):
        self.active_auto_sub_job_id = None
        self.measurement_rate = RATE
        self.unregistered = []

    async def register_auto_sub(self, job_id, entry_epoch=None):
        assert entry_epoch == 7
        self.active_auto_sub_job_id = job_id
        return 1

    async def unregister_auto_sub(self, job_id):
        self.unregistered.append(job_id)
        if self.active_auto_sub_job_id == job_id:
            self.active_auto_sub_job_id = None


class RunnerServiceIOTestBase(unittest.IsolatedAsyncioTestCase):
    runner = None
    roles = ()
    context = {}

    def setUp(self):
        self.harness = ServiceHarness(self, self.roles, activate=False)
        self.service = self.harness.service
        self.owner = self.harness.owner
        self.session = RunnerSession()
        self.runtime = RunnerRuntime()
        autosub_deps.configure_dependencies(autosub_deps.AutoSubDependencies(
            get_dsp_runtime=lambda: self.runtime,
            get_measurement_store=lambda: None,
            get_measurement_session=lambda: self.session,
            get_dsp_manager=lambda: None,
            get_output_service=lambda: self.service,
            create_candidate_session=lambda **kwargs: self.fail("unexpected owner creation")))
        self.job_id = f"service-io-{id(self) % 100000}"
        self.job = {"id": self.job_id, "status": "preparing", "cancel_requested": False,
                    "mode": "legacy-label", "output_state_context": dict(
                        self.context, revision=self.harness.start["revision"]),
                    "target_curve": {"label": "Neutral", "points": _points()},
                    "main_target_anchor": {"status": "ready", "target_vertical_offset_db": 0.0},
                    "playback_gain": {"linear": 1.0},
                    "reference_channels": {"left": "", "right": ""},
                    "current_sweep_id": ""}
        self.runner._AUTO_SUB_JOBS[self.job_id] = self.job
        autosub_deps.register_candidate_owner(self.job_id, self.owner)
        self.addCleanup(autosub_deps.drop_candidate_owner, self.job_id)
        self.addCleanup(autosub_deps._AUTO_SUB_JOBS.pop, self.job_id, None)
        self.addCleanup(setattr, autosub_deps, "_autosub_deps", None)
        self.lock = asyncio.Lock()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        _runner_common_patches(self.stack, self.runner, self.runtime)
        self.stack.enter_context(patch.object(self.runner, "_measurement_session", return_value=self.session))
        self.stack.enter_context(patch.object(autosub_jobs, "_measurement_session", return_value=self.session))
        self.stack.enter_context(patch.object(self.runner, "_capture_auto_sub_main_references", new=AsyncMock()))
        self.stack.enter_context(patch.object(
            self.runner, "_auto_sub_gate_candidate_rows",
            side_effect=lambda rows, *_a, **_k: (rows, [])))
        self.stack.enter_context(patch.object(self.runner, "_auto_sub_lock", asyncio.Lock()))
        self.stack.enter_context(patch.object(autosub_jobs, "_auto_sub_lock", self.lock))

    async def run_runner(self, coro):
        await self.lock.acquire()
        try:
            await coro
        finally:
            if self.lock.locked():
                self.lock.release()
        for task in list(autosub_deps._AUTO_SUB_CLEANUP_TASKS):
            task.cancel()
        await asyncio.gather(*autosub_deps._AUTO_SUB_CLEANUP_TASKS, return_exceptions=True)
        autosub_deps._AUTO_SUB_CLEANUP_TASKS.clear()

    def assert_service_end_state(self):
        self.assertNotIn(self.job_id, autosub_deps._AUTO_SUB_CANDIDATE_OWNERS)
        self.assertEqual(self.session.unregistered, [self.job_id])
        self.assertIsNone(self.session.active_auto_sub_job_id)
        # Slice F: the final retained state is committed, not rolled back.
        # No staged change (equal-to-start) keeps the frozen revision; any
        # adopted candidate bumps exactly one revision. Either way the
        # committed document must compile to the graph the fake hardware
        # actually shows (the retired stager is never restored through).
        final_state = self.service.load()
        self.assertIn(final_state["revision"], (
            self.harness.start["revision"], self.harness.start["revision"] + 1))
        committed_plan = self.service.compile_plan(
            final_state, output_key="dev", channels=4, sample_rate_hz=RATE)
        self.assertEqual(self.service.fingerprint_plan(committed_plan),
                         self.harness.target.config.plan_fingerprint)

    def canned_combined(self, points=None, **overrides):
        pts = copy.deepcopy(points if points is not None else _points())
        result = {
            "delay_ms": 0.0, "name": "0.00", "status": "completed", "scan": "coarse",
            "points": copy.deepcopy(pts), "points_left": copy.deepcopy(pts),
            "points_right": copy.deepcopy(pts),
            "calibrated_points_left": copy.deepcopy(pts),
            "calibrated_points_right": copy.deepcopy(pts),
            "normalized_by_db_left": 0.0, "normalized_by_db_right": 0.0,
            "stage_output_peaks": {"stage": "coarse"},
        }
        result.update(overrides)
        return result

    def combined_measure(self, per_stage=None):
        per_stage = per_stage or {}

        async def measure(**kwargs):
            stage = str(kwargs.get("stage", ""))
            result = self.canned_combined(
                points=per_stage.get(stage),
                delay_ms=float(kwargs.get("delay_ms", 0.0)),
                sub1_alignment_ms=kwargs.get("sub1_alignment_ms", kwargs.get("delay_ms", 0.0)),
                sub2_alignment_ms=kwargs.get("sub2_alignment_ms", 0.0))
            job = kwargs["job"]
            job.setdefault("_sweep_timings", []).extend([
                {"channel": "left", "stage": stage, "durations": {"total_ms": 1.0}},
                {"channel": "right", "stage": stage, "durations": {"total_ms": 1.0}},
            ])
            return result

        return measure


class Runner21ServiceIOTests(RunnerServiceIOTestBase):
    runner = runner_21
    roles = ("sub1",)
    context = {"mode": "stereo", "output_key": "dev", "channels": 4,
               "optimizer_path": "single-sub", "sub_role_map": {"sub1": "sub1"},
               "sub_mute_mask": 4}

    def steering(self, stack, *, gain_deltas, gain_verdicts, correction,
                 dip=None, veto=None, recheck=None):
        def score(rows, **_kwargs):
            results = []
            for row in rows:
                delay = round(float(row.get("delay_ms", 0.0)), 2)
                row_score = 0.677 if delay == -3.12 else (0.60 if delay == 0.0 else 0.2)
                results.append({**row, "score": row_score, "final_score": row_score,
                                "score_pct": round(row_score * 100.0, 1),
                                "xo_score": row_score, "timing_band_score": row_score,
                                "low_guard_loss_db": 0.0, "low_guard_penalty": 0.0,
                                "score_L": row_score, "score_L_pct": round(row_score * 100.0, 1),
                                "score_R": row_score, "score_R_pct": round(row_score * 100.0, 1)})
            results.sort(key=lambda item: item["score"], reverse=True)
            return {"winner": results[0],
                    "runner_up": results[1] if len(results) > 1 else None,
                    "results": results, "confidence": "clear",
                    "scored_candidates": rows}

        stack.enter_context(patch.object(
            self.runner, "_score_auto_sub_combined_candidates", side_effect=score))
        stack.enter_context(patch.object(
            self.runner, "_auto_sub_fine_trigger_reasons", return_value=[]))
        stack.enter_context(patch.object(
            self.runner, "_auto_sub_select_accepted_winner",
            return_value={"accepted_winner": {"delay_ms": -3.12, "score_pct": 67.7},
                          "fine_accepted": False, "reject_reason": "test",
                          "incumbent_score": 60.0}))
        stack.enter_context(patch.object(
            self.runner, "_auto_sub_polarity_decision",
            return_value={"accepted": False, "score_gain": -0.1,
                          "min_score_gain": 0.03, "reason": "test-incumbent-protected"}))
        stack.enter_context(patch.object(
            self.runner, "_calculate_auto_sub_gain",
            side_effect=lambda **_kwargs: {
                "available": True, "gain_calculated": True, "confidence": "high",
                "recommendation": {"raw_delta_db": 0.0}, "channels": {}}))
        stack.enter_context(patch.object(
            self.runner, "_auto_sub_gain_deltas", return_value=dict(gain_deltas)))
        stack.enter_context(patch.object(
            self.runner, "_auto_sub_gain_verdict", side_effect=list(gain_verdicts)))
        stack.enter_context(patch.object(
            self.runner, "_auto_sub_gain_response_correction", return_value=dict(correction)))
        if dip is not None:
            stack.enter_context(patch.object(
                self.runner, "_auto_sub_local_dip_db", side_effect=dip))
        if veto is not None:
            stack.enter_context(patch.object(
                self.runner, "_auto_sub_dip_guard_should_veto", return_value=tuple(veto)))
        if recheck is not None:
            stack.enter_context(patch.object(
                self.runner, "_auto_sub_local_dip_recheck_decision", return_value=dict(recheck)))

    def runner_args(self):
        return dict(job_id=self.job_id, input_id="mic", channel="left",
                    mic_input_channel="1", reference_input_channel="",
                    calibration_ref="", calibration_filename=None, calibration_bytes=None,
                    scan_delays=[0.0, -3.12], fc=80, current_alignment=0.0,
                    original_polarity="normal", original_level=0.0,
                    original_highpass=True,
                    original_config_snapshot=_legacy_21_snapshot(), entry_epoch=7)

    async def test_happy_path_stages_winner_gain_and_correction(self):
        stack = ExitStack()
        with stack:
            stack.enter_context(patch.object(
                self.runner, "_measure_auto_sub_combined_candidate",
                side_effect=self.combined_measure()))
            self.steering(
                stack, gain_deltas={"left": 2.0, "right": 2.0},
                gain_verdicts=[{"accepted": True, "reason": "test", "channels": {}},
                               {"accepted": True, "reason": "test", "channels": {}}],
                correction={"available": True, "reason": "test",
                            "deltas_db": {"left": 1.0}, "channels": {}},
                dip=_dip_by_shape,
                veto=(False, {"failed_sides": []}))
            await self.run_runner(self.runner._run_auto_sub_optimize(**self.runner_args()))
        self.assertEqual(self.job["status"], "completed", self.job.get("error"))
        self.assertEqual(self.job["result"]["applied_alignment_ms"], -3.12)
        self.assertEqual(self.job["auto_gain"]["final_level_db"], 3.0)
        # Winner + gain + correction staged; the cleanup restore is gone
        # because the final state was committed (never restored through).
        self.assertEqual(len(self.harness.transitions), 3)
        self.assertEqual(len(self.harness.stage_calls), 4)
        self.assert_service_end_state()

    async def test_gain_reject_reverts_through_owner(self):
        stack = ExitStack()
        with stack:
            stack.enter_context(patch.object(
                self.runner, "_measure_auto_sub_combined_candidate",
                side_effect=self.combined_measure()))
            self.steering(
                stack, gain_deltas={"left": 2.0, "right": 2.0},
                gain_verdicts=[{"accepted": False, "reason": "test", "channels": {}}],
                correction={"available": False, "reason": "test", "channels": {},
                            "deltas_db": {}},
                dip=_dip_by_shape,
                veto=(False, {"failed_sides": []}))
            await self.run_runner(self.runner._run_auto_sub_optimize(**self.runner_args()))
        self.assertEqual(self.job["status"], "completed", self.job.get("error"))
        self.assertTrue(self.job["auto_gain"]["reverted"])
        self.assertEqual(len(self.harness.transitions), 3)
        self.assert_service_end_state()

    async def test_correction_reject_restores_step1_through_owner(self):
        stack = ExitStack()
        with stack:
            stack.enter_context(patch.object(
                self.runner, "_measure_auto_sub_combined_candidate",
                side_effect=self.combined_measure()))
            self.steering(
                stack, gain_deltas={"left": 2.0, "right": 2.0},
                gain_verdicts=[{"accepted": True, "reason": "test", "channels": {}},
                               {"accepted": False, "reason": "test", "channels": {}}],
                correction={"available": True, "reason": "test",
                            "deltas_db": {"left": 1.0}, "channels": {}},
                dip=_dip_by_shape,
                veto=(False, {"failed_sides": []}))
            await self.run_runner(self.runner._run_auto_sub_optimize(**self.runner_args()))
        self.assertEqual(self.job["status"], "completed", self.job.get("error"))
        self.assertEqual(self.job["auto_gain"]["final_level_db"], 2.0)
        self.assertEqual(len(self.harness.transitions), 4)
        self.assert_service_end_state()

    async def test_veto_final_kept_recommits_through_owner(self):
        dipped = _dipped_points()
        stack = ExitStack()
        with stack:
            stack.enter_context(patch.object(
                self.runner, "_measure_auto_sub_combined_candidate",
                side_effect=self.combined_measure(per_stage={"gain_after": dipped})))
            self.steering(
                stack, gain_deltas={"left": 0.0, "right": 0.0},
                gain_verdicts=[{"accepted": True, "reason": "test", "channels": {}}],
                correction={"available": False, "reason": "test", "channels": {},
                            "deltas_db": {}},
                dip=_dip_by_shape,
                veto=(True, {"failed_sides": ["left"]}),
                recheck={"outcome": "final_kept", "failed_sides": ["left"],
                         "confirmed_failed_sides": [], "evidence_available": True,
                         "incumbent_evidence_available": True, "incumbent_passed": False})
            await self.run_runner(self.runner._run_auto_sub_optimize(**self.runner_args()))
        self.assertEqual(self.job["status"], "completed", self.job.get("error"))
        self.assertEqual(self.job["confirmation_gate"]["action"], "final_kept")
        self.assertEqual(len(self.harness.stage_calls), 3)
        self.assert_service_end_state()

    async def test_veto_incumbent_kept_reverts_through_owner(self):
        dipped = _dipped_points()
        stack = ExitStack()
        with stack:
            stack.enter_context(patch.object(
                self.runner, "_measure_auto_sub_combined_candidate",
                side_effect=self.combined_measure(per_stage={"gain_after": dipped})))
            self.steering(
                stack, gain_deltas={"left": 0.0, "right": 0.0},
                gain_verdicts=[{"accepted": True, "reason": "test", "channels": {}}],
                correction={"available": False, "reason": "test", "channels": {},
                            "deltas_db": {}},
                dip=_dip_by_shape,
                veto=(True, {"failed_sides": ["left"]}),
                recheck={"outcome": "incumbent_kept", "failed_sides": ["left"],
                         "confirmed_failed_sides": ["left"], "evidence_available": True,
                         "incumbent_evidence_available": True, "incumbent_passed": True})
            await self.run_runner(self.runner._run_auto_sub_optimize(**self.runner_args()))
        self.assertEqual(self.job["status"], "completed", self.job.get("error"))
        self.assertEqual(
            self.job["confirmation_gate"]["action"], "alignment_reverted_balance_kept")
        self.assertEqual(len(self.harness.stage_calls), 3)
        self.assert_service_end_state()

    async def test_service_path_has_no_legacy_persistence_surface(self):
        """Task-1 contract, post-deletion form: the legacy writers are gone.

        The full service run (happy path below) proves the behavior; this
        test pins the structural fact the migration requires — no legacy
        persist/load/overview entry point remains importable on the
        service-path modules, so no runner can reach it.
        """
        import measurement.autosub.measurement as funnel_measurement
        from audio import output_routing as routing_module
        for module, names in (
            (autosub_candidates, ("set_audio_output_mode",
                                  "get_audio_output_overview")),
            (funnel_measurement, ("set_audio_output_mode",)),
            (samplerate_module, ("set_audio_output_mode",
                                 "persist_audio_output_mode",
                                 "_load_audio_output_mode")),
            (routing_module, ("save_assignments",)),
        ):
            for name in names:
                self.assertFalse(hasattr(module, name), f"{module.__name__}.{name}")
        stack = ExitStack()
        with stack:
            stack.enter_context(patch.object(
                self.runner, "_measure_auto_sub_combined_candidate",
                side_effect=self.combined_measure()))
            self.steering(
                stack, gain_deltas={"left": 2.0, "right": 2.0},
                gain_verdicts=[{"accepted": True, "reason": "test", "channels": {}},
                               {"accepted": True, "reason": "test", "channels": {}}],
                correction={"available": True, "reason": "test",
                            "deltas_db": {"left": 1.0}, "channels": {}},
                dip=_dip_by_shape,
                veto=(False, {"failed_sides": []}))
            await self.run_runner(self.runner._run_auto_sub_optimize(**self.runner_args()))
        self.assertEqual(self.job["status"], "completed", self.job.get("error"))
        self.assertGreater(len(self.harness.transitions), 0)
        self.assert_service_end_state()


class Runner22ServiceIOTests(RunnerServiceIOTestBase):
    runner = runner_22
    roles = ("sub1", "sub2")
    context = {"mode": "stereo", "output_key": "dev", "channels": 4,
               "optimizer_path": "dual-sub",
               "sub_role_map": {"sub1": "sub1", "sub2": "sub2"},
               "sub_mute_mask": 12}

    def steering(self, stack, *, gain_verdicts, correction_available,
                 correction_delta=0.0, dip=None):
        def score_combined(rows, **_kwargs):
            results = []
            for row in rows:
                delay = round(float(row.get("delay_ms", 0.0)), 2)
                row_score = 0.65 if delay in (-3.12, -0.58) else (
                    0.60 if delay == 0.0 else 0.2)
                results.append({**row, "score": row_score, "final_score": row_score,
                                "score_pct": round(row_score * 100.0, 1),
                                "xo_score": row_score, "timing_band_score": row_score,
                                "low_guard_loss_db": 0.0, "low_guard_penalty": 0.0,
                                "score_L": row_score, "score_L_pct": round(row_score * 100.0, 1),
                                "score_R": row_score, "score_R_pct": round(row_score * 100.0, 1)})
            results.sort(key=lambda item: item["score"], reverse=True)
            return {"winner": results[0],
                    "runner_up": results[1] if len(results) > 1 else None,
                    "results": results, "confidence": "clear",
                    "scored_candidates": rows}

        def score_matrix(rows, **_kwargs):
            results = []
            for row in rows:
                key = (round(float(row.get("sub1_alignment_ms", 0.0)), 2),
                       round(float(row.get("sub2_alignment_ms", 0.0)), 2))
                row_score = 0.6349 if key == (-3.12, -0.58) else (
                    0.3355 if key == (0.0, 0.0) else 0.2)
                results.append({**row, "score": row_score, "final_score": row_score,
                                "score_pct": round(row_score * 100.0, 1),
                                "xo_score": row_score, "timing_band_score": row_score,
                                "low_guard_loss_db": 0.0, "low_guard_penalty": 0.0,
                                "score_L": row_score, "score_L_pct": round(row_score * 100.0, 1),
                                "score_R": row_score, "score_R_pct": round(row_score * 100.0, 1)})
            results.sort(key=lambda item: item["score"], reverse=True)
            incumbent = next((item for item in results if (
                round(float(item.get("sub1_alignment_ms", 0.0)), 2),
                round(float(item.get("sub2_alignment_ms", 0.0)), 2)) == (0.0, 0.0)), None)
            return {"winner": results[0],
                    "runner_up": results[1] if len(results) > 1 else None,
                    "results": results, "confidence": "clear",
                    "matrix_winner": results[0], "incumbent_winner": incumbent,
                    "incumbent_score": 0.3355, "accepted_winner": results[0],
                    "incumbent_accepted": False, "reject_reason": "matrix_better"}

        stack.enter_context(patch.object(
            self.runner, "_score_auto_sub_combined_candidates", side_effect=score_combined))
        stack.enter_context(patch.object(
            self.runner, "_score_auto_sub_matrix_candidates", side_effect=score_matrix))
        stack.enter_context(patch.object(
            self.runner, "_auto_sub_polarity_decision",
            return_value={"accepted": False, "score_gain": -0.1,
                          "min_score_gain": 0.08, "reason": "test-incumbent-best"}))
        stack.enter_context(patch.object(
            self.runner, "_calculate_auto_sub_gain",
            side_effect=lambda **_kwargs: {
                "available": True, "gain_calculated": True, "confidence": "high",
                "recommendation": {"raw_delta_db": 0.0}, "channels": {}}))
        stack.enter_context(patch.object(
            self.runner, "_auto_sub_gain_deltas",
            return_value={"left": 2.0, "right": 2.0}))
        stack.enter_context(patch.object(
            self.runner, "_auto_sub_gain_verdict", side_effect=list(gain_verdicts)))
        stack.enter_context(patch.object(
            self.runner, "_auto_sub_gain_response_correction",
            return_value={"available": correction_available, "reason": "test",
                          "deltas_db": {"left": correction_delta},
                          "channels": {}}))
        if dip is not None:
            stack.enter_context(patch.object(
                self.runner, "_auto_sub_local_dip_db", side_effect=dip))

    def runner_args(self):
        return dict(job_id=self.job_id, input_id="mic",
                    mic_input_channel="1", reference_input_channel="",
                    calibration_ref="", calibration_filename=None, calibration_bytes=None,
                    sub1_scan_delays=[0.0, -3.12], sub2_scan_delays=[0.0, -0.58],
                    fc=80, original_config_snapshot=_legacy_22_snapshot(),
                    fine_step_ms=0.39, entry_epoch=7)

    async def test_happy_path_reports_explicit_derived_delays(self):
        stack = ExitStack()
        with stack:
            stack.enter_context(patch.object(
                self.runner, "_measure_auto_sub_combined_candidate",
                side_effect=self.combined_measure()))
            self.steering(
                stack,
                gain_verdicts=[{"accepted": True, "reason": "test", "channels": {}},
                               {"accepted": True, "reason": "test", "channels": {}}],
                correction_available=True, correction_delta=1.0,
                dip=_dip_by_shape)
            await self.run_runner(self.runner._run_auto_sub_22_optimize(**self.runner_args()))
        self.assertEqual(self.job["status"], "completed", self.job.get("error"))
        result = self.job["result"]
        self.assertEqual(result["applied_sub1_alignment_ms"], -3.12)
        self.assertEqual(result["applied_sub2_alignment_ms"], -0.58)
        self.assertEqual(result["derived_main_delay_ms"], 3.12)
        self.assertEqual(result["derived_sub1_delay_ms"], 0.0)
        self.assertEqual(result["derived_sub2_delay_ms"], 2.54)
        self.assertGreater(len(self.harness.transitions), 0)
        self.assert_service_end_state()

    async def test_gain_reject_reverts_through_owner(self):
        stack = ExitStack()
        with stack:
            stack.enter_context(patch.object(
                self.runner, "_measure_auto_sub_combined_candidate",
                side_effect=self.combined_measure()))
            self.steering(
                stack,
                gain_verdicts=[{"accepted": False, "reason": "test", "channels": {}}],
                correction_available=False,
                dip=_dip_by_shape)
            await self.run_runner(self.runner._run_auto_sub_22_optimize(**self.runner_args()))
        self.assertEqual(self.job["status"], "completed", self.job.get("error"))
        self.assertGreater(len(self.harness.transitions), 0)
        self.assert_service_end_state()

    async def test_correction_reject_restores_step1_through_owner(self):
        stack = ExitStack()
        with stack:
            stack.enter_context(patch.object(
                self.runner, "_measure_auto_sub_combined_candidate",
                side_effect=self.combined_measure()))
            self.steering(
                stack,
                gain_verdicts=[{"accepted": True, "reason": "test", "channels": {}},
                               {"accepted": False, "reason": "test", "channels": {}}],
                correction_available=True, correction_delta=1.0,
                dip=_dip_by_shape)
            await self.run_runner(self.runner._run_auto_sub_22_optimize(**self.runner_args()))
        self.assertEqual(self.job["status"], "completed", self.job.get("error"))
        self.assertGreater(len(self.harness.transitions), 0)
        self.assert_service_end_state()

    async def test_veto_reverts_pair_through_owner(self):
        dipped = _dipped_points()
        stack = ExitStack()
        with stack:
            stack.enter_context(patch.object(
                self.runner, "_measure_auto_sub_combined_candidate",
                side_effect=self.combined_measure(per_stage={"gain_after": dipped})))
            self.steering(
                stack,
                gain_verdicts=[{"accepted": True, "reason": "test", "channels": {}}],
                correction_available=False,
                dip=_dip_by_shape)
            await self.run_runner(self.runner._run_auto_sub_22_optimize(**self.runner_args()))
        self.assertEqual(self.job["status"], "completed", self.job.get("error"))
        self.assertEqual(
            self.job["confirmation_gate"]["action"], "alignment_reverted_balance_kept")
        self.assert_service_end_state()


class RunnerStereoServiceIOTests(RunnerServiceIOTestBase):
    runner = runner_stereo
    roles = ("sub_l", "sub_r")
    context = {"mode": "stereo", "output_key": "dev", "channels": 4,
               "optimizer_path": "stereo-subs",
               "sub_role_map": {"sub1": "sub_l", "sub2": "sub_r"},
               "sub_mute_mask": 12}

    def steering(self, stack, *, step_deltas, gain_verdicts,
                 correction_plan=None, probe_plan=None, dip=None, veto=None,
                 calculate_side_effect=None):
        def score(rows, **_kwargs):
            results = []
            for row in rows:
                delay = round(float(row.get("delay_ms", 0.0)), 2)
                row_score = (0.6810 if delay in (-2.14, -2.54, -3.12, -0.58)
                             else (0.15 if delay == 0.0 else 0.05))
                results.append({**row, "score": row_score, "final_score": row_score,
                                "score_pct": round(row_score * 100.0, 1),
                                "xo_score": 0.75, "timing_band_score": 0.80,
                                "low_guard_loss_db": 0.0, "low_guard_penalty": 0.0})
            results.sort(key=lambda item: item["score"], reverse=True)
            return {"winner": results[0],
                    "runner_up": results[1] if len(results) > 1 else None,
                    "results": results, "confidence": "clear"}

        stack.enter_context(patch.object(
            self.runner, "score_sub_alignment_candidates", side_effect=score))
        stack.enter_context(patch.object(
            self.runner, "_auto_sub_fine_delay_candidates", return_value=[]))
        stack.enter_context(patch.object(
            self.runner, "_auto_sub_remeasure_tiebreak", new=AsyncMock(return_value=None)))
        stack.enter_context(patch.object(
            self.runner, "_auto_sub_polarity_decision",
            return_value={"accepted": False, "score_gain": -0.2,
                          "min_score_gain": 0.03, "reason": "test-incumbent-protected"}))
        stack.enter_context(patch.object(
            self.runner, "_calculate_auto_sub_gain",
            side_effect=list(calculate_side_effect) if calculate_side_effect is not None else (
                lambda **_kwargs: {
                    "available": True, "gain_calculated": True, "confidence": "medium",
                    "recommendation": {"type": "per_channel", "left_delta_db": 0.0,
                                       "right_delta_db": 0.0},
                    "channels": {
                        "left": {"raw_recommendation_db": 0.0, "target_delta_db": 0.0,
                                 "chain_anchor_db": 0.0},
                        "right": {"raw_recommendation_db": 0.0, "target_delta_db": 0.0,
                                  "chain_anchor_db": 0.0}}})))
        stack.enter_context(patch.object(
            self.runner, "_auto_sub_gain_deltas", return_value=dict(step_deltas)))
        stack.enter_context(patch.object(
            self.runner, "_auto_sub_gain_verdict", side_effect=list(gain_verdicts)))
        stack.enter_context(patch.object(
            self.runner, "_auto_sub_gain_response_correction",
            return_value=dict(correction_plan) if correction_plan is not None else None))
        stack.enter_context(patch.object(
            self.runner, "_auto_sub_stereo_probe_plan",
            return_value=dict(probe_plan) if probe_plan is not None else None))
        stack.enter_context(patch.object(
            self.runner, "_auto_sub_stereo_corridor_violation",
            side_effect=[{"available": True, "severity_db": 1.0},
                         {"available": True, "severity_db": 5.0}]))
        if dip is not None:
            stack.enter_context(patch.object(
                self.runner, "_auto_sub_local_dip_db", side_effect=dip))
        if veto is not None:
            stack.enter_context(patch.object(
                self.runner, "_auto_sub_dip_guard_should_veto", return_value=tuple(veto)))

    def single_measure(self, per_stage=None):
        per_stage = per_stage or {}

        async def measure(**kwargs):
            stage = str(kwargs.get("stage", ""))
            pts = copy.deepcopy(per_stage.get(stage, _points()))
            job = kwargs["job"]
            job.setdefault("_sweep_timings", []).append(
                {"channel": kwargs.get("channel", "left"), "stage": stage,
                 "durations": {"total_ms": 1.0}})
            return {
                "delay_ms": float(kwargs.get("delay_ms", 0.0)),
                "name": f'{kwargs.get("delay_ms", 0.0):.2f}',
                "status": "completed", "scan": stage,
                "points": copy.deepcopy(pts),
                "calibrated_points": copy.deepcopy(pts),
                "normalized_by_db": 0.0,
                "stage_output_peaks": {"stage": stage},
            }

        return measure

    def runner_args(self):
        return dict(job_id=self.job_id, input_id="mic",
                    mic_input_channel="1", reference_input_channel="",
                    calibration_ref="", calibration_filename=None, calibration_bytes=None,
                    left_scan_delays=[0.0, -3.12], right_scan_delays=[0.0, -0.58],
                    fc=80, original_config_snapshot=_legacy_22_snapshot("subwoofer-2.2-stereo"),
                    entry_epoch=7)

    async def test_happy_path_applies_pair_and_gain(self):
        stack = ExitStack()
        with stack:
            stack.enter_context(patch.object(
                self.runner, "_measure_auto_sub_candidate",
                side_effect=self.single_measure()))
            self.steering(
                stack, step_deltas={"left": 2.0, "right": 2.0},
                gain_verdicts=[{"accepted": True, "reason": "test",
                                "channels": {"left": {"accepted": True},
                                             "right": {"accepted": True}}},
                               {"accepted": True, "reason": "test", "channels": {}}],
                correction_plan={"available": True, "reason": "test",
                                 "deltas_db": {"left": 1.0, "right": 1.0},
                                 "channels": {}},
                dip=_dip_by_shape,
                veto=(False, {"failed_sides": []}))
            await self.run_runner(self.runner._run_auto_sub_22_stereo_optimize(**self.runner_args()))
        self.assertEqual(self.job["status"], "completed", self.job.get("error"))
        result = self.job["result"]
        self.assertEqual(result["applied_sub1_alignment_ms"], -3.12)
        self.assertEqual(result["applied_sub2_alignment_ms"], -0.58)
        self.assertEqual(result["derived_main_delay_ms"], 3.12)
        self.assertEqual(result["derived_sub1_delay_ms"], 0.0)
        self.assertEqual(result["derived_sub2_delay_ms"], 2.54)
        self.assertGreater(len(self.harness.transitions), 0)
        self.assert_service_end_state()

    async def test_step1_reject_restores_polarity_state(self):
        stack = ExitStack()
        with stack:
            stack.enter_context(patch.object(
                self.runner, "_measure_auto_sub_candidate",
                side_effect=self.single_measure()))
            self.steering(
                stack, step_deltas={"left": 2.0, "right": 2.0},
                gain_verdicts=[{"accepted": False, "reason": "test", "channels": {}}],
                dip=_dip_by_shape,
                veto=(False, {"failed_sides": []}))
            await self.run_runner(self.runner._run_auto_sub_22_stereo_optimize(**self.runner_args()))
        self.assertEqual(self.job["status"], "completed", self.job.get("error"))
        self.assertGreater(len(self.harness.transitions), 0)
        self.assert_service_end_state()

    async def test_partial_step1_keeps_improved_side(self):
        stack = ExitStack()
        with stack:
            stack.enter_context(patch.object(
                self.runner, "_measure_auto_sub_candidate",
                side_effect=self.single_measure()))
            self.steering(
                stack, step_deltas={"left": 2.0, "right": 2.0},
                gain_verdicts=[{"accepted": True, "reason": "test",
                                "channels": {"left": {"accepted": True},
                                             "right": {"accepted": False}}}],
                dip=_dip_by_shape,
                veto=(False, {"failed_sides": []}))
            await self.run_runner(self.runner._run_auto_sub_22_stereo_optimize(**self.runner_args()))
        self.assertEqual(self.job["status"], "completed", self.job.get("error"))
        self.assertGreater(len(self.harness.transitions), 0)
        self.assert_service_end_state()

    async def test_plain_correction_reject_restores_gain_state(self):
        stack = ExitStack()
        with stack:
            stack.enter_context(patch.object(
                self.runner, "_measure_auto_sub_candidate",
                side_effect=self.single_measure()))
            self.steering(
                stack, step_deltas={"left": 2.0, "right": 2.0},
                gain_verdicts=[{"accepted": True, "reason": "test",
                                "channels": {"left": {"accepted": True},
                                             "right": {"accepted": True}}},
                               {"accepted": False, "reason": "test", "channels": {}}],
                correction_plan={"available": True, "reason": "test",
                                 "deltas_db": {"left": 1.0, "right": 1.0},
                                 "channels": {}},
                dip=_dip_by_shape,
                veto=(False, {"failed_sides": []}))
            await self.run_runner(self.runner._run_auto_sub_22_stereo_optimize(**self.runner_args()))
        self.assertEqual(self.job["status"], "completed", self.job.get("error"))
        self.assertGreater(len(self.harness.transitions), 0)
        self.assert_service_end_state()

    async def test_probe_partial_reverts_regressed_side(self):
        def diagnostics(left_delta, right_delta):
            return {
                "available": True, "gain_calculated": True, "confidence": "medium",
                "recommendation": {"type": "per_channel", "left_delta_db": left_delta,
                                   "right_delta_db": right_delta},
                "channels": {
                    "left": {"raw_recommendation_db": left_delta,
                             "target_delta_db": left_delta, "chain_anchor_db": 0.0},
                    "right": {"raw_recommendation_db": right_delta,
                              "target_delta_db": right_delta, "chain_anchor_db": 0.0}}}

        stack = ExitStack()
        with stack:
            stack.enter_context(patch.object(
                self.runner, "_measure_auto_sub_candidate",
                side_effect=self.single_measure()))
            self.steering(
                stack, step_deltas={"left": 2.0, "right": 2.0},
                gain_verdicts=[{"accepted": True, "reason": "test",
                                "channels": {"left": {"accepted": True},
                                             "right": {"accepted": True}}}],
                correction_plan={"available": False, "reason": "test",
                                 "channels": {}, "deltas_db": {}},
                probe_plan={"available": True, "reason": "test",
                            "deltas_db": {"left": 1.0, "right": 1.0},
                            "channels": {
                                "left": {"corridor_before": {"available": True,
                                                             "severity_db": 5.0}},
                                "right": {"corridor_before": {"available": True,
                                                              "severity_db": 5.0}}}},
                calculate_side_effect=[diagnostics(0.0, 0.0), diagnostics(-3.0, -3.0),
                                       diagnostics(-1.0, -3.0)],
                dip=_dip_by_shape,
                veto=(False, {"failed_sides": []}))
            await self.run_runner(self.runner._run_auto_sub_22_stereo_optimize(**self.runner_args()))
        self.assertEqual(self.job["status"], "completed", self.job.get("error"))
        channels = self.job["auto_gain"]["correction_verdict"]["channels"]
        self.assertTrue(channels["left"]["accepted"])
        self.assertFalse(channels["right"]["accepted"])
        self.assertGreater(len(self.harness.transitions), 0)
        self.assert_service_end_state()

    async def test_veto_recheck_passed_reapplies_winner(self):
        dipped = _dipped_points()
        stack = ExitStack()
        with stack:
            stack.enter_context(patch.object(
                self.runner, "_measure_auto_sub_candidate",
                side_effect=self.single_measure(per_stage={"gain_after": dipped})))
            self.steering(
                stack, step_deltas={},
                gain_verdicts=[{"accepted": True, "reason": "test",
                                "channels": {"left": {"accepted": True},
                                             "right": {"accepted": True}}}],
                correction_plan={"available": False, "reason": "test",
                                 "channels": {}, "deltas_db": {}},
                probe_plan={"available": False, "reason": "test", "channels": {}},
                dip=_dip_by_shape,
                veto=(True, {"failed_sides": ["left"]}))
            await self.run_runner(self.runner._run_auto_sub_22_stereo_optimize(**self.runner_args()))
        self.assertEqual(self.job["status"], "completed", self.job.get("error"))
        self.assertEqual(self.job["confirmation_gate"]["action"],
                         "winner_alignment_original_kept")
        self.assertGreater(len(self.harness.transitions), 0)
        self.assert_service_end_state()


if __name__ == "__main__":
    unittest.main(verbosity=2)
