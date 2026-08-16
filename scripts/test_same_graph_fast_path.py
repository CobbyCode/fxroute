#!/usr/bin/env python3

"""Focused contracts for the same-graph fast path.

A local play that provably keeps the committed graph (same mpv instance, same
rate, same output mode, healthy DSP and complete canonical links) must switch
the transport without closing the output gate and without graph
reconcile/verify.  Every case that can change the graph or whose state is not
provably safe must fall back to the full transition.
"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from playback.runtime import FxrouteTransitionRuntime
from playback.transition import (
    PlaybackTransitionCoordinator,
    PlaybackTransitionFailure,
    TransitionRequest,
    TransitionResult,
)


def make_request(
    *,
    operation="play",
    source="local",
    target_rate=44_100,
    rate_change=False,
    reload_source=True,
    graph_only=False,
    should_play=True,
    output_mode_target=None,
    recovery=None,
    native_queue=None,
    audio_overview=None,
):
    return TransitionRequest(
        operation=operation,
        source=source,
        target_rate=target_rate,
        target_url="/music/target.flac",
        target_track={"source": "local", "url": "/music/target.flac"},
        should_play=should_play,
        rate_change=rate_change,
        reload_source=reload_source,
        graph_only=graph_only,
        output_mode_target=dict(output_mode_target or {}),
        recovery_commit_context_id=recovery,
        native_queue=tuple(native_queue or ()),
        audio_overview=dict(audio_overview if audio_overview is not None else {"output_mode": {"mode": "stereo"}}),
        detail="test-fast-path",
    )


class FastPathFakeRuntime:
    """Coordinator runtime with a controllable fast-path decision."""

    def __init__(self, *, fast_path_eligible=True, fail_stage=None):
        self.events = []
        self.muted = False
        self.dsp_muted = False
        self.rate = 44_100
        self.current_file = "/music/current.flac"
        self.paused = False
        self.playing = True
        self.volume = 100
        self.fast_path_eligible = fast_path_eligible
        self.fail_stage = fail_stage

    async def _stage(self, name):
        self.events.append(name)
        if self.fail_stage == name:
            raise RuntimeError(f"fail {name}")

    async def read_hardware_mute(self):
        self.events.append(f"read-mute:{self.muted}")
        return self.muted

    async def set_hardware_mute(self, muted, transition_id):
        self.muted = bool(muted)
        self.events.append(f"mute:{self.muted}")

    async def read_sink_mute(self, sink_name):
        self.events.append(f"read-sink-mute:{self.dsp_muted}")
        return self.dsp_muted

    async def set_sink_mute(self, sink_name, muted, transition_id):
        self.dsp_muted = bool(muted)
        self.events.append(f"sink-mute:{self.dsp_muted}")

    async def read_transition_snapshot(self, request):
        await self._stage("snapshot")
        return {
            "active_rate": self.rate,
            "force_rate": 0,
            "current_track": {"source": "local", "url": self.current_file},
            "player": {"current_file": self.current_file, "ended": False, "volume": 100},
            "audio_overview": {"output_mode": {"mode": "stereo", "effective_output_key": "alsa_output.test"}},
        }

    async def evaluate_same_graph_fast_path(self, request, snapshot):
        await self._stage("evaluate-fast-path")
        if not self.fast_path_eligible:
            return False
        if request.operation != "play" or request.source != "local":
            return False
        if not request.should_play or not request.reload_source:
            return False
        if request.rate_change or request.graph_only or request.output_mode_target:
            return False
        if request.recovery_commit_context_id or request.native_queue:
            return False
        return True

    async def verify_same_graph_commit(self, request):
        await self._stage("verify-fast")
        if self.rate != request.target_rate:
            raise AssertionError("rate not preserved")
        if self.volume != 100:
            raise AssertionError("source volume not restored")
        if request.should_play and (self.paused or not self.playing):
            raise AssertionError("source not playing")
        return {"committed": True, "fast_path": True, "active_rate": self.rate}

    async def quiet_old_source(self, request):
        await self._stage("quiet")
        self.paused = True
        self.playing = False
        self.volume = 0

    async def resolve_target_rate(self, request):
        await self._stage("resolve-rate")
        return request.target_rate

    async def establish_target_rate(self, request):
        await self._stage("rate")
        self.rate = request.target_rate

    async def establish_effects_and_helper(self, request):
        await self._stage("effects-helper-links")
        return {"dsp_reinitialized": bool(request.rate_change)}

    async def prepare_target_source(self, request):
        await self._stage("prepare")
        if request.target_url:
            self.current_file = request.target_url
        self.paused = True
        self.playing = False
        self.volume = 0

    async def start_target_source(self, request):
        await self._stage("start")
        self.paused = not request.should_play
        self.playing = request.should_play

    async def reconcile_post_start_graph(self, request):
        await self._stage("reconcile-post-start")
        return {"graph_complete": True}

    async def stabilize_effects_after_rate_change(self, request, *, dsp_reinitialized=False):
        await self._stage("dsp-stabilize")
        return {"stabilized": True, "active_rate": self.rate, "force_rate": self.rate}

    async def set_source_volume(self, volume, transition_id):
        self.events.append(f"source-volume:{volume}")
        self.volume = volume

    async def verify_committed_transition(self, request):
        await self._stage("verify")
        return {"committed": True, "active_rate": self.rate, "source_volume": self.volume}

    async def verify_transition_graph(self, request):
        await self._stage("verify-graph")
        return {"committed": True, "active_rate": self.rate}

    async def pause_source_after_failure(self, request):
        await self._stage("pause-after-failure")

    async def abort_failed_transition(self, request, snapshot, *, target_staged):
        await self._stage("abort")
        return None

    def target_source_staged(self, request):
        return True


def make_coordinator(runtime, *, committed=True):
    coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
    if committed:
        coordinator.last_result = TransitionResult(
            transition_id="tr-previous",
            committed=True,
            source="local",
            target_rate=44_100,
            state={},
        )
    return coordinator


class SameGraphFastPathCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_rate_same_graph_skips_gate_and_graph_verify(self):
        runtime = FastPathFakeRuntime(fast_path_eligible=True)
        coordinator = make_coordinator(runtime)
        result = await coordinator.execute(make_request())
        self.assertTrue(result.committed)
        gate_events = [e for e in runtime.events if e.startswith(("mute:", "read-mute", "sink-mute"))]
        self.assertEqual(gate_events, [], "output gate must stay untouched")
        self.assertNotIn("verify-graph", runtime.events)
        self.assertNotIn("reconcile-post-start", runtime.events)
        self.assertNotIn("effects-helper-links", runtime.events)
        self.assertIn("verify-fast", runtime.events)
        self.assertEqual(runtime.current_file, "/music/target.flac")
        self.assertEqual(runtime.volume, 100)
        self.assertTrue(runtime.playing)

    async def test_rate_change_uses_full_path(self):
        runtime = FastPathFakeRuntime(fast_path_eligible=True)
        coordinator = make_coordinator(runtime)
        result = await coordinator.execute(make_request(rate_change=True))
        self.assertTrue(result.committed)
        self.assertIn("mute:True", runtime.events)
        self.assertIn("mute:False", runtime.events)
        self.assertIn("verify-graph", runtime.events)
        self.assertIn("effects-helper-links", runtime.events)
        self.assertNotIn("verify-fast", runtime.events)

    async def test_output_mode_target_uses_full_path(self):
        runtime = FastPathFakeRuntime(fast_path_eligible=True)
        coordinator = make_coordinator(runtime)
        result = await coordinator.execute(
            make_request(output_mode_target={"output_mode": {"mode": "subwoofer-2.2"}})
        )
        self.assertTrue(result.committed)
        self.assertIn("mute:True", runtime.events)
        self.assertNotIn("verify-fast", runtime.events)

    async def test_measurement_owned_uses_full_path(self):
        runtime = FastPathFakeRuntime(fast_path_eligible=False)
        coordinator = make_coordinator(runtime)
        result = await coordinator.execute(make_request())
        self.assertTrue(result.committed)
        self.assertIn("mute:True", runtime.events)
        self.assertIn("verify-graph", runtime.events)
        self.assertNotIn("verify-fast", runtime.events)

    async def test_unhealthy_graph_uses_full_path(self):
        runtime = FastPathFakeRuntime(fast_path_eligible=False)
        coordinator = make_coordinator(runtime)
        result = await coordinator.execute(make_request())
        self.assertTrue(result.committed)
        self.assertIn("mute:True", runtime.events)
        self.assertNotIn("verify-fast", runtime.events)

    async def test_fresh_coordinator_without_committed_result_uses_full_path(self):
        runtime = FastPathFakeRuntime(fast_path_eligible=True)
        coordinator = make_coordinator(runtime, committed=False)
        result = await coordinator.execute(make_request())
        self.assertTrue(result.committed)
        self.assertIn("mute:True", runtime.events)
        self.assertNotIn("verify-fast", runtime.events)
        # The coordinator short-circuits on its own committed-state precondition
        # before consulting the runtime evaluator.
        self.assertNotIn("evaluate-fast-path", runtime.events)

    async def test_failed_last_transition_uses_full_path(self):
        runtime = FastPathFakeRuntime(fast_path_eligible=True)
        coordinator = make_coordinator(runtime)
        coordinator.last_error = {"ok": False, "stage": "commit-readback"}
        result = await coordinator.execute(make_request())
        self.assertTrue(result.committed)
        self.assertIn("mute:True", runtime.events)
        self.assertNotIn("evaluate-fast-path", runtime.events)

    async def test_latched_gate_uses_full_path(self):
        runtime = FastPathFakeRuntime(fast_path_eligible=True)
        coordinator = make_coordinator(runtime)
        coordinator.gate.closed = True
        coordinator.gate.failure_latched = True
        result = await coordinator.execute(make_request())
        self.assertTrue(result.committed)
        self.assertNotIn("evaluate-fast-path", runtime.events)

    async def test_failed_fast_path_restores_without_gate_guard(self):
        runtime = FastPathFakeRuntime(fast_path_eligible=True, fail_stage="verify-fast")

        async def abort(request, snapshot, *, target_staged):
            await runtime._stage("abort")
            return {"restore": TransitionRequest(
                operation="replay",
                source="local",
                target_rate=44_100,
                target_url="/music/target.flac",
                target_track={"source": "local", "url": "/music/target.flac"},
                should_play=True,
                rate_change=False,
                reload_source=True,
                detail="failed-transition-restore",
            )}

        runtime.abort_failed_transition = abort
        coordinator = make_coordinator(runtime)
        with self.assertRaises(PlaybackTransitionFailure):
            await coordinator.execute(make_request())
        # The gate was never closed by the fast path, so the Coordinator
        # restore runs ungated (no boundary re-checks); the restored source
        # leaves the open gate open without a latch.
        self.assertFalse(coordinator.gate.closed)
        self.assertFalse(coordinator.gate.failure_latched)
        self.assertIn("abort", runtime.events)
        self.assertIn("verify-fast", runtime.events)
        # The restore ran the standard source handoff stages.
        self.assertIn("rate", runtime.events)
        self.assertIn("verify", runtime.events)
        self.assertEqual(coordinator.last_error["ok"], False)

    async def test_full_path_failure_still_runs_abort_cleanup(self):
        runtime = FastPathFakeRuntime(fast_path_eligible=False, fail_stage="verify-graph")
        coordinator = make_coordinator(runtime)
        with self.assertRaises(PlaybackTransitionFailure):
            await coordinator.execute(make_request())
        # The full path closed the gate; the abort verdict keeps the latch.
        self.assertIn("abort", runtime.events)
        self.assertTrue(coordinator.gate.failure_latched)


class _PlayerDouble:
    def __init__(self, *, current_file="/music/target.flac", playing=True, paused=False, volume=100):
        self.state = {
            "current_file": current_file,
            "playing": playing,
            "paused": paused,
            "volume": volume,
            "ended": False,
        }
        self._live = {"pause": paused, "idle-active": not playing, "volume": float(volume)}

    def get_property(self, name):
        if name not in self._live:
            raise KeyError(name)
        return self._live[name]


class _FastPathDeps:
    def __init__(self, *, owned=False, links_complete=True, player=None, diagnosis_raises=False):
        self._owned = owned
        self._links_complete = links_complete
        self._diagnosis_raises = diagnosis_raises
        self._player = player if player is not None else _PlayerDouble()

    def player(self):
        return self._player

    def measurement_audio_graph_owned(self):
        return self._owned

    async def playback_graph_diagnosis(self, **kwargs):
        if self._diagnosis_raises:
            raise RuntimeError("pw-link failed")
        return {"links_complete": self._links_complete}


def make_production_runtime(deps):
    return FxrouteTransitionRuntime(deps)


class ProductionEvaluateTests(unittest.IsolatedAsyncioTestCase):
    def _snapshot(self, *, active_rate=44_100, force_rate=0, source="local", current_file="/music/current.flac", ended=False):
        return {
            "active_rate": active_rate,
            "force_rate": force_rate,
            "current_track": {"source": source, "url": current_file},
            "player": {"current_file": current_file, "ended": ended},
        }

    async def test_evaluate_true_when_all_preconditions_hold(self):
        runtime = make_production_runtime(_FastPathDeps())
        request = make_request(audio_overview={"output_mode": {"mode": "stereo"}})
        self.assertTrue(await runtime.evaluate_same_graph_fast_path(request, self._snapshot()))

    async def test_evaluate_false_on_rate_change(self):
        runtime = make_production_runtime(_FastPathDeps())
        request = make_request(rate_change=True)
        self.assertFalse(await runtime.evaluate_same_graph_fast_path(request, self._snapshot()))

    async def test_evaluate_false_on_output_mode_target(self):
        runtime = make_production_runtime(_FastPathDeps())
        request = make_request(output_mode_target={"output_mode": {"mode": "subwoofer-2.2"}})
        self.assertFalse(await runtime.evaluate_same_graph_fast_path(request, self._snapshot()))

    async def test_evaluate_false_on_measurement_owned(self):
        runtime = make_production_runtime(_FastPathDeps(owned=True))
        request = make_request()
        self.assertFalse(await runtime.evaluate_same_graph_fast_path(request, self._snapshot()))

    async def test_evaluate_false_on_rate_mismatch(self):
        runtime = make_production_runtime(_FastPathDeps())
        request = make_request()
        snapshot = self._snapshot(active_rate=48_000)
        self.assertFalse(await runtime.evaluate_same_graph_fast_path(request, snapshot))

    async def test_evaluate_false_on_committed_source_mismatch(self):
        runtime = make_production_runtime(_FastPathDeps())
        request = make_request()
        snapshot = self._snapshot(source="radio")
        self.assertFalse(await runtime.evaluate_same_graph_fast_path(request, snapshot))

    async def test_evaluate_false_on_unhealthy_graph(self):
        runtime = make_production_runtime(_FastPathDeps(links_complete=False))
        request = make_request()
        self.assertFalse(await runtime.evaluate_same_graph_fast_path(request, self._snapshot()))

    async def test_evaluate_false_when_graph_check_raises(self):
        runtime = make_production_runtime(_FastPathDeps(diagnosis_raises=True))
        request = make_request()
        self.assertFalse(await runtime.evaluate_same_graph_fast_path(request, self._snapshot()))

    async def test_evaluate_false_on_missing_overview(self):
        runtime = make_production_runtime(_FastPathDeps())
        request = make_request(audio_overview={})
        self.assertFalse(await runtime.evaluate_same_graph_fast_path(request, self._snapshot()))

    async def test_evaluate_false_on_native_queue(self):
        runtime = make_production_runtime(_FastPathDeps())
        request = make_request(native_queue=[{"url": "/music/a.flac"}])
        self.assertFalse(await runtime.evaluate_same_graph_fast_path(request, self._snapshot()))


class ProductionVerifySameGraphCommitTests(unittest.IsolatedAsyncioTestCase):
    async def test_verify_ok_when_target_loaded_playing_at_volume(self):
        runtime = make_production_runtime(_FastPathDeps())
        result = await runtime.verify_same_graph_commit(make_request())
        self.assertTrue(result["committed"])
        self.assertTrue(result["fast_path"])

    async def test_verify_fails_on_wrong_current_file(self):
        player = _PlayerDouble(current_file="/music/other.flac")
        runtime = make_production_runtime(_FastPathDeps(player=player))
        with self.assertRaisesRegex(RuntimeError, r"current_file mismatch"):
            await runtime.verify_same_graph_commit(make_request())

    async def test_verify_fails_when_live_paused(self):
        player = _PlayerDouble(paused=True, playing=False)
        runtime = make_production_runtime(_FastPathDeps(player=player))
        with self.assertRaisesRegex(RuntimeError, r"not actually playing at fast-path commit"):
            await runtime.verify_same_graph_commit(make_request())

    async def test_verify_fails_when_volume_not_restored(self):
        player = _PlayerDouble(volume=0)
        runtime = make_production_runtime(_FastPathDeps(player=player))
        with self.assertRaisesRegex(RuntimeError, r"source volume was not restored: 0"):
            await runtime.verify_same_graph_commit(make_request())


if __name__ == "__main__":
    unittest.main()
