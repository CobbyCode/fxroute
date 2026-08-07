#!/usr/bin/env python3

"""Focused contracts for the single-owner playback transition coordinator."""

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
from player import MPVWrapper


class FakeRuntime:
    def __init__(self, *, muted=False, fail_stage=None):
        self.muted = muted
        self.fail_stage = fail_stage
        self.events = []
        self.rate = 44_100
        self.current_file = "/music/current.flac"
        self.paused = True
        self.playing = False
        self.volume = 100
        self.effects_rebuilds = 0
        self.helper_rebuilds = 0

    async def _stage(self, name):
        self.events.append(name)
        if self.fail_stage == name:
            raise RuntimeError(name)

    async def read_hardware_mute(self):
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
        return request.target_rate

    async def establish_target_rate(self, request):
        await self._stage("rate")
        self.rate = request.target_rate

    async def establish_effects_and_helper(self, request):
        await self._stage("effects-helper-links")
        if request.rate_change:
            self.effects_rebuilds += 1
            self.helper_rebuilds += 1

    async def prepare_target_source(self, request):
        await self._stage("prepare")
        if request.target_url:
            self.current_file = request.target_url
        if request.reload_source:
            self.volume = 0
        self.paused = True
        self.playing = False

    async def start_target_source(self, request):
        await self._stage("start")
        self.paused = not request.should_play
        self.playing = request.should_play

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
        self.assertFalse(runtime.muted)

    async def test_measurement_restore_reloads_a_paused_source_through_same_gate(self):
        runtime = FakeRuntime(muted=False)
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        await coordinator.restore_measurement(
            source="local",
            target_rate=44_100,
            target_url="/music/restored.flac",
            target_track={"source": "local", "url": "/music/restored.flac"},
            should_play=False,
        )

        self.assertEqual(runtime.current_file, "/music/restored.flac")
        self.assertTrue(runtime.paused)
        self.assertFalse(runtime.playing)
        self.assertFalse(runtime.muted)


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
