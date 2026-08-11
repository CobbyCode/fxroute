#!/usr/bin/env python3

"""Cancellation and failure must share one safe abort contract.

The Coordinator drains every uncommitted transition through the same
cleanup contract whether it failed with a stage exception or was cancelled:
attenuate, pause, roll back the output-mode runtime, restore the previous
local/radio volume, abort the failed handoff (the adapter may physically
restore the previously committed source), then either restore the output
gate (recovered source) or latch a failure.  A cancellation arriving while
the failure cleanup is already draining must not interrupt it; the cleanup
runs to a terminal state and the cancellation wins as the caller control
flow.
"""

import asyncio
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from playback_transition import (
    PlaybackTransitionCoordinator,
    PlaybackTransitionFailure,
    TransitionRequest,
)


class FakeRuntime:
    """Coordinator runtime adapter with explicit gate/source state.

    Every stage can optionally block (block_stage) before its own mutation so
    a cancellation injected while that stage is in flight deterministically
    observes the previous stage's completed mutations, or raise
    (fail_stage) to drive the normal failure path at the same boundary.
    """

    def __init__(self, *, abort_result=None):
        self.muted = False
        self.easyeffects_muted = False
        self.rate = 44_100
        self.volume = 100
        self.paused = True
        self.playing = False
        self.staged = False
        self.events = []
        self.abort_calls = []
        self.pause_after_failure_calls = 0
        self.abort_result = abort_result
        self.block_stage = None
        self.block_entered = asyncio.Event()
        self.fail_stage = None
        self.arm_volume_zero_block = False
        self.volume_zero_entered = asyncio.Event()
        self.volume_zero_release = asyncio.Event()
        self.block_abort = False
        self.abort_entered = asyncio.Event()
        self.abort_release = asyncio.Event()

    async def _maybe_block(self, name):
        self.events.append(name)
        if self.block_stage == name:
            self.block_entered.set()
            await asyncio.Event().wait()
        if self.fail_stage == name:
            raise RuntimeError(f"simulated failure at {name}")

    async def read_hardware_mute(self):
        self.events.append(f"read-mute:{self.muted}")
        return self.muted

    async def set_hardware_mute(self, muted, transition_id):
        self.events.append(f"mute:{muted}")
        if muted is False:
            await self._maybe_block("gate-restore-mute")
        self.muted = bool(muted)

    async def read_sink_mute(self, sink_name):
        self.events.append(f"read-sink-mute:{self.easyeffects_muted}")
        return self.easyeffects_muted

    async def set_sink_mute(self, sink_name, muted, transition_id):
        self.easyeffects_muted = bool(muted)
        self.events.append(f"sink-mute:{self.easyeffects_muted}")

    async def read_transition_snapshot(self, request):
        self.events.append("snapshot")
        return {
            "active_rate": self.rate,
            "player": {
                "current_file": "/music/old.flac",
                "volume": 100,
                "playing": True,
                "paused": False,
            },
            "current_track": {"source": "local", "url": "/music/old.flac"},
        }

    async def quiet_old_source(self, request):
        await self._maybe_block("quiet")
        self.playing = False
        self.paused = True
        self.volume = 0

    async def resolve_target_rate(self, request):
        self.events.append("resolve-rate")
        return request.target_rate

    async def establish_target_rate(self, request):
        await self._maybe_block("rate")
        self.rate = request.target_rate

    async def establish_effects_and_helper(self, request):
        self.events.append("effects-helper-links")
        return {"dsp_reinitialized": True, "helper_rebuilt": True}

    async def prepare_target_source(self, request):
        await self._maybe_block("prepare")
        self.staged = True
        self.volume = 0
        self.paused = True
        self.playing = False

    async def start_target_source(self, request):
        await self._maybe_block("start")
        self.paused = not request.should_play
        self.playing = request.should_play

    async def reconcile_post_start_graph(self, request):
        self.events.append("post-start-reconcile")
        return {"graph_complete": True}

    async def verify_transition_graph(self, request):
        await self._maybe_block("graph-readback")
        return {"committed": True, "active_rate": self.rate}

    async def verify_committed_transition(self, request):
        await self._maybe_block("commit-readback")
        return {"committed": True, "active_rate": self.rate}

    async def stabilize_effects_after_rate_change(
        self, request, *, dsp_reinitialized=False
    ):
        self.events.append("dsp-stabilize")
        return {"stabilized": True, "active_rate": self.rate}

    async def set_source_volume(self, volume, transition_id):
        self.events.append(f"source-volume:{volume}")
        self.volume = volume
        if volume == 0 and self.arm_volume_zero_block:
            self.volume_zero_entered.set()
            await self.volume_zero_release.wait()

    async def pause_source_after_failure(self, request):
        self.pause_after_failure_calls += 1
        self.events.append("pause-after-failure")
        self.paused = True
        self.playing = False

    def target_source_staged(self, request):
        return self.staged

    async def abort_failed_transition(
        self, request, snapshot, *, target_staged, ensure_gate_closed=None
    ):
        self.abort_calls.append({"target_staged": target_staged})
        self.events.append("abort-failed-transition")
        if self.block_abort:
            self.abort_entered.set()
            await self.abort_release.wait()
        return self.abort_result


def request():
    return TransitionRequest(
        operation="play",
        source="local",
        target_rate=48_000,
        target_url="/music/target.flac",
        target_track={"source": "local", "url": "/music/target.flac"},
        should_play=True,
        rate_change=True,
        reload_source=True,
        detail="cancellation-contract",
    )


class CancellationCleanupContractTests(unittest.IsolatedAsyncioTestCase):
    def _coordinator(self, runtime):
        return PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

    async def test_cancellation_after_gate_close_cleanup_is_terminal(self):
        runtime = FakeRuntime()
        runtime.block_stage = "quiet"
        coordinator = self._coordinator(runtime)
        task = asyncio.create_task(coordinator.execute(request()))

        await runtime.block_entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertTrue(runtime.muted)
        self.assertTrue(coordinator.gate.closed)
        self.assertTrue(coordinator.gate.failure_latched)
        self.assertEqual(runtime.pause_after_failure_calls, 1)
        self.assertTrue(coordinator.last_error["cancelled"])
        self.assertTrue(coordinator.last_error["failure_latched"])
        self.assertEqual(coordinator.last_error["stage"], "quiet-old-source")
        self.assertFalse(coordinator.transition_active)

    async def test_cancellation_after_old_source_quiet_still_aborts(self):
        runtime = FakeRuntime()
        runtime.block_stage = "rate"
        coordinator = self._coordinator(runtime)
        task = asyncio.create_task(coordinator.execute(request()))

        await runtime.block_entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        # The committed-source abort contract runs even though the old source
        # was already stopped and the target was never started.
        self.assertEqual(len(runtime.abort_calls), 1)
        self.assertTrue(coordinator.gate.failure_latched)
        self.assertTrue(runtime.muted)
        self.assertTrue(runtime.paused)
        self.assertFalse(runtime.playing)
        self.assertEqual(runtime.volume, 100)

    async def test_cancellation_after_target_start_aborts_staged_target(self):
        runtime = FakeRuntime()
        runtime.block_stage = "graph-readback"
        coordinator = self._coordinator(runtime)
        task = asyncio.create_task(coordinator.execute(request()))

        await runtime.block_entered.wait()
        self.assertTrue(runtime.playing)
        self.assertTrue(runtime.staged)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        # The staged/started target is cleaned up through the abort contract;
        # no uncommitted target source remains under an open gate.
        self.assertEqual(len(runtime.abort_calls), 1)
        self.assertTrue(runtime.abort_calls[0]["target_staged"])
        self.assertTrue(coordinator.gate.failure_latched)
        self.assertTrue(coordinator.gate.closed)
        self.assertTrue(runtime.muted)
        self.assertFalse(runtime.playing)
        self.assertTrue(runtime.paused)
        self.assertEqual(coordinator.last_error["stage"], "staged-graph-readback")

    async def test_cancellation_before_gate_open_never_commits(self):
        runtime = FakeRuntime()
        runtime.block_stage = "gate-restore-mute"
        coordinator = self._coordinator(runtime)
        task = asyncio.create_task(coordinator.execute(request()))

        await runtime.block_entered.wait()
        self.assertIn("commit-readback", runtime.events)
        self.assertEqual(runtime.volume, 100)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertIsNone(coordinator.last_result)
        self.assertIsNone(coordinator.last_successful_commit_id)
        self.assertTrue(coordinator.gate.failure_latched)
        self.assertTrue(coordinator.gate.closed)
        self.assertTrue(runtime.muted)
        self.assertTrue(coordinator.last_error["cancelled"])
        self.assertTrue(coordinator.last_error["failure_latched"])
        self.assertEqual(coordinator.last_error["stage"], "output-gate-restore")

    async def test_recovered_abort_reopens_gate_after_cancellation(self):
        runtime = FakeRuntime(abort_result=True)
        runtime.block_stage = "start"
        coordinator = self._coordinator(runtime)
        task = asyncio.create_task(coordinator.execute(request()))

        await runtime.block_entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        # The restored committed source is confirmed inside the abort hook
        # before the Coordinator opens the gate with the same final sequence
        # as a normal commit; failure_latched reflects the real opened gate.
        self.assertLess(
            runtime.events.index("abort-failed-transition"),
            runtime.events.index("mute:False"),
        )
        self.assertFalse(coordinator.gate.failure_latched)
        self.assertFalse(coordinator.gate.closed)
        self.assertFalse(runtime.muted)
        self.assertFalse(runtime.easyeffects_muted)
        self.assertFalse(coordinator.last_error["failure_latched"])
        self.assertTrue(coordinator.last_error["cancelled"])
        self.assertEqual(runtime.volume, 100)

    async def test_cancellation_during_failure_cleanup_drains_to_terminal(self):
        runtime = FakeRuntime()
        runtime.fail_stage = "quiet"
        runtime.arm_volume_zero_block = True
        coordinator = self._coordinator(runtime)
        task = asyncio.create_task(coordinator.execute(request()))

        # The stage fails; the failure cleanup is blocked inside its first
        # step when the outer task gets cancelled (the old third exit).
        await runtime.volume_zero_entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        runtime.volume_zero_release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task

        # The cleanup ran to its terminal contract: abort called, gate
        # latched, last_error present, never closed without a latch, and the
        # cancellation (not a rewritten failure) reached the caller.
        self.assertEqual(len(runtime.abort_calls), 1)
        self.assertTrue(coordinator.gate.failure_latched)
        self.assertTrue(coordinator.gate.closed)
        self.assertTrue(runtime.muted)
        self.assertIsNotNone(coordinator.last_error)
        self.assertTrue(coordinator.last_error["cancelled"])
        self.assertTrue(coordinator.last_error["failure_latched"])
        self.assertEqual(coordinator.last_error["stage"], "quiet-old-source")
        self.assertFalse(coordinator.transition_active)

    async def test_double_cancel_during_cleanup_still_drains_and_releases_lock(self):
        runtime = FakeRuntime()
        runtime.fail_stage = "quiet"
        runtime.block_abort = True
        coordinator = self._coordinator(runtime)
        before = {
            t
            for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
        }
        task = asyncio.create_task(coordinator.execute(request()))

        await runtime.abort_entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        runtime.abort_release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertTrue(task.cancelled())
        self.assertEqual(len(runtime.abort_calls), 1)
        self.assertTrue(coordinator.gate.failure_latched)
        self.assertTrue(coordinator.gate.closed)
        self.assertTrue(runtime.muted)
        self.assertFalse(coordinator.transition_active)
        after = {
            t
            for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
        }
        self.assertEqual(before, after)

    async def test_normal_failure_still_raises_playback_transition_failure(self):
        runtime = FakeRuntime()
        runtime.fail_stage = "graph-readback"
        coordinator = self._coordinator(runtime)

        with self.assertRaises(PlaybackTransitionFailure) as cm:
            await coordinator.execute(request())

        self.assertEqual(cm.exception.stage, "staged-graph-readback")
        self.assertTrue(cm.exception.failure_latched)
        self.assertTrue(coordinator.gate.failure_latched)
        self.assertTrue(coordinator.gate.closed)
        self.assertEqual(len(runtime.abort_calls), 1)
        self.assertTrue(coordinator.last_error["failure_latched"])

    async def test_recovered_failure_keeps_existing_gate_restore_semantics(self):
        runtime = FakeRuntime(abort_result=True)
        runtime.fail_stage = "graph-readback"
        coordinator = self._coordinator(runtime)

        with self.assertRaises(PlaybackTransitionFailure) as cm:
            await coordinator.execute(request())

        self.assertFalse(cm.exception.failure_latched)
        self.assertFalse(coordinator.gate.failure_latched)
        self.assertFalse(coordinator.gate.closed)
        self.assertFalse(runtime.muted)

    async def test_commit_is_not_rolled_back_by_later_caller_cancellation(self):
        runtime = FakeRuntime()
        coordinator = self._coordinator(runtime)

        async def caller():
            result = await coordinator.execute(request())
            await asyncio.sleep(30)
            return result

        task = asyncio.create_task(caller())
        for _ in range(100):
            if coordinator.last_successful_commit_id:
                break
            await asyncio.sleep(0)
        self.assertIsNotNone(coordinator.last_successful_commit_id)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        # The committed state is untouched by the caller-side cancellation.
        self.assertIsNotNone(coordinator.last_successful_commit_id)
        self.assertTrue(coordinator.last_result.committed)
        self.assertFalse(coordinator.gate.closed)
        self.assertFalse(coordinator.gate.failure_latched)
        self.assertEqual(runtime.volume, 100)
        self.assertTrue(runtime.playing)
        self.assertEqual(runtime.abort_calls, [])
        self.assertIsNone(coordinator.last_error)


if __name__ == "__main__":
    unittest.main()
