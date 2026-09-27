#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Speaker commit sessions persist only acoustically confirmed candidates."""

import asyncio
import copy
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import speaker_take_test_support as takes
from audio.output_service import OutputService, OutputServiceDeps
from audio.output_state import default_output_state, set_mode_routing, switch_mode, validate_output_state, set_crossover
from audio.output_state_store import OutputStateStore, StateConflictError
from dsp.manager import DSPManager
from dsp.runtime import DSPRuntimeConfig, PlannedSyncTarget
from measurement.speaker_align import SpeakerAlignment
from measurement.speaker_commit import (
    SpeakerAlignSession,
    create_speaker_release_adapter,
    require_speaker_candidate,
)
from measurement.target import freeze_measurement_target

RATE = 48000


def crossover_state():
    state = default_output_state()
    routes = [f"{side}_{way}" for side in ("right", "left") for way in ("high", "low")]
    routes += ["sub1", "left_low"]
    state = set_mode_routing(set_crossover(state, "stereo-sub", True), "stereo-sub", "dev", routes)
    state = switch_mode(state, "stereo-sub")
    processing = state["modes"]["stereo-sub"]["processing"]
    for side in ("left", "right"):
        processing[f"{side}_low"]["lowpass"] = {
            "family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 2000,
        }
        processing[f"{side}_high"]["highpass"] = {
            "family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 2000,
        }
    return validate_output_state(state)


def planning_from(alignment, arrivals):
    """One shared planning take at the given per-way sample offsets."""
    roles = [request["role"] for request in alignment.capture_requests()]
    return takes.planning_document(
        alignment, {role: arrivals[index] * 1000.0 / RATE for index, role in enumerate(roles)})


def confirmation_from(alignment, arrivals):
    """One shared verification take at the given per-way sample offsets."""
    roles = [request["role"] for request in alignment.capture_requests()]
    return takes.confirmation_document(
        alignment, {role: arrivals[index] * 1000.0 / RATE for index, role in enumerate(roles)})


def lowpass_kernel(cutoff):
    offsets = np.arange(-256, 257, dtype=float)
    sigma = np.sqrt(2 * np.log(2)) * RATE / (2 * np.pi * cutoff)
    kernel = np.exp(-0.5 * (offsets / sigma) ** 2)
    return kernel / np.sum(kernel)


def captures_for(alignment, arrivals):
    from measurement.target import REFERENCE_TAP_INGRESS
    delta = np.zeros(513)
    delta[256] = 1
    lowpass = lowpass_kernel(2000)
    boundaries = [np.zeros(513), lowpass, delta]
    captures = []
    for index, request in enumerate(alignment.capture_requests()):
        reference = 700 + index * 131
        arrival = reference + arrivals[index]
        ir = np.zeros(4096)
        ir[arrival - 256:arrival + 257] = boundaries[index + 1] - boundaries[index]
        captures.append({
            "role": request["role"],
            "measurement_target": request["measurement_target"],
            "reference_id": request["reference_id"],
            "microphone_position_id": request["microphone_position_id"],
            "reference_tap": REFERENCE_TAP_INGRESS,
            "time_reference": "deconvolved-sweep-origin",
            "impulse_response": ir,
            "analysis": {
                "sample_rate": RATE, "peak_dbfs": -12.0,
                "quality_checks": {"status": "pass", "items": []},
                "reference_path": {
                    "usable": True, "electrical_reference_used": True,
                    "timing_status": "electrical-reference", "stability": "stable",
                    "confidence": 0.95, "clipped": False, "peak_dbfs": -9.0,
                },
                "impulse_response": {
                    "direct_arrival_index": arrival, "reference_peak_index": reference,
                    "direct_confidence": 0.95,
                    "timing_source": "direct_arrival_minus_reference_peak",
                },
            },
        })
    return captures


class SessionFixture:
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="speaker-commit-")
        self.addCleanup(directory.cleanup)
        home = Path(directory.name)
        self.manager = DSPManager(home=home / "dsp")
        self.manager.preset_store.write("Room", {
            "schema": "fxroute.dsp.preset", "version": 1, "chain": []})
        self.store = OutputStateStore(home / "output-state.json")
        self.service = OutputService(OutputServiceDeps(
            store=self.store, preset_loader=self.manager.preset_store.read,
            resolve_ir=lambda name: (_ for _ in ()).throw(
                AssertionError(f"fixture has no IR presets: {name}")),
            measurement_active=lambda: False))
        base = crossover_state()
        for config in base["modes"].values():
            for bank in config["banks"].values():
                bank.update(preset="Room", preset_a="Neutral", preset_b="Room")
        self.base = self.store.commit(base, expected_revision=0)
        self.context = dict(output_key="dev", channels=6, sample_rate_hz=RATE)
        # Fake runtime: the "graph" is just the staged fingerprint + gain.
        self.runtime = {"fingerprint": None, "gain_db": 0.0, "links_ok": True}
        self.stage_calls = []
        self.alignment = SpeakerAlignment(
            self.base, side="left", output_key="dev", channels=6, sample_rate_hz=RATE,
            fingerprint="frozen-plan", reference_id="interface:input-2:upstream",
            microphone_position_id="seat-1-fixed",
        )
        self.live = freeze_measurement_target(
            self.base, bank_id="global", output_key="dev", channels=6,
            sample_rate_hz=RATE, fingerprint="frozen-plan",
        )
        self.proposal = self.alignment.propose(
            captures_for(self.alignment, (96, 240)),
            planning=planning_from(self.alignment, (96, 240)), live_target=self.live)

    def build_target(self, plan, *, fingerprint):
        layout = self.service.compile_layout(plan)
        config = DSPRuntimeConfig.from_plan(
            plan, layout=layout, output_key="dev", sample_rate_hz=RATE,
            hardware_ports=[f"playback_AUX{i}" for i in range(6)],
            plan_fingerprint=fingerprint)
        text = self.manager.compile_engine_text(
            layout, preset_name=plan["global"]["preset"], sample_rate_hz=RATE,
            extras_override=plan["global"]["extras"])
        return PlannedSyncTarget(config, text)

    async def guarded_stage(self, new, *, previous, guard_db, apply_candidate,
                            apply_previous, before_ramp=None,
                            before_rollback_ramp=None, ramp_target_db=0.0,
                            rollback_ramp_target_db=0.0):
        self.stage_calls.append(new.config.plan_fingerprint)
        apply_candidate()
        self.runtime["fingerprint"] = new.config.plan_fingerprint
        if before_ramp is not None:
            await before_ramp()
        self.runtime["gain_db"] = float(ramp_target_db)

    async def readback(self):
        return {"active": True,
                "config": {"plan_fingerprint": self.runtime["fingerprint"],
                           "sample_rate": RATE},
                "output_gain_db": self.runtime["gain_db"]}

    def session(self, **overrides):
        options = dict(service=self.service, start_state=self.base,
                       build_target=self.build_target,
                       guarded_stage=self.guarded_stage, readback=self.readback,
                       guard_db=-30.0, **self.context)
        options.update(overrides)
        return SpeakerAlignSession(**options)


class RequireCandidateTests(SessionFixture, unittest.TestCase):
    def test_proposal_candidate_is_accepted(self):
        result = require_speaker_candidate(
            self.base, self.proposal["candidate_state"],
            output_key="dev", channels=6)
        self.assertEqual(result["revision"], self.base["revision"])

    def test_non_alignment_change_is_rejected(self):
        tampered = copy.deepcopy(self.proposal["candidate_state"])
        tampered["modes"]["stereo-sub"]["processing"]["left_low"]["polarity"] = "invert"
        with self.assertRaisesRegex(ValueError, "alignment"):
            require_speaker_candidate(self.base, tampered, output_key="dev", channels=6)

    def test_gain_change_is_accepted(self):
        tampered = copy.deepcopy(self.proposal["candidate_state"])
        tampered["modes"]["stereo-sub"]["processing"]["left_low"]["level_db"] += 1.0
        result = require_speaker_candidate(self.base, tampered, output_key="dev", channels=6)
        self.assertEqual(result["modes"]["stereo-sub"]["processing"]["left_low"]["level_db"],
                         self.proposal["candidate_state"]["modes"]["stereo-sub"]["processing"]["left_low"]["level_db"] + 1.0)

    def test_bank_change_is_rejected(self):
        tampered = copy.deepcopy(self.proposal["candidate_state"])
        tampered["modes"]["stereo-sub"]["banks"]["left_low"]["preset"] = "Direct"
        with self.assertRaisesRegex(ValueError, "alignment"):
            require_speaker_candidate(self.base, tampered, output_key="dev", channels=6)

    def test_rebased_candidate_is_rejected(self):
        tampered = copy.deepcopy(self.proposal["candidate_state"])
        tampered["revision"] += 1
        with self.assertRaisesRegex(ValueError, "revision"):
            require_speaker_candidate(self.base, tampered, output_key="dev", channels=6)

    def test_equal_to_start_is_accepted(self):
        result = require_speaker_candidate(
            self.base, copy.deepcopy(self.base), output_key="dev", channels=6)
        self.assertEqual(result, self.base)

    def test_dormant_roles_carry_exactly_the_common_rebase_offset(self):
        """Main roles dormant under the crossover move with a rebase, never apart."""
        rebased = copy.deepcopy(self.proposal["candidate_state"])
        for settings in rebased["modes"]["stereo-sub"]["processing"].values():
            settings["alignment_ms"] -= 2.0
        require_speaker_candidate(self.base, rebased, output_key="dev", channels=6)
        for label, candidate, role, key, change in (
            ("left behind by the rebase", rebased, "main_l", "alignment_ms", 2.0),
            ("moved apart from the rebase", rebased, "main_r", "alignment_ms", -0.5),
            ("moved without a rebase", self.proposal["candidate_state"], "main_l",
             "alignment_ms", -2.0),
            ("gain changed", rebased, "main_r", "level_db", 1.0),
        ):
            with self.subTest(label):
                tampered = copy.deepcopy(candidate)
                tampered["modes"]["stereo-sub"]["processing"][role][key] += change
                with self.assertRaisesRegex(ValueError, f"unrouted roles apart.*{role}"):
                    require_speaker_candidate(self.base, tampered, output_key="dev", channels=6)


class StageRestoreTests(SessionFixture, unittest.IsolatedAsyncioTestCase):
    async def test_stage_then_restore_roundtrip(self):
        from measurement.speaker_commit import compile_speaker_candidate
        session = self.session()
        expected, _ = compile_speaker_candidate(
            self.proposal["candidate_state"], service=self.service, **self.context)
        staged = await session.stage_candidate(self.proposal["candidate_state"])
        self.assertEqual(staged["fingerprint"], expected)
        self.assertEqual(self.runtime["fingerprint"], staged["fingerprint"])
        restored = await session.restore_start()
        start_fingerprint, _ = self.compile_start()
        self.assertEqual(restored["fingerprint"], start_fingerprint)
        self.assertEqual(self.runtime["fingerprint"], start_fingerprint)

    def compile_start(self):
        from measurement.speaker_commit import compile_speaker_candidate
        return compile_speaker_candidate(self.base, service=self.service, **self.context)

    async def test_stage_rejects_non_candidate(self):
        session = self.session()
        tampered = copy.deepcopy(self.proposal["candidate_state"])
        tampered["modes"]["stereo-sub"]["processing"]["left_low"]["polarity"] = "invert"
        with self.assertRaisesRegex(ValueError, "alignment"):
            await session.stage_candidate(tampered)
        self.assertEqual(self.stage_calls, [])
        self.assertIsNone(self.runtime["fingerprint"])

    async def test_stage_failure_restores_previous_rendering(self):
        from measurement.speaker_commit import SpeakerAlignRestoreError

        async def failing_stage(**kwargs):
            raise RuntimeError("rebuild failed")

        session = self.session(guarded_stage=failing_stage)
        # Recovery re-attempts the rebuild and fails too: the restore error
        # surfaces (mirroring the AutoSub stager), never a silent success.
        with self.assertRaises(SpeakerAlignRestoreError):
            await session.stage_candidate(self.proposal["candidate_state"])
        self.assertIsNone(self.runtime["fingerprint"])

    async def test_stale_revision_refuses_to_touch_runtime(self):
        session = self.session()
        self.store.commit(copy.deepcopy(self.base), expected_revision=self.base["revision"])
        with self.assertRaises(StateConflictError):
            await session.stage_candidate(self.proposal["candidate_state"])
        self.assertEqual(self.stage_calls, [])
        self.assertIsNone(self.runtime["fingerprint"])

    async def test_unverified_stage_is_rolled_back(self):
        calls = {"n": 0}
        real_stage = self.guarded_stage

        async def lying_stage(new, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                # First transition reports the wrong graph; recovery retries
                # honestly and must restore the verified start rendering.
                kwargs["apply_candidate"]()
                self.runtime["fingerprint"] = "some-other-fingerprint"
                return
            await real_stage(new, **kwargs)

        session = self.session(guarded_stage=lying_stage)
        with self.assertRaisesRegex(RuntimeError, "readback"):
            await session.stage_candidate(self.proposal["candidate_state"])
        start_fingerprint, _ = self.compile_start()
        self.assertEqual(self.runtime["fingerprint"], start_fingerprint)


class CommitTests(SessionFixture, unittest.IsolatedAsyncioTestCase):
    async def test_commit_confirmed_candidate_persists(self):
        session = self.session()
        await session.stage_candidate(self.proposal["candidate_state"])
        committed = await session.commit_candidate(self.proposal["candidate_state"])
        self.assertEqual(committed["revision"], self.base["revision"] + 1)
        self.assertEqual(
            committed["modes"]["stereo-sub"]["processing"]["left_low"]["alignment_ms"],
            self.proposal["candidate_state"]["modes"]["stereo-sub"]["processing"]["left_low"]["alignment_ms"])
        self.assertTrue(session.committed)
        # A committed session never restores the old start over the winner.
        with self.assertRaisesRegex(RuntimeError, "committed"):
            await session.restore_start()

    async def test_commit_without_stage_is_rejected(self):
        session = self.session()
        with self.assertRaisesRegex(RuntimeError, "staged"):
            await session.commit_candidate(self.proposal["candidate_state"])
        self.assertEqual(self.service.load()["revision"], self.base["revision"])

    async def test_commit_different_candidate_is_rejected(self):
        session = self.session()
        await session.stage_candidate(self.proposal["candidate_state"])
        other = copy.deepcopy(self.proposal["candidate_state"])
        other["modes"]["stereo-sub"]["processing"]["left_low"]["alignment_ms"] += 0.5
        with self.assertRaisesRegex(ValueError, "staged"):
            await session.commit_candidate(other)
        self.assertEqual(self.service.load()["revision"], self.base["revision"])

    async def test_equal_to_start_commit_is_verified_noop(self):
        session = self.session()
        # Render the start graph first so readback agrees with the no-op.
        await session.stage_candidate(copy.deepcopy(self.base))
        committed = await session.commit_candidate(copy.deepcopy(self.base))
        self.assertEqual(committed["revision"], self.base["revision"])
        self.assertTrue(session.committed)

    async def test_cancel_vetoes_persistence(self):
        session = self.session()
        await session.stage_candidate(self.proposal["candidate_state"])
        with self.assertRaisesRegex(RuntimeError, "cancellation"):
            await session.commit_candidate(
                self.proposal["candidate_state"], cancel_requested=lambda: True)
        self.assertEqual(self.service.load()["revision"], self.base["revision"])
        self.assertFalse(session.committed)

    async def test_stale_revision_vetoes_commit(self):
        session = self.session()
        await session.stage_candidate(self.proposal["candidate_state"])
        self.store.commit(copy.deepcopy(self.base), expected_revision=self.base["revision"])
        with self.assertRaises(StateConflictError):
            await session.commit_candidate(self.proposal["candidate_state"])
        self.assertFalse(session.committed)


class TrialCommitFlowTests(SessionFixture, unittest.IsolatedAsyncioTestCase):
    async def test_confirm_and_commit_end_to_end(self):
        from measurement.speaker_apply import apply_and_confirm

        session = self.session()
        async def confirm():
            return confirmation_from(self.alignment, (500, 500))

        result = await session.confirm_and_commit(
            confirm=confirm, proposal=self.proposal, live_target=self.live)
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["committed"]["revision"], self.base["revision"] + 1)
        self.assertTrue(session.committed)

    async def test_unconfirmed_trial_never_commits(self):
        session = self.session()
        async def confirm():
            return confirmation_from(self.alignment, (500, 548))

        result = await session.confirm_and_commit(
            confirm=confirm, proposal=self.proposal, live_target=self.live)
        self.assertFalse(result["confirmed"])
        self.assertNotIn("committed", result)
        self.assertEqual(self.service.load()["revision"], self.base["revision"])

    async def test_acquire_timeout_restores_and_reraises(self):
        session = self.session()
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        async def hanging_confirm():
            loop.call_soon_threadsafe(entered.set)
            await asyncio.to_thread(release.wait, 10)
            return {}

        worker = asyncio.create_task(session.confirm_and_commit(
            confirm=hanging_confirm, proposal=self.proposal,
            live_target=self.live, acquire_timeout_seconds=0.05))
        try:
            async with asyncio.timeout(10):
                await entered.wait()
            with self.assertRaises(TimeoutError):
                await worker
        finally:
            release.set()
            await asyncio.gather(worker, return_exceptions=True)
        self.assertEqual(self.service.load()["revision"], self.base["revision"])
        start_fingerprint, _ = self.compile_start()
        self.assertEqual(self.runtime["fingerprint"], start_fingerprint)

    def compile_start(self):
        from measurement.speaker_commit import compile_speaker_candidate
        return compile_speaker_candidate(self.base, service=self.service, **self.context)

    async def test_stale_live_target_fails_before_stage(self):
        session = self.session()
        stale = copy.deepcopy(self.live)
        stale["revision"] += 1

        async def confirm():
            self.fail("stale envelope must not reach the confirmation take")

        with self.assertRaisesRegex(ValueError, "stale"):
            await session.confirm_and_commit(
                confirm=confirm, proposal=self.proposal, live_target=stale)
        self.assertEqual(self.stage_calls, [])
        self.assertIsNone(self.runtime["fingerprint"])
        self.assertFalse(session.committed)

    async def test_malformed_proposal_fails_before_stage(self):
        session = self.session()

        async def confirm():
            self.fail("malformed envelope must not reach the confirmation take")

        with self.assertRaisesRegex(ValueError, "proposal"):
            await session.confirm_and_commit(
                confirm=confirm, proposal="not-a-proposal", live_target=self.live)
        self.assertEqual(self.stage_calls, [])

    async def test_concurrent_sessions_commit_only_once(self):
        # Graph ownership across sessions belongs to the caller (documented);
        # the store revision still guarantees exactly one winner commit.
        first_session = self.session()
        second_session = self.session()
        order = []

        async def slow_confirm():
            order.append("acquire-start")
            await asyncio.sleep(0.05)
            order.append("acquire-done")
            return confirmation_from(self.alignment, (500, 500))

        first, second = await asyncio.gather(
            first_session.confirm_and_commit(
                confirm=slow_confirm, proposal=self.proposal, live_target=self.live),
            second_session.confirm_and_commit(
                confirm=slow_confirm, proposal=self.proposal, live_target=self.live),
            return_exceptions=True)
        revisions = sorted(
            result["committed"]["revision"] for result in (first, second)
            if not isinstance(result, BaseException))
        self.assertEqual(revisions, [self.base["revision"] + 1])
        self.assertTrue(any(isinstance(result, StateConflictError)
                            for result in (first, second)))

    async def test_committed_session_starts_no_second_trial(self):
        session = self.session()

        async def aligned_confirm():
            return confirmation_from(self.alignment, (500, 500))

        first = await session.confirm_and_commit(
            confirm=aligned_confirm, proposal=self.proposal, live_target=self.live)
        self.assertTrue(first["confirmed"])
        calls = []

        async def counting_confirm():
            calls.append("confirm")
            return confirmation_from(self.alignment, (500, 500))

        with self.assertRaisesRegex(RuntimeError, "committed"):
            await session.confirm_and_commit(
                confirm=counting_confirm, proposal=self.proposal, live_target=self.live)
        self.assertEqual(calls, [])
        self.assertEqual(self.service.load()["revision"], self.base["revision"] + 1)


class FailedCommitRestoreTests(SessionFixture, unittest.IsolatedAsyncioTestCase):
    """A verified candidate that never reached disk must not stay audible."""

    def compile_start(self):
        from measurement.speaker_commit import compile_speaker_candidate
        return compile_speaker_candidate(self.base, service=self.service, **self.context)[0]

    def compile_candidate(self):
        from measurement.speaker_commit import compile_speaker_candidate
        return compile_speaker_candidate(
            self.proposal["candidate_state"], service=self.service, **self.context)[0]

    async def aligned(self):
        return confirmation_from(self.alignment, (500, 500))

    async def trial(self, session, **options):
        return await session.confirm_and_commit(
            confirm=self.aligned, proposal=self.proposal, live_target=self.live, **options)

    async def test_disk_failure_restores_the_start_rendering(self):
        session = self.session()

        def failing_commit(*args, **kwargs):
            raise OSError("disk full")

        self.service.commit = failing_commit
        with self.assertRaisesRegex(OSError, "disk full"):
            await self.trial(session)
        self.assertEqual(self.runtime["fingerprint"], self.compile_start())
        self.assertEqual(self.runtime["gain_db"], 0.0)
        self.assertEqual(self.store.load()["revision"], self.base["revision"])
        self.assertFalse(session.committed)
        # The session is spent: it neither stages nor restores again.
        with self.assertRaisesRegex(RuntimeError, "retired"):
            await session.restore_start()

    async def test_revision_conflict_at_commit_restores_the_start_rendering(self):
        session = self.session()
        original = self.service.commit

        def racing_commit(candidate, *, expected_revision):
            # Another writer lands between the final check and the store lock.
            other = copy.deepcopy(self.base)
            other["modes"]["stereo-sub"]["processing"]["right_low"]["level_db"] = -2.0
            self.store.commit(other, expected_revision=expected_revision)
            return original(candidate, expected_revision=expected_revision)

        self.service.commit = racing_commit
        with self.assertRaises(StateConflictError):
            await self.trial(session)
        self.assertEqual(self.runtime["fingerprint"], self.compile_start())
        self.assertEqual(self.store.load()["revision"], self.base["revision"] + 1)
        self.assertFalse(session.committed)

    async def test_revision_conflict_leaves_a_runtime_someone_else_rendered(self):
        session = self.session()
        original = self.service.commit

        def racing_commit(candidate, *, expected_revision):
            self.store.commit(copy.deepcopy(self.base), expected_revision=expected_revision)
            self.runtime["fingerprint"] = "other-writer-graph"
            return original(candidate, expected_revision=expected_revision)

        self.service.commit = racing_commit
        with self.assertRaises(StateConflictError):
            await self.trial(session)
        self.assertEqual(self.runtime["fingerprint"], "other-writer-graph")

    async def test_revision_conflict_keeps_a_candidate_the_new_head_renders(self):
        session = self.session()
        original = self.service.commit

        def racing_commit(candidate, *, expected_revision):
            self.store.commit(copy.deepcopy(candidate), expected_revision=expected_revision)
            return original(candidate, expected_revision=expected_revision)

        self.service.commit = racing_commit
        with self.assertRaises(StateConflictError):
            await self.trial(session)
        self.assertEqual(self.runtime["fingerprint"], self.compile_candidate())

    async def test_cancel_veto_at_commit_restores_the_start_rendering(self):
        session = self.session()
        probes = []

        def veto_at_commit():
            # The trial checks before and after its confirmation; the third
            # probe is the commit's own veto.
            probes.append(True)
            return len(probes) > 2

        with self.assertRaisesRegex(RuntimeError, "cancellation"):
            await self.trial(session, cancel_requested=veto_at_commit)
        self.assertEqual(len(probes), 3)
        self.assertEqual(self.runtime["fingerprint"], self.compile_start())
        self.assertEqual(self.store.load()["revision"], self.base["revision"])

    async def test_cancel_in_the_final_readback_restores_the_start_rendering(self):
        entered = asyncio.Event()
        gate = asyncio.Event()
        confirmed = False

        async def readback():
            nonlocal confirmed
            if confirmed and not entered.is_set():
                entered.set()
                await gate.wait()
            return await self.readback()

        async def confirm():
            nonlocal confirmed
            confirmed = True
            return confirmation_from(self.alignment, (500, 500))

        session = self.session(readback=readback)
        worker = asyncio.create_task(session.confirm_and_commit(
            confirm=confirm, proposal=self.proposal, live_target=self.live))
        async with asyncio.timeout(5):
            await entered.wait()
        self.assertEqual(self.runtime["fingerprint"], self.compile_candidate())
        worker.cancel()
        async with asyncio.timeout(5):
            await asyncio.gather(worker, return_exceptions=True)
        self.assertTrue(worker.cancelled())
        self.assertFalse(session.committed)
        self.assertEqual(self.store.load()["revision"], self.base["revision"])
        self.assertEqual(self.runtime["fingerprint"], self.compile_start())

    async def test_cancel_during_the_unconfirmed_restore_waits_for_it(self):
        start = self.compile_start()
        entered = asyncio.Event()
        gate = asyncio.Event()

        async def slow_restore(new, **kwargs):
            if new.config.plan_fingerprint == start and self.stage_calls:
                entered.set()
                await gate.wait()
            await self.guarded_stage(new, **kwargs)

        async def offset():
            return confirmation_from(self.alignment, (500, 548))

        session = self.session(guarded_stage=slow_restore)
        worker = asyncio.create_task(session.confirm_and_commit(
            confirm=offset, proposal=self.proposal, live_target=self.live))
        async with asyncio.timeout(5):
            await entered.wait()
        worker.cancel()
        await asyncio.sleep(0.05)
        self.assertFalse(worker.done())
        gate.set()
        async with asyncio.timeout(5):
            await asyncio.gather(worker, return_exceptions=True)
        self.assertTrue(worker.cancelled())
        self.assertEqual(self.runtime["fingerprint"], start)


    def broken_start_stage(self):
        """A guarded stage that cannot bring the start rendering back while on."""
        start = self.compile_start()
        broken = {"on": True}

        async def stage(new, **kwargs):
            if broken["on"] and new.config.plan_fingerprint == start:
                raise RuntimeError("engine lost the start graph")
            await self.guarded_stage(new, **kwargs)

        return stage, broken

    def assert_restore_noted(self, error):
        notes = getattr(error, "__notes__", [])
        self.assertTrue(any("Restoring the start rendering also failed" in note
                            and "engine lost the start graph" in note for note in notes), notes)

    async def test_failed_restore_keeps_the_commit_error_and_the_session_restorable(self):
        stage, broken = self.broken_start_stage()
        session = self.session(guarded_stage=stage)

        def failing_commit(*args, **kwargs):
            raise OSError("disk full")

        self.service.commit = failing_commit
        with self.assertLogs("common.run_to_completion", level="ERROR"):
            with self.assertRaisesRegex(OSError, "disk full") as raised:
                await self.trial(session)
        self.assert_restore_noted(raised.exception)
        # The verified candidate is still audible: the session must not retire.
        self.assertEqual(self.runtime["fingerprint"], self.compile_candidate())
        self.assertFalse(session.committed)
        with self.assertRaisesRegex(RuntimeError, "only restoring the start"):
            await session.stage_candidate(self.proposal["candidate_state"])
        with self.assertRaisesRegex(RuntimeError, "only restoring the start"):
            await session.commit_candidate(self.proposal["candidate_state"])
        broken["on"] = False
        await session.restore_start()
        self.assertEqual(self.runtime["fingerprint"], self.compile_start())
        with self.assertRaisesRegex(RuntimeError, "retired"):
            await session.restore_start()

    async def test_failed_restore_after_a_trial_error_keeps_the_trial_error(self):
        stage, _ = self.broken_start_stage()
        session = self.session(guarded_stage=stage)

        async def unplugged():
            raise RuntimeError("mic unplugged")

        with self.assertLogs("common.run_to_completion", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "mic unplugged") as raised:
                await session.confirm_and_commit(
                    confirm=unplugged, proposal=self.proposal, live_target=self.live)
        self.assert_restore_noted(raised.exception)
        self.assertFalse(session.committed)

    async def test_failed_restore_after_a_cancel_stays_a_cancel(self):
        stage, _ = self.broken_start_stage()
        session = self.session(guarded_stage=stage)

        async def cancelled():
            raise asyncio.CancelledError("stop")

        with self.assertLogs("common.run_to_completion", level="ERROR"):
            with self.assertRaises(asyncio.CancelledError) as raised:
                await session.confirm_and_commit(
                    confirm=cancelled, proposal=self.proposal, live_target=self.live)
        self.assert_restore_noted(raised.exception)


class ServiceRestoreOwnershipTests(SessionFixture, unittest.IsolatedAsyncioTestCase):
    """The job slot and measurement owner outlive every restore of the run."""

    async def run_cancelled_restore(self, *, dry_run):
        from contextlib import asynccontextmanager
        from measurement.speaker_service import SpeakerAlignService
        from measurement.speaker_commit import compile_speaker_candidate

        start, _ = compile_speaker_candidate(self.base, service=self.service, **self.context)
        entered = asyncio.Event()
        gate = asyncio.Event()
        restored = asyncio.Event()
        held = set()

        async def slow_restore(new, **kwargs):
            restoring = new.config.plan_fingerprint == start and self.runtime["fingerprint"] not in (None, start)
            if restoring:
                entered.set()
                await gate.wait()
            await self.guarded_stage(new, **kwargs)
            if restoring:
                restored.set()

        session = self.session(guarded_stage=slow_restore)

        @asynccontextmanager
        async def scope(job_id):
            held.add(job_id)
            try:
                yield
            finally:
                held.discard(job_id)

        async def acquire(alignment, **kwargs):
            return {"captures": captures_for(alignment, (96, 240)),
                    "planning": planning_from(alignment, (96, 240)), "provenance": {}}

        arrivals = (500, 500) if dry_run else (500, 548)

        async def confirm(alignment, **kwargs):
            return {"confirmation": confirmation_from(alignment, arrivals), "provenance": {}}

        service = SpeakerAlignService(
            get_state=self.service.load,
            describe=lambda state: {**self.context, "fingerprint": "frozen-plan"},
            acquire=acquire, confirm=confirm, create_session=lambda *a, **k: session,
            freeze_live=lambda state, **ctx: freeze_measurement_target(state, bank_id="global", **ctx),
            job_scope=scope)
        job_id = service.start("left", input_id="mic", reference_input_channel="2",
                               reference_id="interface:input-2:upstream",
                               microphone_position_id="seat-1-fixed", dry_run=dry_run)
        async with asyncio.timeout(5):
            await entered.wait()
        service.cancel(job_id)
        await asyncio.sleep(0.05)
        service.cancel(job_id)
        await asyncio.sleep(0.05)
        self.assertEqual(held, {job_id})
        self.assertTrue(service.active)
        self.assertEqual(service.status(job_id)["status"], "cancelling")
        gate.set()
        job = await service.wait_for(job_id, timeout_seconds=5)
        self.assertEqual(job["status"], "cancelled")
        self.assertTrue(restored.is_set())
        self.assertEqual(held, set())
        self.assertFalse(service.active)
        self.assertEqual(self.runtime["fingerprint"], start)
        self.assertEqual(self.store.load()["revision"], self.base["revision"])

    async def test_cancel_during_trial_restore_keeps_the_owner(self):
        await self.run_cancelled_restore(dry_run=True)

    async def test_cancel_during_unconfirmed_restore_keeps_the_owner(self):
        await self.run_cancelled_restore(dry_run=False)


class ReleaseAdapterTests(SessionFixture, unittest.IsolatedAsyncioTestCase):
    async def test_release_rebuilds_committed_plan(self):
        session = self.session()
        await session.stage_candidate(self.proposal["candidate_state"])
        await session.commit_candidate(self.proposal["candidate_state"])
        seen = {}

        class NativeRuntime:
            async def sync_rendered(self, target):
                seen["fingerprint"] = target.config.plan_fingerprint

        adapter = create_speaker_release_adapter(
            service=self.service, dsp_manager=self.manager,
            hardware_ports=[f"playback_AUX{i}" for i in range(6)],
            get_native_runtime=lambda: NativeRuntime(),
            output_key="dev", channels=6)
        result = await adapter(RATE)
        self.assertEqual(result["revision"], self.base["revision"] + 1)
        self.assertEqual(seen["fingerprint"], result["plan_fingerprint"])

    async def test_release_validates_device_context_eagerly(self):
        with self.assertRaisesRegex(ValueError, "playback ports"):
            create_speaker_release_adapter(
                service=self.service, dsp_manager=self.manager, hardware_ports=["only-one"],
                get_native_runtime=lambda: None, output_key="dev", channels=6)

    async def test_release_without_runtime_is_loud(self):
        adapter = create_speaker_release_adapter(
            service=self.service, dsp_manager=self.manager,
            hardware_ports=[f"playback_AUX{i}" for i in range(6)],
            get_native_runtime=lambda: None, output_key="dev", channels=6)
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            await adapter(RATE)


if __name__ == "__main__":
    unittest.main()
