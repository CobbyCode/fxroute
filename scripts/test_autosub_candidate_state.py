#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Candidate proposals use real state persistence, plans and native rendering."""

from __future__ import annotations

import asyncio
import copy
import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.output_service import OutputService, OutputServiceDeps
from audio.output_state import default_output_state, set_mode_routing, validate_output_state, set_crossover
from audio.output_state_store import OutputStateStore, StateConflictError
from audio.output_topology import SUB_ROLES
from dsp.manager import DSPManager
from dsp.runtime import DSPRuntime, DSPRuntimeConfig, PlannedSyncTarget

try:
    candidate_module = importlib.import_module("measurement.autosub.candidate_state")
except ModuleNotFoundError as exc:
    if exc.name != "measurement.autosub.candidate_state":
        raise
    candidate_module = None


def state_fixture(ways=0, subs=("sub1",)):
    from audio.output_state import switch_mode
    state = default_output_state()
    mode = "stereo-sub"
    state = set_crossover(state, mode, bool(ways))
    speakers = ([f"{side}_{way}" for side in ("left", "right")
                 for way in (("low", "high") if ways == 2 else ("low", "mid", "high"))]
                if ways else ["main_l", "main_r"])
    state = switch_mode(set_mode_routing(state, mode, "dev", [*speakers, *subs]), mode)
    config = state["modes"][mode]
    for index, role in enumerate(speakers):
        settings = config["processing"][role]
        settings["alignment_ms"] = index * 0.5
        if ways:
            way = role.split("_", 1)[1]
            for kind, frequency in (("highpass", 300 if way == "mid" else 2500),
                                    ("lowpass", 300 if way == "low" else 2500)):
                if (kind == "highpass" and way != "low") or (kind == "lowpass" and way != "high"):
                    settings[kind] = {"family": "linkwitz-riley", "slope_db_oct": 24,
                                      "frequency_hz": frequency}
    for config in state["modes"].values():
        for bank in config["banks"].values():
            bank.update(preset="Room", preset_a="Neutral", preset_b="Room")
    return validate_output_state(state)


class Fixture:
    def setUp(self):
        self.assertIsNotNone(candidate_module, "candidate-state foundation is not implemented")
        directory = tempfile.TemporaryDirectory(prefix="autosub-candidate-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "output-state.json"
        self.manager = DSPManager(home=Path(directory.name) / "dsp")
        self.manager.preset_store.write("Room", {
            "schema": "fxroute.dsp.preset", "version": 1, "chain": []})
        self.store = OutputStateStore(self.path)
        self.service = OutputService(OutputServiceDeps(
            store=self.store, preset_loader=self.manager.preset_store.read,
            resolve_ir=lambda name: (_ for _ in ()).throw(
                AssertionError(f"fixture has no IR presets: {name}")),
            measurement_active=lambda: True))
        self.base = self.store.commit(state_fixture(), expected_revision=0)
        self.context = dict(output_key="dev", channels=3, sample_rate_hz=48000)
        self.render_error = False

    def candidate(self, state=None, *, output_key="dev", channels=3, **knobs):
        return candidate_module.sub_candidate_state(
            self.base if state is None else state,
            output_key=output_key, channels=channels, **knobs)

    def compile(self, state):
        return candidate_module.compile_candidate(state, service=self.service, **self.context)

    def build_target(self, plan, *, fingerprint):
        if self.render_error:
            raise RuntimeError("render failed")
        layout = self.service.compile_layout(plan)
        config = DSPRuntimeConfig.from_plan(
            plan, layout=layout, output_key="dev", sample_rate_hz=48000,
            hardware_ports=[f"playback_AUX{i}" for i in range(self.context["channels"])],
            plan_fingerprint=fingerprint)
        text = self.manager.compile_engine_text(
            layout, preset_name=plan["global"]["preset"], sample_rate_hz=48000,
            extras_override=plan["global"]["extras"])
        return PlannedSyncTarget(config, text)


class SubCandidateStateTests(Fixture, unittest.TestCase):
    def test_sub_trims_and_bass_only_change_requested_fields(self):
        base = state_fixture(subs=("sub_r", "sub1"))
        result = self.candidate(base, channels=4, sub_delays={"sub1": -3.5},
                                sub_levels={"sub_r": -4}, sub_polarities={"sub1": "invert"},
                                bass={"frequency_hz": 95, "main_highpass_enabled": False})
        expected = copy.deepcopy(base)
        config = expected["modes"]["stereo-sub"]
        config["processing"]["sub1"].update(alignment_ms=-3.5, polarity="invert")
        config["processing"]["sub_r"]["level_db"] = -4
        config["bass_management"].update(frequency_hz=95, main_highpass_enabled=False)
        self.assertEqual(result, expected)
        result["modes"]["stereo-sub"]["banks"]["sub1"]["preset"] = "Direct"
        self.assertEqual(base["modes"]["stereo-sub"]["banks"]["sub1"]["preset"], "Room")

    def test_two_and_three_way_banks_and_relative_delays_survive(self):
        for ways in (2, 3):
            with self.subTest(ways=ways):
                base = state_fixture(ways, ("sub_r", "sub1"))
                self.context["channels"] = ways * 2 + 2
                result = self.candidate(base, channels=self.context["channels"],
                                        sub_delays={"sub1": -4, "sub_r": 2},
                                        bass={"frequency_hz": 100})
                before_fp, before = self.compile(base)
                after_fp, after = self.compile(result)
                self.assertNotEqual(before_fp, after_fp)
                self.assertEqual(after["way_count"], ways)
                self.assertEqual(after["sub_mode"], "dual-mono")
                for mode in base["modes"]:
                    self.assertEqual(result["modes"][mode]["banks"], base["modes"][mode]["banks"])
                for old, new in zip(before["outputs"], after["outputs"]):
                    if old["role"] not in SUB_ROLES:
                        self.assertEqual(new["delay_ms"] - old["delay_ms"], 4)
                        self.assertEqual(result["modes"]["stereo-sub"]["processing"][old["role"]],
                                         base["modes"]["stereo-sub"]["processing"][old["role"]])
                    self.assertEqual(old["bank"], new["bank"])

    def test_fanout_is_one_sub_and_dormant_ports_do_not_authorize_changes(self):
        base = state_fixture(subs=("sub1", "sub1", "sub2"))
        result = self.candidate(base, channels=4, sub_delays={"sub1": 2})
        self.context["channels"] = 4
        _, plan = self.compile(result)
        self.assertEqual([row["role"] for row in plan["outputs"]], ["main_l", "main_r", "sub1"])
        self.assertEqual(plan["physical_routes"][-2:],
                         [{"output": 2, "channel": 2}, {"output": 2, "channel": 3}])
        for context in ({"channels": 4}, {"channels": 5, "output_key": "other"}):
            with self.subTest(context=context), self.assertRaises(ValueError):
                self.candidate(base, sub_delays={"sub2": 2}, **context)
        # Stored bank without a port is not an active sub either.
        base["modes"]["stereo-sub"]["routing"]["dev"] = ["main_l", "main_r", "sub1"]
        with self.assertRaises(ValueError):
            self.candidate(base, channels=5, sub_delays={"sub2": 2})

    def test_rejects_non_sub_roles_unknown_bass_fields_and_invalid_values(self):
        invalid = [
            {"sub_delays": {"main_l": 1}}, {"sub_levels": {"main_r": -3}},
            {"sub_polarities": {"left_low": "invert"}},
            {"sub_delays": {"sub1": float("nan")}}, {"sub_delays": {"sub1": 41}},
            {"sub_delays": {"sub1": True}}, {"sub_delays": {"sub1": None}},
            {"sub_levels": {"sub1": float("inf")}}, {"sub_levels": {"sub1": -81}},
            {"sub_levels": {"sub1": 25}}, {"sub_polarities": {"sub1": "sideways"}},
            {"bass": {"frequency_hz": 201}}, {"bass": {"frequency_hz": 39}},
            {"bass": {"main_highpass_enabled": 1}}, {"bass": {"unknown": 3}},
            {"sub_delays": []}, {"bass": []},
        ]
        for knobs in invalid:
            with self.subTest(knobs=knobs), self.assertRaises(ValueError):
                self.candidate(**knobs)

    def test_entire_state_is_validated_including_inactive_mode(self):
        base = copy.deepcopy(self.base)
        base["modes"]["stereo-sub"]["bass_management"]["frequency_hz"] = 999
        with self.assertRaises(ValueError):
            self.candidate(base, sub_delays={"sub1": 2})
        with self.assertRaises(ValueError):
            self.candidate(state_fixture(subs=()), bass={"frequency_hz": 90})

    def test_real_plan_fingerprint_tracks_audible_candidate(self):
        before_bytes = self.path.read_bytes()
        fp, plan = self.compile(self.candidate(sub_delays={"sub1": 3}))
        baseline_fp, baseline_plan = self.compile(self.base)
        self.assertNotEqual(fp, baseline_fp)
        self.assertEqual(len(fp), 64)
        self.assertEqual(plan["outputs"][2]["delay_ms"], 3)
        self.assertEqual(baseline_plan["outputs"][2]["delay_ms"], 0)
        self.assertEqual(fp, self.service.fingerprint_plan(plan))
        self.assertEqual(self.path.read_bytes(), before_bytes)


class HardwareBoundary:
    """Only engine/graph I/O is simulated; targets contain real rendered text.

    Matches the native guarded_rebuild_rendered callback protocol, including
    apply_previous's ability to veto rollback before touching a newer graph.
    """
    def __init__(self, target):
        self.target = target
        self.active = True
        self.gain = 0.0
        self.transitions = []
        self.reads = 0
        self.stage_error = False
        self.bad_readback = False
        self.restore_error = False
        self.entered = asyncio.Event()
        self.release = None
        self.during_stage = None

    async def readback(self):
        self.reads += 1
        return {"active": self.active, "output_gain_db": self.gain, "config": {
            "plan_fingerprint": self.target.config.plan_fingerprint}}

    async def guarded_stage(self, new, *, previous, guard_db, apply_candidate,
                            apply_previous, before_ramp, before_rollback_ramp,
                            ramp_target_db=0.0, rollback_ramp_target_db=0.0):
        if not -80 <= guard_db <= 0:
            raise AssertionError("invalid guard")
        self.transitions.append(new)
        try:
            self.target = new
            self.active = True
            self.entered.set()
            if self.release is not None:
                await self.release.wait()
            if self.during_stage:
                self.during_stage()
            if self.stage_error:
                raise RuntimeError("stage failed")
            if self.bad_readback:
                self.active = False
            await before_ramp()
            self.gain = ramp_target_db
            apply_candidate()
        except BaseException:
            apply_previous()
            if self.restore_error:
                self.active = False
                raise RuntimeError("restore failed")
            self.target = previous
            self.active = True
            await before_rollback_ramp()
            self.gain = rollback_ramp_target_db
            raise


class CandidateStagerTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        fp, plan = self.compile(self.base)
        self.initial = self.build_target(plan, fingerprint=fp)
        self.hardware = HardwareBoundary(self.initial)
        self.stager = candidate_module.CandidateStager(
            service=self.service, start_state=self.base, build_target=self.build_target,
            guarded_stage=self.hardware.guarded_stage, readback=self.hardware.readback,
            guard_db=-30, **self.context)
        self.bytes_before = self.path.read_bytes()

    def assert_unchanged(self):
        self.assertEqual(self.path.read_bytes(), self.bytes_before)
        self.assertEqual(OutputStateStore(self.path).load()["revision"], 1)

    def newer_winner(self):
        winner = self.service.load()
        winner["modes"]["stereo-sub"]["processing"]["sub1"]["alignment_ms"] = 9
        winner = self.service.commit(winner, expected_revision=1)
        fp, plan = self.compile(winner)
        self.hardware.target = self.build_target(plan, fingerprint=fp)
        self.winner_bytes = self.path.read_bytes()
        self.winner_target = self.hardware.target

    async def test_stage_renders_and_verifies_without_persisting(self):
        result = await self.stager.stage(sub_delays={"sub1": 5})
        self.assertEqual(result["plan"]["outputs"][2]["delay_ms"], 5)
        self.assertEqual(self.hardware.target.config.plan_fingerprint, result["fingerprint"])
        self.assertIn("output 2 0 5 normal", self.hardware.target.text)
        self.assertTrue(self.hardware.active)
        self.assertGreater(self.hardware.reads, 0)
        self.assert_unchanged()

    async def test_identical_cache_rechecks_active_and_fingerprint(self):
        first = await self.stager.stage(sub_delays={"sub1": 5})
        reads = self.hardware.reads
        second = await self.stager.stage(sub_delays={"sub1": 5})
        self.assertEqual(first, second)
        self.assertEqual(len(self.hardware.transitions), 1)
        self.assertGreater(self.hardware.reads, reads)
        for loss in ("inactive", "different"):
            if loss == "inactive":
                self.hardware.active = False
            else:
                self.hardware.target = self.initial
            await self.stager.stage(sub_delays={"sub1": 5})
            self.assertTrue(self.hardware.active)
            self.assertEqual(self.hardware.target.config.plan_fingerprint, first["fingerprint"])
        self.assertEqual(len(self.hardware.transitions), 3)
        self.assert_unchanged()

    async def test_each_candidate_uses_detached_start_not_last_candidate(self):
        first = await self.stager.stage(sub_delays={"sub1": 5})
        self.base["modes"]["stereo-sub"]["processing"]["sub1"]["alignment_ms"] = 30
        first["state"]["modes"]["stereo-sub"]["processing"]["sub1"]["alignment_ms"] = 25
        second = await self.stager.stage(sub_levels={"sub1": -2})
        self.assertEqual(second["plan"]["outputs"][2]["delay_ms"], 0)
        self.assertEqual(second["plan"]["outputs"][2]["gain_db"], -2)
        self.assert_unchanged()

    async def test_render_failure_does_not_resync_previous(self):
        await self.stager.stage(sub_delays={"sub1": 2})
        previous = self.hardware.target
        self.render_error = True
        with self.assertRaisesRegex(RuntimeError, "render failed"):
            await self.stager.stage(sub_delays={"sub1": 5})
        self.assertEqual(self.hardware.target, previous)
        self.assertEqual(len(self.hardware.transitions), 1)
        self.assert_unchanged()

    async def test_stage_and_readback_failure_restore_last_verified_candidate(self):
        await self.stager.stage(sub_delays={"sub1": 2})
        previous = self.hardware.target
        for flag in ("stage_error", "bad_readback"):
            with self.subTest(flag=flag):
                setattr(self.hardware, flag, True)
                with self.assertRaises(RuntimeError):
                    await self.stager.stage(sub_delays={"sub1": 5})
                setattr(self.hardware, flag, False)
                self.assertEqual(self.hardware.target, previous)
                self.assertTrue(self.hardware.active)
                self.assert_unchanged()

    async def test_readback_transport_failure_still_attempts_guarded_restoration(self):
        reads = 0

        async def readback():
            nonlocal reads
            reads += 1
            if 2 <= reads <= 3:
                raise RuntimeError("readback unavailable")
            return await self.hardware.readback()

        async def stage_without_internal_rollback(new, **kwargs):
            self.hardware.transitions.append(new)
            self.hardware.target = new
            await kwargs["before_ramp"]()

        stager = candidate_module.CandidateStager(
            service=self.service, start_state=self.base, build_target=self.build_target,
            guarded_stage=stage_without_internal_rollback, readback=readback,
            guard_db=-30, **self.context)
        with self.assertRaisesRegex(RuntimeError, "readback unavailable"):
            await stager.stage(sub_delays={"sub1": 5})
        self.assertEqual(self.hardware.target, self.initial)
        self.assertTrue(self.hardware.active)
        self.assertEqual(len(self.hardware.transitions), 2)
        self.assertGreaterEqual(reads, 3)
        self.assert_unchanged()

    async def test_post_stage_wrong_fingerprint_restores_even_if_callback_skips_hooks(self):
        calls = 0

        async def stage(new, **kwargs):
            nonlocal calls
            calls += 1
            self.hardware.target = new
            if calls == 1:
                fp, plan = self.compile(self.candidate(sub_delays={"sub1": 9}))
                self.hardware.target = self.build_target(plan, fingerprint=fp)

        stager = candidate_module.CandidateStager(
            service=self.service, start_state=self.base, build_target=self.build_target,
            guarded_stage=stage, readback=self.hardware.readback,
            guard_db=-30, **self.context)
        with self.assertRaisesRegex(RuntimeError, "fingerprint"):
            await stager.stage(sub_delays={"sub1": 5})
        self.assertEqual(self.hardware.target, self.initial)
        self.assertEqual(calls, 2)
        self.assert_unchanged()

    async def test_cancel_after_newer_winner_does_not_restore_start(self):
        self.hardware.release = asyncio.Event()
        task = asyncio.create_task(self.stager.stage(sub_delays={"sub1": 5}))
        await self.hardware.entered.wait()
        self.newer_winner()
        task.cancel()
        with self.assertRaises(StateConflictError):
            await task
        self.assertEqual(self.hardware.target, self.winner_target)
        self.assertEqual(self.path.read_bytes(), self.winner_bytes)

    async def test_restoration_failure_is_reported_not_claimed_as_success(self):
        self.hardware.stage_error = self.hardware.restore_error = True
        with self.assertRaises(candidate_module.CandidateRestoreError):
            await self.stager.stage(sub_delays={"sub1": 5})
        self.assertFalse(self.hardware.active)
        self.assert_unchanged()

    async def test_cancel_waits_for_verified_restoration(self):
        self.hardware.release = asyncio.Event()
        task = asyncio.create_task(self.stager.stage(sub_delays={"sub1": 5}))
        await self.hardware.entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.hardware.target, self.initial)
        self.assertTrue(self.hardware.active)
        self.assertGreater(self.hardware.reads, 0)
        self.assert_unchanged()

    async def test_restore_returns_to_start_without_commit(self):
        await self.stager.stage(sub_delays={"sub1": 5})
        await self.stager.restore()
        self.assertEqual(self.hardware.target, self.initial)
        self.assertTrue(self.hardware.active)
        self.assert_unchanged()

    async def test_stale_before_stage_or_restore_leaves_newer_winner_alone(self):
        await self.stager.stage(sub_delays={"sub1": 5})
        self.newer_winner()
        calls = len(self.hardware.transitions)
        for operation in (self.stager.stage(sub_delays={"sub1": 5}), self.stager.restore()):
            with self.assertRaises(StateConflictError):
                await operation
        self.assertEqual(len(self.hardware.transitions), calls)
        self.assertEqual(self.path.read_bytes(), self.winner_bytes)
        self.assertEqual(self.hardware.target, self.winner_target)

    async def test_stale_during_stage_vetoes_internal_and_external_rollback(self):
        self.hardware.during_stage = self.newer_winner
        with self.assertRaises(StateConflictError):
            await self.stager.stage(sub_delays={"sub1": 5})
        self.assertEqual(len(self.hardware.transitions), 1)
        self.assertEqual(self.hardware.target, self.winner_target)
        self.assertEqual(self.path.read_bytes(), self.winner_bytes)
        with self.assertRaises(StateConflictError):
            await self.stager.stage(sub_delays={"sub1": 6})

    async def test_post_stage_revision_check_even_if_callback_skips_hooks(self):
        async def stage(new, **kwargs):
            self.hardware.target = new
            self.newer_winner()
        stager = candidate_module.CandidateStager(
            service=self.service, start_state=self.base, build_target=self.build_target,
            guarded_stage=stage, readback=self.hardware.readback,
            guard_db=-30, **self.context)
        with self.assertRaises(StateConflictError):
            await stager.stage(sub_delays={"sub1": 5})
        self.assertEqual(self.hardware.target, self.winner_target)
        self.assertEqual(self.path.read_bytes(), self.winner_bytes)


class RealGuardedRecoveryTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        fingerprint, plan = self.compile(self.base)
        self.initial = self.build_target(plan, fingerprint=fingerprint)
        self.runtime = DSPRuntime(self.manager)
        self.runtime._config = self.initial.config
        self.runtime._process = SimpleNamespace(returncode=None, pid=1)
        self.runtime._links = [object()]
        self.runtime._control_socket = object()
        self.runtime._can_hot_update = lambda config: False
        self.syncs = []
        self.settles = [asyncio.Event() for _ in range(3)]
        self.release_recovery = asyncio.Event()
        self.rollback_ramp_error = False
        self.gain_commands = []
        self.engine_gain = 0.0
        # When set, the engine reports this gain instead of accepting the last
        # write, i.e. a gain control whose effect never lands.
        self.gain_readback = None
        self.bytes_before = self.path.read_bytes()

        async def sync(config, text, *, initial_output_gain_db):
            self.syncs.append(config.plan_fingerprint)
            self.runtime._config = config
            await self.runtime.set_output_gain_db(initial_output_gain_db)

        async def control(command, *, reply):
            if command == "gain db get":
                # The engine's own readback, the authority on the operating gain.
                if self.gain_readback is not None:
                    return f"{self.gain_readback:.9g}"
                return f"{self.engine_gain:.9g}"
            gain = float(command.removeprefix("gain db "))
            if self.rollback_ramp_error and len(self.syncs) == 2 and gain > -30:
                raise RuntimeError("rollback ramp failed")
            self.engine_gain = gain
            self.gain_commands.append(gain)
            return "ok"

        async def sleep(seconds):
            if seconds == 0.35:
                index = len(self.syncs) - 1
                if index < len(self.settles):
                    self.settles[index].set()
                if index < 2:
                    await asyncio.Event().wait()
                else:
                    await self.release_recovery.wait()

        self.runtime._run_sync = sync
        self.runtime._control = control
        self.asyncio_sleep = asyncio.sleep
        self.sleep_patch = patch("dsp.runtime.asyncio.sleep", sleep)
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

        async def readback():
            return self.runtime.snapshot()

        self.stager = candidate_module.CandidateStager(
            service=self.service, start_state=self.base, build_target=self.build_target,
            guarded_stage=self.runtime.guarded_rebuild_rendered, readback=readback,
            guard_db=-30, **self.context)

    def set_engine_gain(self, value):
        """Set the engine's gain and this process's record of it together."""
        self.engine_gain = value
        self.runtime._output_gain_db = value

    def assert_restored(self, gain):
        snapshot = self.runtime.snapshot()
        self.assertTrue(snapshot["active"])
        self.assertEqual(snapshot["config"]["plan_fingerprint"],
                         self.initial.config.plan_fingerprint)
        self.assertEqual(snapshot["output_gain_db"], gain)
        self.assertTrue(all(command <= gain for command in self.gain_commands),
                        self.gain_commands)
        self.assertEqual(self.path.read_bytes(), self.bytes_before)
        self.assertEqual(self.stager._current.fingerprint, self.initial.config.plan_fingerprint)

    async def test_repeated_cancel_during_rollback_settle_restores_original_gain(self):
        for gain in (0.0, -12.0):
            with self.subTest(gain=gain):
                self.syncs.clear()
                self.gain_commands.clear()
                self.settles = [asyncio.Event() for _ in range(3)]
                self.set_engine_gain(gain)
                self.release_recovery.set()
                task = asyncio.create_task(self.stager.stage(sub_delays={"sub1": 5}))
                with self.assertLogs("dsp.runtime", level="ERROR"):
                    await asyncio.wait_for(self.settles[0].wait(), 2)
                    task.cancel()
                    await asyncio.wait_for(self.settles[1].wait(), 2)
                    self.assertEqual(self.runtime.snapshot()["output_gain_db"], -30)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 2)
                self.assert_restored(gain)

    async def test_stage_and_recovery_never_exceed_captured_pretransition_gain(self):
        self.set_engine_gain(-12.0)
        self.release_recovery.set()
        task = asyncio.create_task(self.stager.stage(sub_delays={"sub1": 5}))
        with self.assertLogs("dsp.runtime", level="ERROR"):
            await asyncio.wait_for(self.settles[0].wait(), 2)
            task.cancel()
            await asyncio.wait_for(self.settles[1].wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        self.assert_restored(-12.0)
        self.assertTrue(all(gain <= -12.0 for gain in self.gain_commands),
                        self.gain_commands)

    async def test_successful_stage_ramps_back_to_captured_gain_not_zero(self):
        self.runtime._can_hot_update = lambda config: True
        for delay, gain in ((5, -12.0), (6, -40.0)):
            with self.subTest(gain=gain):
                self.set_engine_gain(gain)
                self.gain_commands.clear()
                result = await asyncio.wait_for(
                    self.stager.stage(sub_delays={"sub1": delay}), 2)
                self.assertTrue(self.runtime.snapshot()["active"])
                self.assertEqual(self.runtime.snapshot()["config"]["plan_fingerprint"],
                                 result["fingerprint"])
                self.assertEqual(self.runtime.snapshot()["output_gain_db"], gain)
                self.assertTrue(all(command <= gain for command in self.gain_commands),
                                self.gain_commands)
                self.assertEqual(self.path.read_bytes(), self.bytes_before)

    async def test_failed_stage_internal_rollback_respects_captured_gain_ceiling(self):
        self.runtime._can_hot_update = lambda config: True

        async def readback():
            snapshot = self.runtime.snapshot()
            if snapshot["config"]["plan_fingerprint"] != self.initial.config.plan_fingerprint:
                raise RuntimeError("candidate readback failed")
            return snapshot

        self.stager._readback = readback
        for gain in (-12.0, -40.0):
            with self.subTest(gain=gain):
                self.set_engine_gain(gain)
                self.syncs.clear()
                self.gain_commands.clear()
                with self.assertRaisesRegex(RuntimeError, "candidate readback failed"):
                    await self.stager.stage(sub_delays={"sub1": 5})
                self.assert_restored(gain)
                self.assertEqual(len(self.syncs), 2)

    async def test_existing_caller_default_ramps_forward_and_rollback_to_zero(self):
        self.set_engine_gain(-12.0)
        await self.runtime.guarded_rebuild_rendered(
            self.stager._current.target, previous=self.initial,
            guard_db=-30.0, apply_candidate=lambda: None,
            apply_previous=lambda: None, settle_seconds=0.0)
        self.assertEqual(self.runtime.snapshot()["output_gain_db"], 0.0)
        self.assertEqual(self.gain_commands[-1], 0.0)

        async def fail():
            raise RuntimeError("stage failed")

        with self.assertRaisesRegex(RuntimeError, "stage failed"):
            await self.runtime.guarded_rebuild_rendered(
                self.initial, previous=self.initial, guard_db=-30.0,
                apply_candidate=lambda: None, apply_previous=lambda: None,
                before_ramp=fail, settle_seconds=0.0)
        self.assertEqual(self.runtime.snapshot()["output_gain_db"], 0.0)

    async def test_runtime_rejects_invalid_ramp_targets_before_any_actions(self):
        for field in ("ramp_target_db", "rollback_ramp_target_db"):
            for value in (float("nan"), float("inf"), -81, 1, True, "-12", None):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        await self.runtime.guarded_rebuild_rendered(
                            self.initial, previous=self.initial, guard_db=-30,
                            apply_candidate=lambda: None, apply_previous=lambda: None,
                            settle_seconds=0.0, **{field: value})
                    self.assertEqual(self.syncs, [])
                    self.assertEqual(self.gain_commands, [])

    async def test_failed_gain_restoration_is_reported_even_with_matching_identity(self):
        self.set_engine_gain(-12.0)
        self.release_recovery.set()
        task = asyncio.create_task(self.stager.stage(sub_delays={"sub1": 5}))
        with self.assertLogs("dsp.runtime", level="ERROR"):
            await asyncio.wait_for(self.settles[0].wait(), 2)
            # The guard no longer comes back: the engine keeps reporting it.
            self.gain_readback = -30.0
            task.cancel()
            await asyncio.wait_for(self.settles[1].wait(), 2)
            task.cancel()
            with self.assertRaises(candidate_module.CandidateRestoreError):
                await asyncio.wait_for(task, 2)
        self.assertEqual(self.runtime.snapshot()["config"]["plan_fingerprint"],
                         self.initial.config.plan_fingerprint)
        self.assertEqual(self.path.read_bytes(), self.bytes_before)

    async def test_invalid_pretransition_gain_fails_before_graph_work(self):
        for gain in (None, True, float("nan"), 1.0, -81.0):
            with self.subTest(gain=gain):
                self.runtime._output_gain_db = gain
                with self.assertRaisesRegex(RuntimeError, "gain readback"):
                    await self.stager.stage(sub_delays={"sub1": 5})
                self.assertEqual(self.syncs, [])
                self.assertEqual(self.gain_commands, [])
                self.assertEqual(self.path.read_bytes(), self.bytes_before)

    async def test_further_cancel_waits_for_recovery_gain_restoration(self):
        self.set_engine_gain(-12.0)
        task = asyncio.create_task(self.stager.stage(sub_delays={"sub1": 5}))
        with self.assertLogs("dsp.runtime", level="ERROR"):
            await asyncio.wait_for(self.settles[0].wait(), 2)
            task.cancel()
            await asyncio.wait_for(self.settles[1].wait(), 2)
            # The guard is reported again, so the caller must still recover.
            self.gain_readback = -30.0
            task.cancel()
            await asyncio.wait_for(self.settles[2].wait(), 2)
        task.cancel()
        await self.asyncio_sleep(0)
        self.assertFalse(task.done())
        self.gain_readback = None
        self.release_recovery.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        self.assert_restored(-12.0)

    async def test_stale_during_recovery_vetoes_gain_restoration(self):
        task = asyncio.create_task(self.stager.stage(sub_delays={"sub1": 5}))
        with self.assertLogs("dsp.runtime", level="ERROR"):
            await asyncio.wait_for(self.settles[0].wait(), 2)
            task.cancel()
            await asyncio.wait_for(self.settles[1].wait(), 2)
            # The guard is reported again, so the caller must still recover.
            self.gain_readback = -30.0
            task.cancel()
            await asyncio.wait_for(self.settles[2].wait(), 2)
            winner = self.service.commit(self.candidate(sub_delays={"sub1": 9}),
                                         expected_revision=1)
            fingerprint, plan = self.compile(winner)
            self.runtime._config = self.build_target(plan, fingerprint=fingerprint).config
            self.set_engine_gain(-7.0)
            commands = list(self.gain_commands)
            syncs = list(self.syncs)
            winner_bytes = self.path.read_bytes()
            self.release_recovery.set()
            with self.assertRaises(StateConflictError):
                await asyncio.wait_for(task, 2)
        self.assertEqual(self.runtime.snapshot()["config"]["plan_fingerprint"], fingerprint)
        self.assertEqual(self.runtime.snapshot()["output_gain_db"], -7.0)
        self.assertEqual(self.gain_commands, commands)
        self.assertEqual(self.syncs, syncs)
        self.assertEqual(self.path.read_bytes(), winner_bytes)

    async def test_failed_transition_does_not_leave_the_engine_on_the_guard(self):
        """A rollback whose ramp fails must still return to the operating gain."""
        self.set_engine_gain(-12.0)
        ramps = []

        async def ramp(start, target, **kwargs):
            # The candidate never reaches its ramp (before_ramp fails first), so
            # every ramp call here is the rollback's.
            ramps.append((start, target))
            raise RuntimeError("rollback ramp failed")

        self.runtime.ramp_output_gain_db = ramp

        async def fail():
            raise RuntimeError("stage failed")

        with self.assertLogs("dsp.runtime", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "stage failed"):
                await self.runtime.guarded_rebuild_rendered(
                    self.initial, previous=self.initial, guard_db=-30.0,
                    ramp_target_db=-12.0, rollback_ramp_target_db=-12.0,
                    apply_candidate=lambda: None, apply_previous=lambda: None,
                    before_ramp=fail, settle_seconds=0.0)
        self.assertEqual(self.engine_gain, -12.0)
        self.assertEqual(await self.runtime.read_output_gain_db(), -12.0)
        self.assertEqual(self.runtime.snapshot()["output_gain_db"], -12.0)

    async def test_stale_guard_target_cannot_pin_the_output_low(self):
        """A caller reading the guard back as the operating gain must not latch it."""
        self.set_engine_gain(-12.0)
        with self.assertLogs("dsp.runtime", level="WARNING") as logs:
            await self.runtime.guarded_rebuild_rendered(
                self.initial, previous=self.initial, guard_db=-30.0,
                ramp_target_db=-30.0, rollback_ramp_target_db=-30.0,
                apply_candidate=lambda: None, apply_previous=lambda: None,
                settle_seconds=0.0)
        self.assertEqual(self.runtime.snapshot()["output_gain_db"], -12.0)
        self.assertEqual(await self.runtime.read_output_gain_db(), -12.0)
        self.assertEqual(self.gain_commands[-1], -12.0)
        self.assertIn("below the engine's operating gain",
                      "\n".join(record.getMessage() for record in logs.records))

    async def test_readback_replaces_a_recorded_gain_the_engine_disagrees_with(self):
        """A record left on the guard is corrected from the engine, not trusted."""
        self.engine_gain = 0.0
        self.runtime._output_gain_db = -30.0
        await self.runtime.guarded_rebuild_rendered(
            self.initial, previous=self.initial, guard_db=-30.0,
            apply_candidate=lambda: None, apply_previous=lambda: None,
            settle_seconds=0.0)
        self.assertEqual(self.runtime.snapshot()["output_gain_db"], 0.0)
        self.assertEqual(self.engine_gain, 0.0)

    async def test_readback_rejects_a_reply_it_cannot_use(self):
        self.set_engine_gain(-12.0)
        for reply in ("not a number\n", "-120\n", "1\n"):
            with self.subTest(reply=reply):
                async def control(command, *, reply, answer=reply):
                    return answer if command == "gain db get" else "ok"

                self.runtime._control = control
                with self.assertRaisesRegex(RuntimeError, "gain readback"):
                    await self.runtime.read_output_gain_db()
                self.assertEqual(self.runtime.snapshot()["output_gain_db"], -12.0)

    async def test_rollback_ramp_failure_restores_original_gain(self):
        self.set_engine_gain(-12.0)
        self.rollback_ramp_error = True
        self.release_recovery.set()
        # Let rollback settle finish, then fail its real gain-control command.
        async def sleep(seconds):
            if seconds == 0.35 and len(self.syncs) == 1:
                self.settles[0].set()
                await asyncio.Event().wait()
        with patch("dsp.runtime.asyncio.sleep", sleep), self.assertLogs("dsp.runtime", level="ERROR"):
            task = asyncio.create_task(self.stager.stage(sub_delays={"sub1": 5}))
            await asyncio.wait_for(self.settles[0].wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        self.assert_restored(-12.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
