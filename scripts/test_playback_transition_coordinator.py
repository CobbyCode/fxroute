#!/usr/bin/env python3

"""Focused contracts for the single-owner playback transition coordinator."""

import asyncio
import pathlib
import tempfile
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from playback_transition import (
    PlaybackTransitionCoordinator,
    PlaybackTransitionFailure,
    TransitionRequest,
)
from player import MPVWrapper


class FakeRuntime:
    def __init__(
        self,
        *,
        muted=False,
        fail_stage=None,
        force_dsp_reinit=False,
        drop_mute_read_number=None,
        fail_mute_read_number=None,
    ):
        self.muted = muted
        self.fail_stage = fail_stage
        self.force_dsp_reinit = force_dsp_reinit
        self.drop_mute_read_number = drop_mute_read_number
        self.fail_mute_read_number = fail_mute_read_number
        self.mute_reads = 0
        self.events = []
        self.rate = 44_100
        self.current_file = "/music/current.flac"
        self.paused = True
        self.playing = False
        self.volume = 100
        self.position = 0.0
        self.effects_rebuilds = 0
        self.helper_rebuilds = 0
        self.dsp_stabilizations = 0

    async def _stage(self, name):
        self.events.append(name)
        if self.fail_stage == name:
            raise RuntimeError(name)

    async def read_hardware_mute(self):
        self.mute_reads += 1
        if self.mute_reads == self.fail_mute_read_number:
            raise RuntimeError("physical mute read failed")
        if self.mute_reads == self.drop_mute_read_number:
            self.events.append("read-mute:False-transient")
            return False
        self.events.append(f"read-mute:{self.muted}")
        return self.muted

    async def set_hardware_mute(self, muted, transition_id):
        self.muted = muted
        self.events.append(f"mute:{muted}")

    async def read_transition_snapshot(self, request):
        await self._stage("snapshot")
        return {"active_rate": self.rate}

    async def quiet_old_source(self, request):
        await self._stage("quiet")
        self.paused = True
        self.playing = False

    async def resolve_target_rate(self, request):
        await self._stage("resolve-rate")
        return 48_000 if request.target_rate is None else request.target_rate

    async def establish_target_rate(self, request):
        await self._stage("rate")
        self.rate = request.target_rate

    async def establish_effects_and_helper(self, request):
        await self._stage("effects-helper-links")
        if request.rate_change:
            self.effects_rebuilds += 1
            self.helper_rebuilds += 1
        return {
            "dsp_reinitialized": bool(request.rate_change or self.force_dsp_reinit),
            "helper_rebuilt": bool(request.rate_change),
        }

    async def prepare_target_source(self, request):
        await self._stage("prepare")
        if request.target_url:
            self.current_file = request.target_url
        if request.reload_source:
            self.volume = 0
        self.paused = True
        self.playing = False
        if request.restore_position is not None:
            self.position = float(request.restore_position)
            self.events.append("seek")

    async def start_target_source(self, request):
        await self._stage("start")
        self.paused = not request.should_play
        self.playing = request.should_play

    async def stabilize_effects_after_rate_change(
        self, request, *, dsp_reinitialized=False
    ):
        await self._stage("dsp-stabilize")
        self.dsp_stabilizations += 1
        return {
            "stabilized": True,
            "no_op": not (request.rate_change or dsp_reinitialized),
            "active_rate": self.rate,
            "force_rate": self.rate,
            "graph_complete": True,
        }

    async def set_source_volume(self, volume, transition_id):
        self.events.append(f"source-volume:{volume}")
        self.volume = volume

    async def verify_committed_transition(self, request):
        await self._stage("verify")
        if self.rate != request.target_rate:
            raise AssertionError("rate not committed")
        if self.volume != 100:
            raise AssertionError("source volume not restored")
        if request.should_play and (self.paused or not self.playing):
            raise AssertionError("source not playing")
        if not request.should_play and not self.paused:
            raise AssertionError("source not paused")
        return {"committed": True, "active_rate": self.rate}

    async def verify_transition_graph(self, request):
        await self._stage("verify-graph")
        if self.rate != request.target_rate:
            raise AssertionError("rate not aligned")
        if request.should_play and (self.paused or not self.playing):
            raise AssertionError("source not playing")
        if not request.should_play and not self.paused:
            raise AssertionError("source not paused")
        return {"committed": True, "active_rate": self.rate}

    async def pause_source_after_failure(self, request):
        await self._stage("pause-after-failure")


def request(*, rate_change=True):
    return TransitionRequest(
        operation="play",
        source="local",
        target_rate=48_000,
        target_url="/music/target.flac",
        target_track={"source": "local", "url": "/music/target.flac"},
        should_play=True,
        rate_change=rate_change,
        reload_source=True,
    )


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_restores_original_unmuted_state_after_commit(self):
        runtime = FakeRuntime(muted=False)
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        result = await coordinator.execute(request())

        self.assertTrue(result.committed)
        self.assertFalse(runtime.muted)
        self.assertLess(runtime.events.index("mute:True"), runtime.events.index("rate"))
        self.assertLess(runtime.events.index("verify"), runtime.events.index("mute:False"))
        self.assertEqual(coordinator.gate.as_dict()["owner"], None)

    async def test_gate_readback_recloses_a_transiently_unmuted_sink(self):
        runtime = FakeRuntime(muted=False, drop_mute_read_number=3)
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        result = await coordinator.execute(request())

        self.assertTrue(result.committed)
        self.assertFalse(runtime.muted)
        self.assertIn("read-mute:False-transient", runtime.events)
        self.assertGreaterEqual(runtime.events.count("mute:True"), 2)

    async def test_gate_readback_failure_latches_a_safe_transition(self):
        runtime = FakeRuntime(muted=False, fail_mute_read_number=3)
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        with self.assertRaises(PlaybackTransitionFailure) as caught:
            await coordinator.execute(request())

        self.assertEqual(caught.exception.stage, "target-rate")
        self.assertTrue(coordinator.gate.failure_latched)
        self.assertTrue(runtime.muted)

    async def test_failure_latches_gate_and_next_success_does_not_inherit_fxroute_mute_as_user_mute(self):
        runtime = FakeRuntime(muted=False, fail_stage="rate")
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        with self.assertRaises(PlaybackTransitionFailure):
            await coordinator.execute(request())
        self.assertTrue(runtime.muted)
        self.assertTrue(coordinator.gate.failure_latched)
        self.assertFalse(coordinator.gate.original_user_muted)

        runtime.fail_stage = None
        await coordinator.execute(request())

        self.assertFalse(runtime.muted)
        self.assertFalse(coordinator.gate.failure_latched)
        self.assertIsNone(coordinator.gate.original_user_muted)

    async def test_intentionally_user_muted_sink_remains_muted_after_success(self):
        runtime = FakeRuntime(muted=True)
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        await coordinator.execute(request())

        self.assertTrue(runtime.muted)
        self.assertFalse(coordinator.gate.closed)

    async def test_failure_exposes_structured_ui_status(self):
        runtime = FakeRuntime(muted=False, fail_stage="effects-helper-links")
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        with self.assertRaises(PlaybackTransitionFailure) as caught:
            await coordinator.execute(request())

        status = caught.exception.as_status()
        self.assertFalse(status["ok"])
        self.assertTrue(status["failure_latched"])
        self.assertEqual(status["stage"], "effects-helper-links")
        self.assertEqual(coordinator.status()["last_error"], status)

    async def test_same_rate_pause_does_not_change_hardware_gate(self):
        runtime = FakeRuntime(muted=False)
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
        pause_request = TransitionRequest(
            operation="pause",
            source="local",
            target_rate=44_100,
            target_url="/music/current.flac",
            should_play=False,
            rate_change=False,
            reload_source=False,
        )

        await coordinator.execute(pause_request)

        self.assertFalse(runtime.muted)
        self.assertNotIn("mute:True", runtime.events)

    async def test_rate_change_both_directions_keeps_loudness_transition_atomic(self):
        runtime = FakeRuntime(muted=False)
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        await coordinator.execute(request(rate_change=True))
        self.assertEqual(runtime.rate, 48_000)
        await coordinator.execute(TransitionRequest(
            operation="play",
            source="local",
            target_rate=44_100,
            target_url="/music/return.flac",
            target_track={"source": "local", "url": "/music/return.flac"},
            should_play=True,
            rate_change=True,
            reload_source=True,
        ))

        self.assertEqual(runtime.rate, 44_100)
        self.assertFalse(runtime.muted)
        self.assertEqual(runtime.effects_rebuilds, 2)
        self.assertEqual(runtime.helper_rebuilds, 2)
        self.assertEqual(runtime.dsp_stabilizations, 2)

    async def test_gate_stays_closed_until_post_start_dsp_readback(self):
        runtime = FakeRuntime(muted=False)
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        await coordinator.execute(request())

        self.assertLess(
            runtime.events.index("source-volume:100"),
            runtime.events.index("dsp-stabilize"),
        )
        self.assertLess(
            runtime.events.index("dsp-stabilize"),
            runtime.events.index("verify"),
        )
        self.assertLess(
            runtime.events.index("verify"),
            runtime.events.index("mute:False"),
        )

    async def test_same_rate_resume_and_radio_switch_do_not_request_rate_rebuild(self):
        runtime = FakeRuntime(muted=False)
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        await coordinator.execute(TransitionRequest(
            operation="resume",
            source="local",
            target_rate=44_100,
            target_url="/music/current.flac",
            target_track={"source": "local", "url": "/music/current.flac"},
            should_play=True,
            rate_change=False,
            reload_source=False,
        ))
        await coordinator.execute(TransitionRequest(
            operation="play",
            source="radio",
            target_rate=44_100,
            target_url="https://radio.example/next",
            target_track={"source": "radio", "url": "https://radio.example/next"},
            should_play=True,
            rate_change=False,
            reload_source=True,
        ))

        self.assertEqual(runtime.effects_rebuilds, 0)
        self.assertEqual(runtime.helper_rebuilds, 0)
        self.assertEqual(runtime.dsp_stabilizations, 0)
        self.assertFalse(runtime.muted)

    async def test_same_rate_effects_reinitialization_triggers_post_start_dsp(self):
        runtime = FakeRuntime(muted=False, force_dsp_reinit=True)
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
        same_rate_request = TransitionRequest(
            operation="play",
            source="local",
            target_rate=44_100,
            target_url="/music/same-rate.flac",
            target_track={"source": "local", "url": "/music/same-rate.flac"},
            should_play=True,
            rate_change=False,
            reload_source=True,
        )

        result = await coordinator.execute(same_rate_request)

        self.assertTrue(result.committed)
        self.assertEqual(runtime.dsp_stabilizations, 1)
        self.assertTrue(result.state["effects_graph"]["dsp_reinitialized"])

    async def test_each_transition_emits_one_stage_timing_line(self):
        runtime = FakeRuntime(muted=False)
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        with self.assertLogs("playback_transition", level="INFO") as captured:
            await coordinator.execute(request())

        timing = [line for line in captured.output if "Playback transition timing:" in line]
        self.assertEqual(len(timing), 1)
        self.assertIn("transition_id=tr-", timing[0])
        self.assertIn("target-rate=", timing[0])

    async def test_measurement_restore_reloads_a_paused_source_through_same_gate(self):
        runtime = FakeRuntime(muted=False)
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        await coordinator.restore_measurement(
            source="local",
            target_rate=44_100,
            target_url="/music/restored.flac",
            target_track={"source": "local", "url": "/music/restored.flac"},
            should_play=False,
            restore_position=123.5,
        )

        self.assertEqual(runtime.current_file, "/music/restored.flac")
        self.assertTrue(runtime.paused)
        self.assertFalse(runtime.playing)
        self.assertEqual(runtime.position, 123.5)
        self.assertLess(runtime.events.index("seek"), runtime.events.index("start"))
        self.assertFalse(runtime.muted)

    async def test_stale_measurement_restore_is_discarded_before_source_mutation(self):
        runtime = FakeRuntime(muted=False)

        async def validate(_request, _snapshot):
            return False

        runtime.validate_measurement_restore_intent = validate
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
        result = await coordinator.restore_measurement(
            source="local",
            target_rate=44_100,
            target_url="/music/old.flac",
            target_track={"source": "local", "url": "/music/old.flac"},
            should_play=True,
            restore_position=20.0,
            restore_intent={"source": "local", "url": "/music/old.flac"},
        )

        self.assertFalse(result.committed)
        self.assertTrue(result.state["skipped"])
        self.assertNotIn("quiet", runtime.events)
        self.assertNotIn("prepare", runtime.events)
        self.assertFalse(coordinator.gate.closed)

    async def test_local_unknown_rate_is_committed_from_same_transition(self):
        runtime = FakeRuntime(muted=False)
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
        unknown_rate_request = TransitionRequest(
            operation="play",
            source="local",
            target_rate=None,
            target_url="/music/unknown.flac",
            target_track={"source": "local", "url": "/music/unknown.flac"},
            should_play=True,
            rate_change=False,
            reload_source=True,
        )

        result = await coordinator.execute(unknown_rate_request)

        self.assertTrue(result.committed)
        self.assertEqual(result.target_rate, 48_000)
        self.assertEqual(runtime.rate, 48_000)
        self.assertLess(runtime.events.index("resolve-rate"), runtime.events.index("rate"))
        self.assertLess(runtime.events.index("rate"), runtime.events.index("prepare"))

    async def test_startup_reconciles_a_persisted_fxroute_failure_mute(self):
        with tempfile.TemporaryDirectory(prefix="fxroute-gate-test-") as directory:
            gate_path = pathlib.Path(directory) / "playback-gate.json"
            runtime = FakeRuntime(muted=False, fail_stage="rate")
            first = PlaybackTransitionCoordinator(
                runtime, gate_settle_seconds=0, gate_state_path=gate_path
            )

            with self.assertRaises(PlaybackTransitionFailure):
                await first.execute(request())
            self.assertTrue(gate_path.exists())
            self.assertTrue(runtime.muted)

            second = PlaybackTransitionCoordinator(
                runtime, gate_settle_seconds=0, gate_state_path=gate_path
            )
            self.assertTrue(await second.reconcile_startup_gate())
            self.assertFalse(runtime.muted)
            self.assertFalse(second.gate.closed)
            self.assertFalse(second.gate.failure_latched)
            self.assertFalse(gate_path.exists())


class PlayerLoadContractTests(unittest.TestCase):
    def test_loadfile_preserves_cached_pause_until_explicit_start(self):
        player = MPVWrapper()
        player._running = True
        player._state["paused"] = True
        player._state["current_file"] = "/music/old.flac"
        player._send_command = lambda *args: {"error": "success"}
        player._notify_callbacks = lambda: None

        player.loadfile("/music/new.flac")
        self.assertTrue(player.state["paused"])
        self.assertFalse(player.state["playing"])

        player.loadfile("/music/next.flac", start_paused=False)
        self.assertFalse(player.state["paused"])
        self.assertTrue(player.state["playing"])


if __name__ == "__main__":
    unittest.main()
