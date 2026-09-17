#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Session ownership over real output state and fake guarded hardware I/O."""

from __future__ import annotations

import asyncio
import copy
import gc
import importlib
import sys
import tempfile
import unittest
import weakref
import wave
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.output_service import OutputService, OutputServiceDeps
from audio.output_state import default_output_state, set_mode_routing, validate_output_state
from audio.output_state_store import OutputStateStore, StateConflictError
from dsp.manager import DSPManager
from dsp.runtime import DSPRuntimeConfig, PlannedSyncTarget

try:
    session_module = importlib.import_module("measurement.autosub.candidate_session")
except ModuleNotFoundError as exc:
    if exc.name != "measurement.autosub.candidate_session":
        raise
    session_module = None


class HardwareBoundary:
    """Honor revision vetoes, guarded verification hooks and both gain targets."""

    def __init__(self, target):
        self.target = target
        self.active = True
        self.gain = -12.0
        self.transitions = []
        self.reads = 0
        self.during_readback = None
        self.rate_override = None
        self.fingerprint_override = None

    async def readback(self):
        self.reads += 1
        if self.during_readback:
            self.during_readback()
        return {"active": self.active, "output_gain_db": self.gain, "config": {
            "plan_fingerprint": self.fingerprint_override or self.target.config.plan_fingerprint,
            "sample_rate": self.rate_override or self.target.config.sample_rate}}

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


class AutoSubCandidateSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.assertIsNotNone(session_module, "AutoSub candidate session is not implemented")
        directory = tempfile.TemporaryDirectory(prefix="autosub-session-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "output-state.json"
        self.manager = DSPManager(home=Path(directory.name) / "dsp")
        self.store = OutputStateStore(self.path)
        self.service = OutputService(OutputServiceDeps(
            store=self.store, preset_loader=self.manager.preset_store.read,
            resolve_ir=lambda name: (_ for _ in ()).throw(AssertionError(name)),
            measurement_active=lambda: True))
        state = set_mode_routing(default_output_state(), "stereo", "dev",
                                 ["main_l", "main_r", "sub1", "sub2"])
        self.base = self.store.commit(state, expected_revision=0)
        self.context = dict(output_key="dev", channels=4)
        self.initial = self.target_for(self.base)
        self.hardware = HardwareBoundary(self.initial)
        self.owner = self.make_owner()
        self.before_bytes = self.path.read_bytes()

    def build_target(self, plan, *, fingerprint):
        rate = plan["sample_rate_hz"]
        layout = self.service.compile_layout(plan)
        config = DSPRuntimeConfig.from_plan(
            plan, layout=layout, output_key="dev", sample_rate_hz=rate,
            hardware_ports=[f"playback_AUX{i}" for i in range(4)],
            plan_fingerprint=fingerprint)
        text = self.manager.compile_engine_text(
            layout, preset_name=plan["global"]["preset"], sample_rate_hz=rate,
            extras_override=plan["global"]["extras"])
        return PlannedSyncTarget(config, text)

    def target_for(self, state):
        plan = self.service.compile_plan(state, sample_rate_hz=48000, **self.context)
        return self.build_target(plan, fingerprint=self.service.fingerprint_plan(plan))

    def make_owner(self, **overrides):
        arguments = dict(service=self.service, start_state=self.base,
                         build_target=self.build_target,
                         guarded_stage=self.hardware.guarded_stage,
                         readback=self.hardware.readback, guard_db=-30, **self.context)
        arguments.update(overrides)
        return session_module.AutoSubCandidateSession(**arguments)

    def proposal(self, **overrides):
        fields = dict(sub_delays={"sub1": 0, "sub2": 0},
                      sub_levels={"sub1": 0, "sub2": 0},
                      sub_polarities={"sub1": "normal", "sub2": "normal"},
                      bass={"frequency_hz": 80, "main_highpass_enabled": True})
        fields.update(overrides)
        return session_module.AutoSubProposal(**fields)

    def newer_winner(self):
        winner = self.service.load()
        winner["modes"]["stereo"]["processing"]["sub1"]["alignment_ms"] = 9
        winner = self.service.commit(winner, expected_revision=winner["revision"])
        self.hardware.target = self.target_for(winner)
        return winner

    def test_construction_is_inert_even_if_revision_is_stale(self):
        self.newer_winner()
        with patch.object(self.service, "load", side_effect=AssertionError("load")), \
                patch.object(self.service, "compile_plan", side_effect=AssertionError("compile")), \
                patch.object(self.service, "commit", side_effect=AssertionError("commit")):
            self.make_owner(build_target=lambda *a, **k: self.fail("render"))
        self.assertEqual(self.hardware.transitions, [])
        self.assertEqual(self.hardware.reads, 0)

    def test_constructor_validates_topology_and_guard(self):
        for overrides in ({"channels": 2}, {"guard_db": True}, {"guard_db": -81},
                          {"guard_db": float("nan")}, {"readback": None}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.make_owner(**overrides)

    def test_proposal_is_frozen_and_detached_from_input(self):
        delays = {"sub1": 3, "sub2": 0}
        proposal = self.proposal(sub_delays=delays)
        delays["sub1"] = 19
        self.assertEqual(proposal.sub_delays["sub1"], 3)
        with self.assertRaises(FrozenInstanceError):
            proposal.bass = {}

    def test_activation_checks_revision_before_rendering(self):
        self.newer_winner()
        with patch.object(self.service, "compile_plan", side_effect=AssertionError("compile")):
            with self.assertRaises(StateConflictError):
                self.owner.activate(48000)
        self.assertEqual(self.hardware.transitions, [])

    def test_activation_validates_rate_and_cannot_rebind(self):
        for rate in (0, -1, True, 48000.0, "48000", None):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                self.owner.activate(rate)
        self.owner.activate(44100)
        self.assertEqual(self.hardware.transitions, [])
        with self.assertRaises(RuntimeError):
            self.owner.activate(48000)

    async def test_stage_requires_activation(self):
        with self.assertRaises(RuntimeError):
            await self.owner.stage(self.proposal())

    async def test_complete_maps_are_required_for_every_routed_sub(self):
        self.owner.activate(48000)
        for field in ("sub_delays", "sub_levels", "sub_polarities"):
            for values in ({"sub1": 0}, {}, None, []):
                with self.subTest(field=field, values=values), self.assertRaises(ValueError):
                    await self.owner.stage(self.proposal(**{field: values}))
        self.assertEqual(self.hardware.transitions, [])

    async def test_unknown_non_sub_and_dormant_roles_are_rejected(self):
        self.owner.activate(48000)
        for role in ("unknown", "main_l", "sub_l"):
            with self.subTest(role=role), self.assertRaises(ValueError):
                await self.owner.stage(self.proposal(sub_delays={"sub1": 0, "sub2": 0, role: 2}))
        # A stored port outside the hardware tier is dormant, not an optimizable sub.
        owner = self.make_owner(channels=3)
        owner.activate(48000)
        with self.assertRaises(ValueError):
            await owner.stage(self.proposal())
        self.assertEqual(self.hardware.transitions, [])

    async def test_bass_requires_exactly_both_fields(self):
        self.owner.activate(48000)
        for bass in ({}, {"frequency_hz": 90}, {"main_highpass_enabled": False}, None,
                     {"frequency_hz": 90, "main_highpass_enabled": True, "extra": 1}):
            with self.subTest(bass=bass), self.assertRaises(ValueError):
                await self.owner.stage(self.proposal(bass=bass))
        self.assertEqual(self.hardware.transitions, [])

    async def test_values_use_authoritative_state_validation(self):
        self.owner.activate(48000)
        for fields in ({"sub_delays": {"sub1": float("nan"), "sub2": 0}},
                       {"sub_levels": {"sub1": -81, "sub2": 0}},
                       {"sub_polarities": {"sub1": "bad", "sub2": "normal"}},
                       {"bass": {"frequency_hz": 999, "main_highpass_enabled": True}}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                await self.owner.stage(self.proposal(**fields))
        self.assertEqual(self.hardware.transitions, [])

    async def test_successive_candidates_never_inherit_previous_knobs(self):
        self.owner.activate(48000)
        await self.owner.stage(self.proposal(
            sub_levels={"sub1": -6, "sub2": -3},
            sub_polarities={"sub1": "invert", "sub2": "invert"}))
        result = await self.owner.stage(self.proposal(sub_delays={"sub1": 4, "sub2": 2}))
        expected = copy.deepcopy(self.base)
        expected["modes"]["stereo"]["processing"]["sub1"]["alignment_ms"] = 4
        expected["modes"]["stereo"]["processing"]["sub2"]["alignment_ms"] = 2
        self.assertEqual(result["state"], validate_output_state(expected))
        self.assertEqual(self.path.read_bytes(), self.before_bytes)

    async def test_stage_returns_detached_compiled_layout_and_plan_mode(self):
        self.owner.activate(44100)
        result = await self.owner.stage(self.proposal(sub_delays={"sub1": 4, "sub2": 0}))
        self.assertEqual(set(result), {"state", "plan", "fingerprint", "expected_native_layout",
                                      "expected_native_output_mode"})
        self.assertEqual(result["expected_native_layout"], self.service.compile_layout(result["plan"]))
        self.assertEqual(result["expected_native_output_mode"], "stereo")
        self.assertEqual(result["fingerprint"], self.service.fingerprint_plan(result["plan"]))
        self.assertEqual(result["plan"]["sample_rate_hz"], 44100)
        self.assertEqual(self.hardware.target.config.plan_fingerprint, result["fingerprint"])
        result["state"]["modes"]["stereo"]["processing"]["sub1"]["alignment_ms"] = 25
        result["plan"].clear()
        result["expected_native_layout"].clear()
        result["fingerprint"] = "tampered"
        snapshot = await self.owner.ensure_ready(44100)
        self.assertEqual(snapshot["config"]["plan_fingerprint"], self.hardware.target.config.plan_fingerprint)
        committed = await self.owner.commit_winner(self.proposal(sub_delays={"sub1": 4, "sub2": 0}))
        self.assertEqual(committed["modes"]["stereo"]["processing"]["sub1"]["alignment_ms"], 4)

    def ir_info(self, resolution):
        path = self.path.parent / f"resolution-{resolution}.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(48000)
            wav.writeframes(b"\x00\x00" * 8)
        return {"path": str(path), "channels": 1}

    def configure_ir(self, resolver):
        self.manager.preset_store.write("Sub IR", {
            "schema": "fxroute.dsp.preset", "version": 1,
            "chain": [{"id": "conv", "type": "convolver", "params": {"kernel": "sub"}}]})
        state = self.service.load()
        state["modes"]["stereo"]["banks"]["sub1"]["preset"] = "Sub IR"
        self.base = self.store.commit(state, expected_revision=state["revision"])
        self.service = OutputService(OutputServiceDeps(
            store=self.store, preset_loader=self.manager.preset_store.read,
            resolve_ir=resolver, measurement_active=lambda: True))
        self.before_bytes = self.path.read_bytes()
        self.owner = self.make_owner()

    async def test_stage_does_not_resolve_ir_again_after_transition(self):
        for fail_second in (True, False):
            with self.subTest(fail_second=fail_second):
                calls = []

                def resolve_ir(name):
                    calls.append(name)
                    if len(calls) > 1 and fail_second:
                        raise RuntimeError("IR disappeared after preparation")
                    return self.ir_info(len(calls))

                self.configure_ir(resolve_ir)
                self.owner.activate(48000)
                calls.clear()
                result = await self.owner.stage(self.proposal(sub_delays={"sub1": 4, "sub2": 0}))
                self.assertEqual(calls, ["sub"])
                self.assertEqual(result["expected_native_layout"], list(self.hardware.target.config.layout))
                self.assertEqual(result["expected_native_layout"][2]["oconv"]["path"],
                                 str(self.path.parent / "resolution-1.wav"))
                self.assertEqual(self.path.read_bytes(), self.before_bytes)

    async def test_layout_cache_preserves_initial_and_identical_targets(self):
        calls = []

        def resolve_ir(name):
            calls.append(name)
            return self.ir_info(len(calls))

        self.configure_ir(resolve_ir)
        self.owner.activate(48000)
        proposal = self.proposal(sub_delays={"sub1": 4, "sub2": 0})
        first = await self.owner.stage(proposal)
        first_target = self.hardware.target
        same = await self.owner.stage(proposal)
        self.assertIs(self.hardware.target, first_target)
        self.assertEqual(same["expected_native_layout"], first["expected_native_layout"])
        # An inactive identical graph must rebuild using the retained render.
        self.hardware.active = False

        async def reactivate(new, **kwargs):
            self.hardware.active = True
            await self.hardware.guarded_stage(new, **kwargs)

        with patch.object(self.owner._stager, "_guarded_stage", reactivate):
            rebuilt = await self.owner.stage(proposal)
        self.assertEqual(rebuilt["expected_native_layout"], list(self.hardware.target.config.layout))
        self.assertEqual(rebuilt["expected_native_layout"], first["expected_native_layout"])
        # Restore must use the initial render, not the latest matching fingerprint.
        restored = await self.owner.restore()
        self.assertEqual(restored["expected_native_layout"][2]["oconv"]["path"],
                         str(self.path.parent / "resolution-1.wav"))
        self.assertEqual(restored["expected_native_layout"], list(self.hardware.target.config.layout))
        restored["expected_native_layout"][2]["oconv"]["path"] = "tampered"
        repeated = await self.owner.restore()
        self.assertEqual(repeated["expected_native_layout"][2]["oconv"]["path"],
                         str(self.path.parent / "resolution-1.wav"))
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.path.read_bytes(), self.before_bytes)

    async def test_layout_cache_does_not_retain_candidate_history(self):
        retained_rows = []
        targets = []

        class LayoutRow(dict):
            def __deepcopy__(self, memo):
                detached = LayoutRow(copy.deepcopy(dict(self), memo))
                retained_rows.append(weakref.ref(detached))
                return detached

        def build_target(plan, *, fingerprint):
            target = self.build_target(plan, fingerprint=fingerprint)
            row = LayoutRow(target.config.layout[0])
            target.config.layout[0]["tracked"] = row
            targets.append(weakref.ref(target))
            return target

        self.owner = self.make_owner(build_target=build_target)
        self.owner.activate(48000)
        for delay in range(1, 21):
            result = await self.owner.stage(self.proposal(sub_delays={"sub1": delay, "sub2": 0}))
            self.hardware.transitions.clear()
            await asyncio.sleep(0)
        del result
        gc.collect()
        self.assertLessEqual(sum(ref() is not None for ref in targets), 3)
        self.assertLessEqual(sum(ref() is not None for ref in retained_rows), 3)

    async def test_ensure_ready_requires_activation(self):
        with self.assertRaises(RuntimeError):
            await self.owner.ensure_ready(48000)
        self.assertEqual(self.hardware.reads, 0)

    async def test_ensure_ready_success_is_read_only(self):
        self.owner.activate(48000)
        result = await self.owner.stage(self.proposal(sub_delays={"sub1": 4, "sub2": 0}))
        transitions = len(self.hardware.transitions)
        with patch.object(self.service, "compile_plan", side_effect=AssertionError("rebuild")):
            snapshot = await self.owner.ensure_ready(48000)
        self.assertTrue(snapshot["active"])
        self.assertEqual(snapshot["config"]["plan_fingerprint"], result["fingerprint"])
        self.assertEqual(snapshot["output_gain_db"], -12)
        self.assertEqual(len(self.hardware.transitions), transitions)
        self.assertEqual(self.path.read_bytes(), self.before_bytes)

    async def test_ensure_ready_checks_initial_fingerprint_without_staging(self):
        self.owner.activate(48000)
        snapshot = await self.owner.ensure_ready(48000)
        self.assertEqual(snapshot["config"]["plan_fingerprint"], self.initial.config.plan_fingerprint)

    async def test_ensure_ready_rejects_rate_mismatch(self):
        self.owner.activate(48000)
        with self.assertRaises(RuntimeError):
            await self.owner.ensure_ready(44100)
        self.hardware.rate_override = 44100
        with self.assertRaises(RuntimeError):
            await self.owner.ensure_ready(48000)

    async def test_ensure_ready_rejects_inactive_or_wrong_fingerprint(self):
        self.owner.activate(48000)
        self.hardware.active = False
        with self.assertRaises(RuntimeError):
            await self.owner.ensure_ready(48000)
        self.hardware.active = True
        self.hardware.fingerprint_override = "wrong"
        with self.assertRaises(RuntimeError):
            await self.owner.ensure_ready(48000)

    async def test_ensure_ready_revision_drift_before_readback(self):
        self.owner.activate(48000)
        self.newer_winner()
        with self.assertRaises(StateConflictError):
            await self.owner.ensure_ready(48000)
        self.assertEqual(self.hardware.reads, 0)
        self.assertEqual(self.hardware.transitions, [])

    async def test_ensure_ready_revision_drift_during_readback(self):
        self.owner.activate(48000)
        self.hardware.during_readback = self.newer_winner
        with self.assertRaises(StateConflictError):
            await self.owner.ensure_ready(48000)
        self.assertEqual(self.hardware.reads, 1)
        self.assertEqual(self.hardware.transitions, [])

    async def test_readiness_and_commit_reject_invalid_gains(self):
        proposal = self.proposal(sub_delays={"sub1": 4, "sub2": 0})
        self.owner.activate(48000)
        await self.owner.stage(proposal)
        for gain in (None, True, False, "-12", float("nan"), float("inf"),
                     -float("inf"), -81, 0.1, [], {}):
            self.hardware.gain = gain
            for operation in (self.owner.ensure_ready(48000), self.owner.commit_winner(proposal)):
                with self.subTest(gain=gain), self.assertRaisesRegex(RuntimeError, "gain"):
                    await operation
                self.assertEqual(self.path.read_bytes(), self.before_bytes)
                self.assertFalse(self.owner.committed)

    async def test_readiness_accepts_finite_gain_boundaries(self):
        self.owner.activate(48000)
        for gain in (-80, -80.0, -12.5, 0, 0.0):
            self.hardware.gain = gain
            self.assertEqual((await self.owner.ensure_ready(48000))["output_gain_db"], gain)

    async def test_commit_reads_runtime_again_and_rejects_drift(self):
        proposal = self.proposal(sub_delays={"sub1": 4, "sub2": 0})
        self.owner.activate(48000)
        await self.owner.stage(proposal)
        await self.owner.ensure_ready(48000)
        for field, value in (("active", False), ("fingerprint_override", "drift"),
                             ("rate_override", 44100)):
            original = getattr(self.hardware, field)
            setattr(self.hardware, field, value)
            reads = self.hardware.reads
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                await self.owner.commit_winner(proposal)
            self.assertGreater(self.hardware.reads, reads)
            self.assertEqual(self.path.read_bytes(), self.before_bytes)
            self.assertFalse(self.owner.committed)
            setattr(self.hardware, field, original)

    async def test_commit_rejects_revision_drift_during_readback(self):
        proposal = self.proposal(sub_delays={"sub1": 4, "sub2": 0})
        self.owner.activate(48000)
        await self.owner.stage(proposal)
        winner_bytes = []

        def drift():
            self.newer_winner()
            winner_bytes.append(self.path.read_bytes())

        self.hardware.during_readback = drift
        with self.assertRaises(StateConflictError):
            await self.owner.commit_winner(proposal)
        self.assertEqual(len(winner_bytes), 1)
        self.assertEqual(self.path.read_bytes(), winner_bytes[0])
        self.assertFalse(self.owner.committed)

    async def test_failed_restoration_does_not_authorize_old_winner_commit(self):
        from measurement.autosub.candidate_state import CandidateRestoreError

        fail_restore = False

        async def guarded_stage(new, **kwargs):
            if fail_restore:
                self.hardware.target = new
                self.hardware.active = False
                raise RuntimeError("hardware restoration failed")
            await self.hardware.guarded_stage(new, **kwargs)

        self.owner = self.make_owner(guarded_stage=guarded_stage)
        self.owner.activate(48000)
        proposal = self.proposal(sub_delays={"sub1": 4, "sub2": 0})
        await self.owner.stage(proposal)
        fail_restore = True
        with self.assertRaises(CandidateRestoreError):
            await self.owner.restore()
        with self.assertRaises(RuntimeError):
            await self.owner.commit_winner(proposal)
        self.assertEqual(self.path.read_bytes(), self.before_bytes)
        self.assertFalse(self.owner.committed)

    async def test_proposal_subclass_cannot_bypass_complete_map_validation(self):
        class IncompleteProposal(session_module.AutoSubProposal):
            def validated_knobs(self, sub_roles):
                return {}

        proposal = IncompleteProposal({}, {}, {}, {})
        self.owner.activate(48000)
        with self.assertRaises(ValueError):
            await self.owner.stage(proposal)
        await self.owner.stage(self.proposal())
        with self.assertRaises(ValueError):
            await self.owner.commit_winner(proposal)
        self.assertEqual(self.path.read_bytes(), self.before_bytes)
        self.assertFalse(self.owner.committed)

    async def test_cancelled_stage_holds_owner_lock_until_recovery_then_queued_commit(self):
        entered = asyncio.Event()
        recovering = asyncio.Event()
        release_recovery = asyncio.Event()
        queued = asyncio.Event()
        interrupted = False
        transition_count = 0

        async def guarded_stage(new, **kwargs):
            nonlocal transition_count
            if interrupted:
                transition_count += 1
                if transition_count == 1:
                    self.hardware.target = new
                    self.hardware.gain = kwargs["guard_db"]
                    entered.set()
                    await asyncio.Future()
                recovering.set()
                await release_recovery.wait()
            await self.hardware.guarded_stage(new, **kwargs)

        self.owner = self.make_owner(guarded_stage=guarded_stage)
        self.owner.activate(48000)
        winner = self.proposal(sub_delays={"sub1": 4, "sub2": 0})
        await self.owner.stage(winner)
        interrupted = True
        staging = asyncio.create_task(self.owner.stage(
            self.proposal(sub_delays={"sub1": 8, "sub2": 0})))
        await asyncio.wait_for(entered.wait(), 1)
        staging.cancel()
        await asyncio.wait_for(recovering.wait(), 1)

        async def commit():
            queued.set()
            return await self.owner.commit_winner(winner)

        committing = asyncio.create_task(commit())
        try:
            await asyncio.wait_for(queued.wait(), 1)
            reads = self.hardware.reads
            staging.cancel()
            await asyncio.sleep(0)
            self.assertFalse(staging.done())
            self.assertFalse(committing.done())
            self.assertEqual(self.hardware.reads, reads)
            self.assertEqual(self.path.read_bytes(), self.before_bytes)
        finally:
            release_recovery.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(staging, 1)
        committed = await asyncio.wait_for(committing, 1)
        self.assertEqual(committed["modes"]["stereo"]["processing"]["sub1"]["alignment_ms"], 4)
        self.assertEqual(self.hardware.gain, -12)
        self.assertTrue(self.owner.committed)

    async def test_commit_holds_owner_lock_across_readback_and_retires_queued_restore(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        queued = asyncio.Event()
        block = False

        async def readback():
            if block:
                entered.set()
                await release.wait()
            return await self.hardware.readback()

        self.owner = self.make_owner(readback=readback)
        self.owner.activate(48000)
        proposal = self.proposal(sub_delays={"sub1": 4, "sub2": 0})
        await self.owner.stage(proposal)
        transitions = len(self.hardware.transitions)
        block = True
        committing = asyncio.create_task(self.owner.commit_winner(proposal))
        await asyncio.wait_for(entered.wait(), 1)

        async def restore():
            queued.set()
            return await self.owner.restore()

        restoring = asyncio.create_task(restore())
        try:
            await asyncio.wait_for(queued.wait(), 1)
            self.assertFalse(restoring.done())
            self.assertEqual(len(self.hardware.transitions), transitions)
            self.assertEqual(self.path.read_bytes(), self.before_bytes)
        finally:
            release.set()
        await asyncio.wait_for(committing, 1)
        with self.assertRaisesRegex(RuntimeError, "retired"):
            await asyncio.wait_for(restoring, 1)
        self.assertEqual(len(self.hardware.transitions), transitions)
        self.assertEqual(self.service.load()["revision"], self.base["revision"] + 1)

    async def test_cancelled_readiness_releases_lock_for_queued_stage(self):
        entered = asyncio.Event()
        queued = asyncio.Event()
        block_once = True

        async def readback():
            nonlocal block_once
            if block_once:
                block_once = False
                entered.set()
                await asyncio.Future()
            return await self.hardware.readback()

        self.owner = self.make_owner(readback=readback)
        self.owner.activate(48000)
        checking = asyncio.create_task(self.owner.ensure_ready(48000))
        await asyncio.wait_for(entered.wait(), 1)

        async def stage():
            queued.set()
            return await self.owner.stage(self.proposal(sub_delays={"sub1": 4, "sub2": 0}))

        staging = asyncio.create_task(stage())
        await asyncio.wait_for(queued.wait(), 1)
        self.assertFalse(staging.done())
        self.assertEqual(self.hardware.transitions, [])
        checking.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(checking, 1)
        result = await asyncio.wait_for(staging, 1)
        self.assertEqual(result["state"]["modes"]["stereo"]["processing"]["sub1"]["alignment_ms"], 4)
        self.assertEqual(self.path.read_bytes(), self.before_bytes)

    async def test_commit_winner_persists_staged_normalized_state(self):
        self.owner.activate(48000)
        proposal = self.proposal(sub_delays={"sub1": 4, "sub2": 0})
        staged = await self.owner.stage(proposal)
        self.assertEqual(self.path.read_bytes(), self.before_bytes)
        committed = await self.owner.commit_winner(
            self.proposal(sub_delays={"sub1": 4.0, "sub2": 0.0}))
        self.assertEqual(committed["revision"], self.base["revision"] + 1)
        expected = copy.deepcopy(staged["state"])
        expected["revision"] += 1
        self.assertEqual(committed, expected)
        self.assertEqual(self.service.load(), committed)
        self.assertTrue(self.owner.committed)
        self.assertEqual(len(self.hardware.transitions), 1)

    async def test_commit_winner_rejects_each_mismatched_proposal_field(self):
        self.owner.activate(48000)
        await self.owner.stage(self.proposal())
        for fields in ({"sub_delays": {"sub1": 2, "sub2": 0}},
                       {"sub_levels": {"sub1": -2, "sub2": 0}},
                       {"sub_polarities": {"sub1": "invert", "sub2": "normal"}},
                       {"bass": {"frequency_hz": 90, "main_highpass_enabled": True}}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                await self.owner.commit_winner(self.proposal(**fields))
        self.assertEqual(self.path.read_bytes(), self.before_bytes)
        self.assertFalse(self.owner.committed)

    async def test_commit_requires_a_successfully_staged_proposal(self):
        for activated in (False, True):
            if activated:
                self.owner.activate(48000)
            with self.assertRaises(RuntimeError):
                await self.owner.commit_winner(self.proposal())
        self.assertEqual(self.path.read_bytes(), self.before_bytes)

    async def test_committed_owner_rejects_stage_restore_and_second_commit(self):
        self.owner.activate(48000)
        await self.owner.stage(self.proposal())
        await self.owner.commit_winner(self.proposal())
        transitions = len(self.hardware.transitions)
        for operation in (self.owner.stage(self.proposal()), self.owner.restore(),
                          self.owner.commit_winner(self.proposal())):
            with self.assertRaises(RuntimeError):
                await operation
        self.assertEqual(self.service.load()["revision"], 2)
        self.assertEqual(len(self.hardware.transitions), transitions)

    async def test_inert_restore_is_noop_even_after_revision_drift(self):
        self.newer_winner()
        with patch.object(self.service, "load", side_effect=AssertionError("load")):
            self.assertIsNone(await self.owner.restore())
        self.assertEqual(self.hardware.transitions, [])
        self.assertEqual(self.hardware.reads, 0)

    async def test_activated_restore_returns_to_start_fingerprint(self):
        self.owner.activate(48000)
        await self.owner.stage(self.proposal(sub_delays={"sub1": 4, "sub2": 0}))
        result = await self.owner.restore()
        self.assertEqual(result["fingerprint"], self.initial.config.plan_fingerprint)
        snapshot = await self.owner.ensure_ready(48000)
        self.assertEqual(snapshot["config"]["plan_fingerprint"], self.initial.config.plan_fingerprint)
        self.assertEqual(self.path.read_bytes(), self.before_bytes)
        with self.assertRaises(ValueError):
            await self.owner.commit_winner(self.proposal(sub_delays={"sub1": 4, "sub2": 0}))

    async def test_stale_revision_vetoes_stage_restore_and_commit(self):
        self.owner.activate(48000)
        await self.owner.stage(self.proposal())
        winner = self.newer_winner()
        target = self.hardware.target
        transitions = len(self.hardware.transitions)
        for operation in (self.owner.stage(self.proposal()), self.owner.restore(),
                          self.owner.commit_winner(self.proposal())):
            with self.assertRaises(StateConflictError):
                await operation
        self.assertEqual(self.service.load(), winner)
        self.assertIs(self.hardware.target, target)
        self.assertEqual(len(self.hardware.transitions), transitions)


if __name__ == "__main__":
    unittest.main(verbosity=2)
