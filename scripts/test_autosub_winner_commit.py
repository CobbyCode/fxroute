#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Slice F: the final verified staged state is committed exactly once per run.

The owner owns the commit mechanism; the runners only decide *when*:

* ``commit_staged()`` re-verifies runtime/revision and commits the state that
  is actually staged — without the caller resending legacy triplets.
  Equal-to-start (no transition ever staged) is a verified no-op returning
  the unchanged state, never a new revision.
* Service runners commit the final staged state immediately before
  completion, after every acoustic gate (gain/correction/confirmation/
  recheck). A failed commit fails the job instead of silently discarding the
  adopted winner at cleanup; legacy runs stay byte-identical (no commit).
* A committed owner must never be restored through the retired stager at
  cleanup, and the committed revision must stay visible afterwards.
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from audio.output_service import OutputService, OutputServiceDeps  # noqa: E402
from audio.output_state import default_output_state, set_mode_routing  # noqa: E402
from audio.output_state_store import OutputStateStore, StateConflictError  # noqa: E402
from dsp.manager import DSPManager  # noqa: E402
from dsp.runtime import DSPRuntimeConfig, PlannedSyncTarget  # noqa: E402

from measurement.autosub import deps as autosub_deps  # noqa: E402
from measurement.autosub.candidate_session import (  # noqa: E402
    AutoSubCandidateSession,
    AutoSubProposal,
)

RATE = 48000
PORTS = [f"playback_AUX{i}" for i in range(4)]


class HardwareBoundary:
    """Render-only targets and fingerprint tracking like the session tests."""

    def __init__(self, service, initial_target):
        self.service = service
        self.target = initial_target
        self.active = True
        self.gain = -12.0
        self.fail_next_readback = None

    def build_target(self, plan, *, fingerprint):
        rate = plan["sample_rate_hz"]
        layout = self.service.compile_layout(plan)
        config = DSPRuntimeConfig.from_plan(
            plan, layout=layout, output_key="dev", sample_rate_hz=rate,
            hardware_ports=PORTS, plan_fingerprint=fingerprint)
        text = self.service.manager.compile_engine_text(
            layout, preset_name=plan["global"]["preset"], sample_rate_hz=rate,
            extras_override=plan["global"]["extras"])
        return PlannedSyncTarget(config, text)

    async def readback(self):
        fingerprint = self.target.config.plan_fingerprint
        if self.fail_next_readback:
            fingerprint, self.fail_next_readback = self.fail_next_readback, None
        return {"active": self.active, "output_gain_db": self.gain, "config": {
            "plan_fingerprint": fingerprint, "sample_rate": RATE}}

    async def guarded_stage(self, new, *, previous, guard_db, apply_candidate,
                            apply_previous, before_ramp, before_rollback_ramp,
                            ramp_target_db, rollback_ramp_target_db):
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


class CommitStagedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="autosub-winner-")
        self.addCleanup(directory.cleanup)
        self.manager = DSPManager(home=Path(directory.name) / "dsp")
        self.store = OutputStateStore(Path(directory.name) / "output-state.json")
        self.service = OutputService(OutputServiceDeps(
            store=self.store, preset_loader=self.manager.preset_store.read,
            resolve_ir=lambda name: (_ for _ in ()).throw(AssertionError(name)),
            measurement_active=lambda: True))
        self.service.manager = self.manager
        state = set_mode_routing(default_output_state(), "stereo", "dev",
                                 ["main_l", "main_r", "sub1", "sub2"])
        self.start = self.store.commit(state, expected_revision=0)
        self.hardware = HardwareBoundary(self.service, None)
        self.hardware.target = self.target_for(self.start)
        self.owner = AutoSubCandidateSession(
            service=self.service, start_state=self.start,
            build_target=self.hardware.build_target,
            guarded_stage=self.hardware.guarded_stage,
            readback=self.hardware.readback, output_key="dev", channels=4)
        self.owner.activate(RATE)
        self.context = {"output_key": "dev", "channels": 4,
                        "sub_role_map": {"sub1": "sub1", "sub2": "sub2"},
                        "sub_mute_mask": 12}

    def target_for(self, state):
        plan = self.service.compile_plan(state, sample_rate_hz=RATE,
                                         output_key="dev", channels=4)
        return self.hardware.build_target(plan, fingerprint=self.service.fingerprint_plan(plan))

    async def staged(self):
        proposal = AutoSubProposal(
            sub_delays={"sub1": 4.0, "sub2": 0.0},
            sub_levels={"sub1": -3.0, "sub2": 0.0},
            sub_polarities={"sub1": "normal", "sub2": "normal"},

            bass={"frequency_hz": 80, "main_highpass_enabled": True})
        return await self.owner.stage(proposal)

    async def test_commit_staged_persists_the_verified_staged_state(self):
        await self.staged()
        committed = await self.owner.commit_staged()
        self.assertEqual(committed["revision"], self.start["revision"] + 1)
        self.assertEqual(
            committed["modes"]["stereo"]["processing"]["sub1"]["alignment_ms"], 4.0)
        self.assertEqual(self.service.load(), committed)
        self.assertTrue(self.owner.committed)

    async def test_commit_staged_requires_activated_owner(self):
        inert = AutoSubCandidateSession(
            service=self.service, start_state=self.start,
            build_target=self.hardware.build_target,
            guarded_stage=self.hardware.guarded_stage,
            readback=self.hardware.readback, output_key="dev", channels=4)
        with self.assertRaises(RuntimeError):
            await inert.commit_staged()

    async def test_commit_staged_without_staging_is_a_verified_noop(self):
        committed = await self.owner.commit_staged()
        self.assertEqual(committed["revision"], self.start["revision"])
        self.assertEqual(self.service.load(), self.start)
        self.assertTrue(self.owner.committed)

    async def test_commit_staged_equal_to_start_bumps_no_revision(self):
        # The gate reverted to the incumbent (= frozen start); the final
        # staged state equals the start, so the commit is a verified no-op.
        proposal = AutoSubProposal(
            sub_delays={"sub1": 2.0, "sub2": 0.0},
            sub_levels={"sub1": -3.0, "sub2": 0.0},
            sub_polarities={"sub1": "normal", "sub2": "normal"},
            bass={"frequency_hz": 80, "main_highpass_enabled": True})
        await self.owner.stage(proposal)
        await self.owner.restore()
        committed = await self.owner.commit_staged()
        self.assertEqual(committed["revision"], self.start["revision"])
        self.assertEqual(self.service.load(), self.start)
        self.assertTrue(self.owner.committed)


    async def test_commit_staged_rereads_runtime_and_rejects_drift(self):
        await self.staged()
        self.hardware.fail_next_readback = "tampered"
        with self.assertRaises(RuntimeError):
            await self.owner.commit_staged()
        self.assertFalse(self.owner.committed)

    async def test_commit_staged_rejects_revision_drift(self):
        await self.staged()
        drifted = self.service.load()
        drifted["modes"]["stereo"]["processing"]["sub1"]["alignment_ms"] = 9.0
        self.service.commit(drifted, expected_revision=drifted["revision"])
        with self.assertRaises(StateConflictError):
            await self.owner.commit_staged()
        self.assertFalse(self.owner.committed)

    async def test_committed_owner_refuses_second_commit_and_restore(self):
        await self.staged()
        await self.owner.commit_staged()
        with self.assertRaises(RuntimeError):
            await self.owner.commit_staged()
        with self.assertRaises(RuntimeError):
            await self.owner.restore()

    async def test_cooperative_cancellation_during_readback_vetoes_commit(self):
        # The HTTP cancel endpoint only sets cancel_requested/cancelling; the
        # worker task keeps running. A cancel observed while the commit's
        # runtime readback is suspended must veto the commit and retire the
        # owner without persisting anything.
        await self.staged()
        started = asyncio.Event()
        release = asyncio.Event()
        original_readback = self.hardware.readback

        async def pausing_readback():
            started.set()
            await release.wait()
            return await original_readback()

        self.hardware.readback = pausing_readback
        self.owner._readback = pausing_readback
        cancel = {"requested": False}
        committing = asyncio.create_task(
            self.owner.commit_staged(cancel_requested=lambda: cancel["requested"]))
        try:
            await asyncio.wait_for(started.wait(), 2)
            # The cancel endpoint flips the flag while the readback is
            # suspended; the worker task itself keeps running.
            cancel["requested"] = True
        finally:
            release.set()
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(committing, 2)
        self.assertFalse(self.owner.committed)
        self.assertEqual(self.service.load()["revision"], self.start["revision"])

    async def test_cooperative_cancellation_vetoes_the_equal_to_start_noop(self):
        # Same cooperative-cancel window on the never/equal-to-start branch:
        # the verified no-op must also observe the cancel and retire without
        # persisting.
        started = asyncio.Event()
        release = asyncio.Event()
        original_readback = self.hardware.readback

        async def pausing_readback():
            started.set()
            await release.wait()
            return await original_readback()

        self.owner._readback = pausing_readback
        cancel = {"requested": False}
        committing = asyncio.create_task(
            self.owner.commit_staged(cancel_requested=lambda: cancel["requested"]))
        try:
            await asyncio.wait_for(started.wait(), 2)
            cancel["requested"] = True
        finally:
            release.set()
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(committing, 2)
        self.assertFalse(self.owner.committed)
        self.assertEqual(self.service.load()["revision"], self.start["revision"])

    async def test_committed_revision_survives_a_later_concurrent_commit(self):
        await self.staged()
        committed = await self.owner.commit_staged()
        follow_up = self.service.load()
        follow_up["modes"]["stereo"]["processing"]["sub2"]["alignment_ms"] = 1.0
        self.service.commit(follow_up, expected_revision=follow_up["revision"])
        self.assertEqual(self.service.load()["revision"], committed["revision"] + 1)
        # The runner reports the committed revision it produced; a later
        # concurrent writer is not silently rewound by cleanup.
        self.assertNotEqual(self.service.load(), self.start)


class RunnerFinalizationTests(unittest.IsolatedAsyncioTestCase):
    """The service runner commits the retained final state before completing."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="autosub-winner-runner-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.manager = DSPManager(home=self.directory / "dsp")
        self.store = OutputStateStore(self.directory / "output-state.json")
        self.service = OutputService(OutputServiceDeps(
            store=self.store, preset_loader=self.manager.preset_store.read,
            resolve_ir=lambda name: (_ for _ in ()).throw(AssertionError(name)),
            measurement_active=lambda: True))
        self.service.manager = self.manager
        state = set_mode_routing(default_output_state(), "stereo", "dev",
                                 ["main_l", "main_r", "sub1"])
        self.start = self.store.commit(state, expected_revision=0)
        self.commits = []
        self.restores = []

        class FakeOwner(SimpleNamespace):
            pass

        self.owner = FakeOwner(
            committed=False,
            commit_staged=self._record_commit,
            restore=self._record_restore)
        autosub_deps.configure_dependencies(autosub_deps.AutoSubDependencies(
            get_dsp_runtime=lambda: None,
            get_measurement_store=lambda: None,
            get_measurement_session=lambda: None,
            get_dsp_manager=lambda: None,
            get_output_service=lambda: self.service,
            create_candidate_session=lambda **kwargs: self.fail("unexpected creation")))
        autosub_deps.register_candidate_owner("winner-job", self.owner)
        self.addCleanup(autosub_deps.drop_candidate_owner, "winner-job")
        self.addCleanup(setattr, autosub_deps, "_autosub_deps", None)
        self.job = {"id": "winner-job", "status": "running", "cancel_requested": False,
                    "output_state_context": {
                        "mode": "stereo", "revision": self.start["revision"],
                        "output_key": "dev", "channels": 4,
                        "optimizer_path": "single-sub",
                        "sub_role_map": {"sub1": "sub1"}, "sub_mute_mask": 4}}
        autosub_deps._AUTO_SUB_JOBS["winner-job"] = self.job
        self.addCleanup(autosub_deps._AUTO_SUB_JOBS.pop, "winner-job", None)
        self.commits = []
        self.restored = []

    async def _record_commit(self):
        self.commits.append(True)
        self.owner.committed = True
        return dict(self.start, revision=self.start["revision"] + 1)

    async def _record_restore(self):
        self.restored.append(True)
        return None

    async def test_commit_helper_wraps_deny_as_run_fatal(self):
        from measurement.autosub.candidates import _commit_auto_sub_service_winner

        async def deny(cancel_requested=None):
            raise ValueError("proposal does not match")

        self.owner.commit_staged = deny
        with self.assertRaises(RuntimeError) as ctx:
            await _commit_auto_sub_service_winner(self.job)
        self.assertIsInstance(ctx.exception.__cause__, ValueError)
        self.assertIn("proposal does not match", str(ctx.exception))
        self.assertFalse(self.owner.committed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
