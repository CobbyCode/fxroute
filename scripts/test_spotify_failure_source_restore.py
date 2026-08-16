#!/usr/bin/env python3
"""Focused tests for the committed-source restore after a failed source
handoff (P1-2 completion).

When a transition fails before commit and a Local/Radio source was committed
before, the runtime abort hook decides the outcome:

  * None                   - the committed context is unchanged; nothing runs
  * {"invalidate": True}   - a staged target was stopped and the active
    metadata invalidated; the committed queue stays
  * {"restore": <request>} - the Coordinator physically restores that source
    AND its full playback graph by running its own bounded stage sequence
    under the still-closed output gate (never a nested Coordinator
    transition):

      old rate -> effects/helper for the old rate -> source/queue transport
      (including a committed native MPV playlist) -> post-start graph
      reconcile -> staged graph readback -> DSP stabilization when the failed
      Spotify transition reinitialized the DSP -> final commit readback that
      must positively confirm source volume 100.

The Coordinator restore returns True only when every stage confirmed the old
source; any stage failure keeps the Coordinator failure latch as the safe
state.  The Coordinator then restores the output gate (no failure latch).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import playback.queue as playback_queue
import main
from playback_queue_test_support import queue_state, restore_queue_state
from playback_transition_test_support import make_transition_runtime
from playback.transition import (
    PlaybackTransitionCoordinator,
    PlaybackTransitionFailure,
    TransitionRequest,
)
from test_playback_transition_coordinator import FakeRuntime


def spotify_request(*, rate_change: bool = True) -> TransitionRequest:
    return TransitionRequest(
        operation="spotify-play",
        source="spotify",
        target_rate=48_000,
        should_play=True,
        rate_change=rate_change,
        reload_source=True,
        detail="api-spotify-play",
    )


def local_snapshot(
    *,
    force_rate: int = 44_100,
    active_rate: int = 44_100,
    playing: bool = True,
    position: float = 12.5,
) -> dict:
    return {
        "force_rate": force_rate,
        "active_rate": active_rate,
        "player": {
            "current_file": "/music/old.flac",
            "playing": playing,
            "paused": not playing,
            "ended": False,
            "volume": 73,
            "position": position,
        },
        "current_track": {
            "source": "local",
            "url": "/music/old.flac",
            "id": "old",
            "title": "Old Track",
            "sample_rate_hz": 44100,
        },
    }


def radio_snapshot(*, force_rate: int = 44_100) -> dict:
    return {
        "force_rate": force_rate,
        "active_rate": force_rate,
        "player": {
            "current_file": "https://radio.example/live",
            "playing": True,
            "paused": False,
            "ended": False,
            "volume": 80,
            "position": 0.0,
        },
        "current_track": {
            "source": "radio",
            "url": "https://radio.example/live",
            "id": "radio_1",
            "title": "Radio One",
            "sample_rate_hz": 44100,
        },
    }


def restore_request(
    *,
    source: str = "local",
    target_rate: int = 48_000,
    should_play: bool = True,
    rate_change: bool = True,
    url: str = "/music/old.flac",
    position: float | None = None,
) -> TransitionRequest:
    return TransitionRequest(
        operation="replay",
        source=source,
        target_rate=target_rate,
        target_url=url,
        target_track={
            "source": source,
            "url": url,
            "id": "old",
            "title": "Old Track",
            "sample_rate_hz": target_rate,
        },
        should_play=should_play,
        rate_change=rate_change,
        reload_source=True,
        restore_position=position,
        detail="failed-transition-restore",
    )


class AbortVerdictTests(unittest.IsolatedAsyncioTestCase):
    """The runtime abort hook decides the outcome and builds the restore
    request; the Coordinator runs the restore itself."""

    async def asyncSetUp(self):
        self._rate_patch = patch.object(
            main, "get_samplerate_status", return_value={"active_rate": 44_100}
        )
        self._rate_patch.start()

    async def asyncTearDown(self):
        self._rate_patch.stop()

    async def test_local_spotify_failure_requests_full_restore(self):
        verdict = await make_transition_runtime().abort_failed_transition(
            spotify_request(),
            local_snapshot(),
            target_staged=False,
        )
        self.assertIsNotNone(verdict)
        restore = verdict["restore"]
        self.assertEqual(restore.operation, "replay")
        self.assertEqual(restore.source, "local")
        self.assertEqual(restore.target_rate, 44_100)
        self.assertEqual(restore.target_url, "/music/old.flac")
        self.assertTrue(restore.should_play)
        self.assertTrue(restore.rate_change)
        self.assertEqual(restore.restore_position, 12.5)
        self.assertTrue(restore.reload_source)
        self.assertEqual(restore.detail, "failed-transition-restore")

    async def test_same_rate_48k_to_48k_failure_keeps_authoritative_rate(self):
        # Local 48 kHz committed; Spotify also targets 48 kHz, so the failed
        # request carried rate_change=False.  The restore must still use the
        # authoritative committed active_rate.
        with patch.object(
            main, "get_samplerate_status", return_value={"active_rate": 48_000}
        ):
            verdict = await make_transition_runtime().abort_failed_transition(
                spotify_request(rate_change=False),
                local_snapshot(force_rate=None, active_rate=48_000),
                target_staged=False,
            )
        self.assertIsNotNone(verdict)
        restore = verdict["restore"]
        self.assertEqual(restore.target_rate, 48_000)
        self.assertFalse(restore.rate_change)

    async def test_fixed_policy_track_rate_is_not_trusted_over_committed_active_rate(self):
        # Track metadata says 44.1 kHz but the committed hardware state was
        # 48 kHz (fixed policy): the restore must take the committed rate.
        snapshot = local_snapshot(force_rate=None, active_rate=48_000)
        snapshot["current_track"]["sample_rate_hz"] = 44_100
        with patch.object(
            main, "get_samplerate_status", return_value={"active_rate": 48_000}
        ):
            verdict = await make_transition_runtime().abort_failed_transition(
                spotify_request(rate_change=False),
                snapshot,
                target_staged=False,
            )
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict["restore"].target_rate, 48_000)

    async def test_unknown_live_rate_is_treated_as_rate_change(self):
        # Same-rate 48k restore with unavailable live status: rate_change
        # must be conservative True so effects/helper are validated, while
        # establish_target_rate remains an idempotent no-op if the hardware
        # already stands at the committed rate.
        with patch.object(
            main,
            "get_samplerate_status",
            Mock(side_effect=RuntimeError("samplerate status unavailable")),
        ):
            verdict = await make_transition_runtime().abort_failed_transition(
                spotify_request(rate_change=False),
                local_snapshot(force_rate=None, active_rate=48_000),
                target_staged=False,
            )
        self.assertIsNotNone(verdict)
        restore = verdict["restore"]
        self.assertEqual(restore.target_rate, 48_000)
        self.assertTrue(restore.rate_change)

    async def test_local_position_restored_for_playing_and_paused(self):
        verdict = await make_transition_runtime().abort_failed_transition(
            spotify_request(),
            local_snapshot(playing=True, position=37.25),
            target_staged=False,
        )
        self.assertIsNotNone(verdict)
        restore = verdict["restore"]
        self.assertEqual(restore.restore_position, 37.25)
        self.assertTrue(restore.should_play)

        verdict = await make_transition_runtime().abort_failed_transition(
            spotify_request(),
            local_snapshot(playing=False, position=37.25),
            target_staged=False,
        )
        self.assertIsNotNone(verdict)
        restore = verdict["restore"]
        self.assertEqual(restore.restore_position, 37.25)
        self.assertFalse(restore.should_play)

    async def test_radio_gets_no_position_restore(self):
        verdict = await make_transition_runtime().abort_failed_transition(
            spotify_request(),
            radio_snapshot(),
            target_staged=False,
        )
        self.assertIsNotNone(verdict)
        restore = verdict["restore"]
        self.assertEqual(restore.source, "radio")
        self.assertEqual(restore.target_rate, 44_100)
        self.assertIsNone(restore.restore_position)
        self.assertIsNone(restore.native_queue)

    async def test_spotify_failure_without_prior_local_context_restores_nothing(self):
        verdict = await make_transition_runtime().abort_failed_transition(
            spotify_request(),
            {"player": {}, "current_track": {"source": "spotify"}},
            target_staged=False,
        )
        self.assertIsNone(verdict)


class NativeQueueRestoreVerdictTests(unittest.IsolatedAsyncioTestCase):
    """A committed native MPV queue is carried into the restore request."""

    async def asyncSetUp(self):
        self._rate_patch = patch.object(
            main, "get_samplerate_status", return_value={"active_rate": 44_100}
        )
        self._rate_patch.start()

    async def asyncTearDown(self):
        self._rate_patch.stop()

    def _queue_state(self):
        # A committed native queue is homogeneous (validated by the queue
        # owner at commit time), so every entry carries its sample rate.
        queue = [
            {"source": "local", "url": "/music/a.flac", "id": "a", "sample_rate_hz": 44100},
            {"source": "local", "url": "/music/b.flac", "id": "b", "sample_rate_hz": 44100},
            {"source": "local", "url": "/music/c.flac", "id": "c", "sample_rate_hz": 44100},
        ]
        return queue

    async def test_native_queue_restore_request_rebuilds_full_playlist_with_index(self):
        queue = self._queue_state()
        saved_queue = queue_state()
        try:
            playback_queue.queue.tracks = [dict(item) for item in queue]
            playback_queue.queue.original = [dict(item) for item in queue]
            playback_queue.queue.index = 1
            playback_queue.queue.mode = "native_mpv"
            playback_queue.queue.loop = True
            playback_queue.queue.shuffle = False
            playback_queue.queue.single_track_loop = False
            verdict = await make_transition_runtime().abort_failed_transition(
                spotify_request(),
                local_snapshot(),
                target_staged=False,
            )
        finally:
            restore_queue_state(saved_queue)

        self.assertIsNotNone(verdict)
        restore = verdict["restore"]
        self.assertEqual(len(restore.native_queue), 3)
        self.assertEqual(restore.native_queue[0]["url"], "/music/a.flac")
        self.assertEqual(restore.native_queue[2]["url"], "/music/c.flac")
        self.assertEqual(restore.native_queue_index, 1)
        self.assertTrue(restore.native_queue_loop)


class RestoreRuntime:
    """Coordinator-facing runtime double with gate primitives and recorded
    restore stages."""

    def __init__(
        self,
        *,
        effects_result=None,
        graph_result=None,
        final_result=None,
        dsp_result=None,
        reconcile_result=None,
        fail_stage=None,
        release_result=True,
    ):
        self.muted = True
        self.events: list[str] = []
        self.requests: dict[str, TransitionRequest] = {}
        self.fail_stage = fail_stage
        self.release_result = release_result
        self.effects_result = effects_result or {"dsp_reinitialized": True, "helper_rebuilt": True}
        self.graph_result = graph_result or {"committed": True}
        self.final_result = final_result or {"committed": True, "source_volume": 100}
        self.dsp_result = dsp_result or {"stabilized": True}
        self.reconcile_result = reconcile_result or {"graph_complete": True}

    async def read_hardware_mute(self) -> bool:
        return self.muted

    async def set_hardware_mute(self, muted: bool, transition_id: str) -> None:
        self.muted = bool(muted)
        self.events.append(f"mute:{self.muted}")

    async def wait_for_pipewire_spotify_release(self) -> bool:
        self.events.append("spotify-release-confirmed")
        return self.release_result

    async def _stage(self, name, request, result=None):
        if self.fail_stage == name:
            raise RuntimeError(f"stage failed: {name}")
        self.events.append(name)
        self.requests[name] = request
        return result

    async def establish_target_rate(self, request):
        return await self._stage("establish_target_rate", request)

    async def establish_effects_and_helper(self, request):
        return await self._stage("establish_effects_and_helper", request, self.effects_result)

    async def prepare_target_source(self, request):
        return await self._stage("prepare_target_source", request)

    async def start_target_source(self, request):
        return await self._stage("start_target_source", request)

    async def reconcile_post_start_graph(self, request):
        return await self._stage("reconcile_post_start_graph", request, self.reconcile_result)

    async def verify_transition_graph(self, request):
        return await self._stage("verify_transition_graph", request, self.graph_result)

    async def set_source_volume(self, volume, transition_id):
        self.events.append("set_source_volume")

    async def stabilize_effects_after_rate_change(self, request, *, dsp_reinitialized=False):
        return await self._stage("stabilize_effects_after_rate_change", request, self.dsp_result)

    async def verify_committed_transition(self, request):
        return await self._stage("verify_committed_transition", request, self.final_result)


def make_restore_coordinator(
    runtime: RestoreRuntime, *, transition_id: str = "tr-failed"
) -> PlaybackTransitionCoordinator:
    """Build a Coordinator that already owns the closed output gate."""
    coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
    coordinator.gate.closed = True
    coordinator.gate.owner = "fxroute"
    coordinator.gate.transition_id = transition_id
    return coordinator


def run_restore(
    runtime: RestoreRuntime,
    *,
    failed_request: TransitionRequest | None = None,
    request: TransitionRequest | None = None,
    transition_id: str = "tr-failed",
    gate_required: bool = True,
    gate_recorder: list[str] | None = None,
    gate_fail_stage: str | None = None,
) -> bool:
    """Drive the Coordinator restore through its own stages under an owned
    gate.  ``gate_recorder`` collects the gate-confirmation stage labels;
    ``gate_fail_stage`` simulates a physical gate loss at one boundary."""
    coordinator = make_restore_coordinator(runtime, transition_id=transition_id)
    real_ensure = coordinator.ensure_output_gate_closed

    async def recording_ensure(transition_id_: str, *, stage: str) -> None:
        if gate_recorder is not None:
            gate_recorder.append(stage)
        if gate_fail_stage is not None and stage == gate_fail_stage:
            raise RuntimeError(f"output gate lost at {stage}")
        return await real_ensure(transition_id_, stage=stage)

    coordinator.ensure_output_gate_closed = recording_ensure
    return coordinator._restore_committed_source(
        failed_request or spotify_request(),
        request or restore_request(),
        transition_id=transition_id,
        gate_required=gate_required,
    )


class CoordinatorRestoreTests(unittest.IsolatedAsyncioTestCase):
    """The Coordinator runs the restore through its own stage sequence."""

    async def test_local_44100_to_48000_failure_restores_full_graph(self):
        runtime = RestoreRuntime()
        restored = await run_restore(runtime, request=restore_request(target_rate=44_100))
        self.assertTrue(restored)

        self.assertEqual(runtime.events, [
            "spotify-release-confirmed",
            "establish_target_rate",
            "establish_effects_and_helper",
            "prepare_target_source",
            "start_target_source",
            "reconcile_post_start_graph",
            "verify_transition_graph",
            "set_source_volume",
            "stabilize_effects_after_rate_change",
            "verify_committed_transition",
        ])
        start_request = runtime.requests["start_target_source"]
        self.assertTrue(start_request.should_play)
        self.assertEqual(start_request.target_url, "/music/old.flac")

    async def test_radio_restore_runs_same_sequence_with_radio_request(self):
        runtime = RestoreRuntime()
        restored = await run_restore(
            runtime, request=restore_request(source="radio", url="https://radio.example/live")
        )
        self.assertTrue(restored)

        self.assertEqual(runtime.events[-1], "verify_committed_transition")
        self.assertEqual(runtime.requests["establish_target_rate"].source, "radio")

    async def test_same_rate_skips_unnecessary_dsp_stabilization(self):
        runtime = RestoreRuntime(
            effects_result={"dsp_reinitialized": False, "helper_rebuilt": False}
        )
        restored = await run_restore(
            runtime, request=restore_request(target_rate=44_100, rate_change=False)
        )
        self.assertTrue(restored)

        self.assertEqual(runtime.events[1], "establish_target_rate")
        self.assertIn("establish_effects_and_helper", runtime.events)
        self.assertIn("verify_transition_graph", runtime.events)
        self.assertNotIn("stabilize_effects_after_rate_change", runtime.events)
        self.assertEqual(runtime.events[-1], "verify_committed_transition")

    async def test_paused_restore_keeps_paused_with_volume_but_without_dsp_stage(self):
        runtime = RestoreRuntime()
        restored = await run_restore(
            runtime, request=restore_request(should_play=False)
        )
        self.assertTrue(restored)

        # The source-volume invariant (MPV volume 100) holds also for a
        # paused restore; DSP stabilization keeps its rate/DSP condition.
        self.assertIn("set_source_volume", runtime.events)
        self.assertNotIn("stabilize_effects_after_rate_change", runtime.events)
        self.assertEqual(runtime.events[-1], "verify_committed_transition")

    async def test_final_readback_without_volume_100_aborts_restore(self):
        runtime = RestoreRuntime(final_result={"committed": True, "source_volume": 0})
        restored = await run_restore(
            runtime, request=restore_request(should_play=False)
        )
        self.assertFalse(restored)

    async def test_final_readback_without_volume_confirmation_aborts_restore(self):
        # source_volume missing/unusable is not a successful confirmation:
        # no recovery, no committed track metadata, failure latch stays.
        runtime = RestoreRuntime(final_result={"committed": True, "source_volume": None})
        restored = await run_restore(runtime)
        self.assertFalse(restored)

    async def test_final_readback_failure_keeps_latch(self):
        runtime = RestoreRuntime(final_result={"committed": False})
        restored = await run_restore(runtime)
        self.assertFalse(restored)

    async def test_stage_failure_keeps_latch_semantics(self):
        runtime = RestoreRuntime(fail_stage="prepare_target_source")
        restored = await run_restore(runtime)
        self.assertFalse(restored)

    async def test_spotify_sink_not_quiesced_aborts_restore_before_any_stage(self):
        # A verify failure after a successful Spotify start can leave the
        # sink active; without a confirmed release no old-graph stage runs.
        runtime = RestoreRuntime(release_result=False)
        restored = await run_restore(runtime)
        self.assertFalse(restored)
        self.assertEqual(runtime.events, ["spotify-release-confirmed"])

    async def test_spotify_release_confirmed_before_restore_stages(self):
        runtime = RestoreRuntime()
        restored = await run_restore(runtime)
        self.assertTrue(restored)

        self.assertEqual(runtime.events[0], "spotify-release-confirmed")
        self.assertEqual(runtime.events[1], "establish_target_rate")


class RestoreGateBoundaryTests(unittest.IsolatedAsyncioTestCase):
    """The restore re-confirms the physical gate at every critical boundary."""

    async def test_gate_guard_runs_at_all_critical_boundaries(self):
        runtime = RestoreRuntime()
        guard_calls: list[str] = []
        restored = await run_restore(runtime, gate_recorder=guard_calls)
        self.assertTrue(restored)

        self.assertEqual(guard_calls, [
                "failed-transition-restore-before-rate",
                "failed-transition-restore-after-rate",
                "failed-transition-restore-after-effects-helper",
                "failed-transition-restore-before-start",
                "failed-transition-restore-before-volume",
                "failed-transition-restore-after-dsp",
        ])

    async def test_initial_gate_guard_failure_aborts_before_any_mutating_stage(self):
        runtime = RestoreRuntime()
        restored = await run_restore(
            runtime, gate_fail_stage="failed-transition-restore-before-rate"
        )
        self.assertFalse(restored)

        # No single mutating restore stage ran: only the read-only Spotify
        # quiesce wait, then the restore aborted before any rate/effects/MPV/
        # graph/volume mutation under an unverified gate.
        self.assertEqual(runtime.events, ["spotify-release-confirmed"])

    async def test_gate_loss_before_volume_aborts_restore_without_volume_change(self):
        runtime = RestoreRuntime()
        restored = await run_restore(
            runtime, gate_fail_stage="failed-transition-restore-before-volume"
        )
        self.assertFalse(restored)

        # No source volume may ever be set under an unconfirmed gate.
        self.assertNotIn("set_source_volume", runtime.events)

    async def test_gate_guard_after_volume_runs_in_no_dsp_and_paused_paths(self):
        # Same-rate/no-DSP: the after-volume gate re-check still runs, like
        # the normal Coordinator after-dsp-stabilization boundary.
        runtime = RestoreRuntime(
            effects_result={"dsp_reinitialized": False, "helper_rebuilt": False}
        )
        guard_calls: list[str] = []
        restored = await run_restore(
            runtime,
            request=restore_request(rate_change=False),
            gate_recorder=guard_calls,
        )
        self.assertTrue(restored)
        self.assertEqual(guard_calls[-1], "failed-transition-restore-after-dsp")
        self.assertNotIn("stabilize_effects_after_rate_change", runtime.events)

        # Paused/no-DSP: same gate re-check after the volume restore.
        runtime = RestoreRuntime(
            effects_result={"dsp_reinitialized": False, "helper_rebuilt": False}
        )
        guard_calls = []
        restored = await run_restore(
            runtime,
            request=restore_request(should_play=False),
            gate_recorder=guard_calls,
        )
        self.assertTrue(restored)
        self.assertEqual(guard_calls[-1], "failed-transition-restore-after-dsp")
        self.assertNotIn("stabilize_effects_after_rate_change", runtime.events)

    async def test_gate_loss_after_volume_aborts_restore_before_final_readback(self):
        runtime = RestoreRuntime()
        restored = await run_restore(
            runtime, gate_fail_stage="failed-transition-restore-after-dsp"
        )
        self.assertFalse(restored)

        self.assertIn("set_source_volume", runtime.events)
        self.assertNotIn("verify_committed_transition", runtime.events)

    async def test_fast_path_restore_runs_ungated(self):
        # A transition that never closed the output gate (same-graph fast
        # path) cannot re-confirm a gate it does not own: no boundary checks.
        runtime = RestoreRuntime()
        guard_calls: list[str] = []
        restored = await run_restore(
            runtime, gate_required=False, gate_recorder=guard_calls
        )
        self.assertTrue(restored)
        self.assertEqual(guard_calls, [])


class PositionRestoreOrderTests(unittest.IsolatedAsyncioTestCase):
    """Local position restore happens paused/under the gate before start."""

    async def test_position_restore_orders_load_paused_seek_then_start(self):
        player = RecordingPlayer()
        runtime = make_transition_runtime()
        with patch.object(main.runtime, "player_instance", player), patch.object(
            main, "_load_player_paused",
            side_effect=lambda path: player.set_pause(True) or player._state.update(current_file=path),
        ), patch.object(
            main, "_wait_for_player_current_file", AsyncMock(return_value=True)
        ), patch.object(main, "_ensure_mpv_to_dsp_links", AsyncMock(return_value=True)):
            request = TransitionRequest(
                operation="replay",
                source="local",
                target_rate=44_100,
                target_url="/music/old.flac",
                target_track={"source": "local", "url": "/music/old.flac"},
                should_play=True,
                rate_change=False,
                reload_source=True,
                restore_position=37.25,
                detail="failed-transition-restore",
            )
            await runtime.prepare_target_source(request)
            await runtime.start_target_source(request)

        # volume 0 -> non-native loop/shuffle reset -> load paused -> re-pause
        # under the gate -> seek while paused -> only then start/unpause.
        self.assertEqual(player.ops, [
            "volume:0",
            "loop-playlist:False",
            "shuffle:False",
            "pause:True",
            "pause:True",
            "seek:37.25",
            "pause:False",
        ])
        self.assertEqual(player.state["position"], 37.25)
        self.assertTrue(player.state["playing"])
        self.assertFalse(player.state["paused"])

    async def test_paused_restore_volume_100_then_resume_starts_from_100(self):
        player = RecordingPlayer()
        runtime = make_transition_runtime()
        with patch.object(main.runtime, "player_instance", player), patch.object(
            main, "_load_player_paused",
            side_effect=lambda path: player.set_pause(True) or player._state.update(current_file=path),
        ), patch.object(
            main, "_wait_for_player_current_file", AsyncMock(return_value=True)
        ), patch.object(main, "_ensure_mpv_to_dsp_links", AsyncMock(return_value=True)):
            request = TransitionRequest(
                operation="replay",
                source="local",
                target_rate=44_100,
                target_url="/music/old.flac",
                target_track={"source": "local", "url": "/music/old.flac"},
                should_play=False,
                rate_change=False,
                reload_source=True,
                restore_position=37.25,
                detail="failed-transition-restore",
            )
            await runtime.prepare_target_source(request)
            await runtime.start_target_source(request)
            # The source-volume invariant applies to the paused restore too:
            # MPV volume 100 under the confirmed closed gate.
            await runtime.set_source_volume(100, "failed-transition-restore")

            self.assertTrue(player.state["paused"])
            self.assertFalse(player.state["playing"])
            self.assertEqual(player.state["volume"], 100)
            self.assertEqual(player.state["position"], 37.25)

            # A later pure pause-toggle/resume starts from volume 100, never
            # from the failure leftover volume 0.
            resume = TransitionRequest(
                operation="resume",
                source="local",
                target_rate=44_100,
                target_url="/music/old.flac",
                target_track={"source": "local", "url": "/music/old.flac"},
                should_play=True,
                rate_change=False,
                reload_source=False,
                detail="toggle-resume",
            )
            ops_before = list(player.ops)
            await runtime.start_target_source(resume)

        self.assertEqual(player.state["volume"], 100)
        # Resume unpauses; no volume:0 and no other transport mutation.
        self.assertEqual(player.ops, ops_before + ["pause:False"])
        self.assertTrue(player.state["playing"])
        self.assertFalse(player.state["paused"])


class RecordingPlayer:
    """Player double recording transport operations in call order."""

    _running = True

    def __init__(self) -> None:
        self.ops: list[str] = []
        self._state = {
            "current_file": None,
            "playing": False,
            "paused": True,
            "ended": False,
            "volume": 0,
            "position": 0.0,
        }

    @property
    def state(self) -> dict:
        return self._state

    def set_pause(self, paused: bool) -> None:
        self.ops.append(f"pause:{bool(paused)}")
        self._state["paused"] = bool(paused)
        self._state["playing"] = not bool(paused) and bool(self._state["current_file"])

    def set_volume(self, volume: int) -> None:
        self.ops.append(f"volume:{volume}")
        self._state["volume"] = volume

    def seek(self, position: float) -> None:
        self.ops.append(f"seek:{position}")
        self._state["position"] = position

    def set_loop_playlist(self, enabled: bool) -> None:
        self.ops.append(f"loop-playlist:{bool(enabled)}")

    def set_shuffle(self, enabled: bool) -> None:
        self.ops.append(f"shuffle:{bool(enabled)}")


class NativeQueueRestoreFailureTests(unittest.IsolatedAsyncioTestCase):
    """A failed native-playlist restore normalizes the retained queue."""

    def _queue_state(self):
        queue = [
            {"source": "local", "url": "/music/a.flac", "id": "a", "sample_rate_hz": 44100},
            {"source": "local", "url": "/music/b.flac", "id": "b", "sample_rate_hz": 44100},
            {"source": "local", "url": "/music/c.flac", "id": "c", "sample_rate_hz": 44100},
        ]
        return queue

    async def test_restore_failure_normalizes_to_app_replace(self):
        queue = self._queue_state()
        saved_queue = queue_state()
        try:
            playback_queue.queue.tracks = [dict(item) for item in queue]
            playback_queue.queue.original = [dict(item) for item in queue]
            playback_queue.queue.index = 1
            playback_queue.queue.mode = "native_mpv"
            playback_queue.queue.loop = False
            playback_queue.queue.shuffle = False
            playback_queue.queue.single_track_loop = False

            class NativeRestoreFailingRuntime(FakeRuntime):
                async def abort_failed_transition(self, request, snapshot, *, target_staged):
                    return {"restore": restore_request()}

                async def normalize_queue_after_native_loss(self):
                    self.normalize_calls += 1
                    playback_queue.queue.normalize_after_native_loss()

            runtime = NativeRestoreFailingRuntime(muted=False, fail_stage="verify-graph")
            runtime.normalize_calls = 0
            coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
            with patch.object(
                playback_queue.queue, "reduce_native_playlist_to_current"
            ), patch.object(playback_queue.queue, "reset_mpv_loop_state"):
                with self.assertRaises(PlaybackTransitionFailure):
                    await coordinator.execute(spotify_request())

                self.assertTrue(coordinator.gate.failure_latched)
                self.assertEqual(playback_queue.queue.mode, "app_replace")
                self.assertEqual(runtime.normalize_calls, 1)
                playback_queue.queue.reduce_native_playlist_to_current.assert_called_once()
                playback_queue.queue.reset_mpv_loop_state.assert_called_once()
        finally:
            restore_queue_state(saved_queue)


class CoordinatorGateRestoreTests(unittest.IsolatedAsyncioTestCase):
    """The Coordinator opens the gate only after a recovered abort."""

    async def test_coordinator_opens_gate_when_abort_recovers_source(self):
        runtime = FakeRuntime(muted=False, fail_stage="start")

        class RecoveredAbortRuntime(FakeRuntime):
            async def abort_failed_transition(
                self, request, snapshot, *, target_staged
            ):
                self.events.append("abort-restore-complete")
                # The restore is a fresh bounded sequence for the committed
                # source; it must not re-hit the original start failure.
                self.fail_stage = None
                return {"restore": restore_request()}

        runtime = RecoveredAbortRuntime(muted=False, fail_stage="start")
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        with self.assertRaises(PlaybackTransitionFailure) as cm:
            await coordinator.execute(spotify_request())

        # The restored source is fully confirmed inside the Coordinator
        # restore before the output gate opens (mute:False).
        self.assertLess(
            runtime.events.index("abort-restore-complete"),
            runtime.events.index("mute:False"),
        )
        self.assertFalse(coordinator.gate.failure_latched)
        self.assertFalse(coordinator.gate.closed)
        self.assertFalse(runtime.muted)
        self.assertFalse(runtime.dsp_muted)
        # The failure status reflects the real, opened gate.
        self.assertFalse(cm.exception.failure_latched)
        self.assertFalse(coordinator.last_error["failure_latched"])

    async def test_coordinator_recloses_lost_gate_before_opening_after_recovery(self):
        # The gate goes physically open during the abort restore; the final
        # Coordinator gate sequence re-mutes (mute:True), holds, and only then
        # opens (mute:False) - never an unverified open.
        runtime = FakeRuntime(muted=False, fail_stage="start")

        class GateLossAbortRuntime(FakeRuntime):
            async def abort_failed_transition(
                self, request, snapshot, *, target_staged
            ):
                self.events.append("abort-restore-complete")
                self.fail_stage = None
                self.muted = False
                return {"restore": restore_request()}

        runtime = GateLossAbortRuntime(muted=False, fail_stage="start")
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        with self.assertRaises(PlaybackTransitionFailure) as cm:
            await coordinator.execute(spotify_request())

        self.assertLess(
            runtime.events.index("abort-restore-complete"),
            runtime.events.index("mute:False"),
        )
        re_mute_index = len(runtime.events) - 1 - runtime.events[::-1].index("mute:True")
        self.assertGreater(re_mute_index, runtime.events.index("abort-restore-complete"))
        self.assertLess(re_mute_index, runtime.events.index("mute:False"))
        self.assertFalse(coordinator.gate.failure_latched)
        self.assertFalse(coordinator.gate.closed)
        self.assertFalse(runtime.muted)
        self.assertFalse(cm.exception.failure_latched)

    async def test_coordinator_keeps_latch_when_abort_does_not_recover(self):
        runtime = FakeRuntime(muted=False, fail_stage="start")
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        with self.assertRaises(PlaybackTransitionFailure) as cm:
            await coordinator.execute(spotify_request())

        self.assertTrue(coordinator.gate.failure_latched)
        self.assertTrue(coordinator.gate.closed)
        self.assertTrue(runtime.muted)
        self.assertTrue(cm.exception.failure_latched)
        self.assertTrue(coordinator.last_error["failure_latched"])

    async def test_coordinator_latches_when_gate_restore_fails_after_recovery(self):
        runtime = FakeRuntime(muted=False, fail_stage="start")

        class RecoveredAbortRuntime(FakeRuntime):
            async def abort_failed_transition(
                self, request, snapshot, *, target_staged
            ):
                return {"restore": restore_request()}

        runtime = RecoveredAbortRuntime(muted=False, fail_stage="start")
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        async def broken_restore_gate(
            transition_id, *, audible_output=False, after_physical_restore=None
        ):
            raise RuntimeError("gate restore failed")

        coordinator._restore_gate = broken_restore_gate
        with self.assertRaises(PlaybackTransitionFailure) as cm:
            await coordinator.execute(spotify_request())

        self.assertTrue(coordinator.gate.failure_latched)
        self.assertTrue(coordinator.gate.closed)
        # Source was recovered but the gate restore failed: latched again.
        self.assertTrue(cm.exception.failure_latched)
        self.assertTrue(coordinator.last_error["failure_latched"])

    async def test_gate_close_failure_prevents_restore_under_unverified_gate(self):
        # The original Spotify transition failed at output-gate-close (the
        # first hardware mute read failed after the mute was issued).  The
        # Coordinator's first restore boundary check cannot confirm a closed
        # gate, so no rate/graph restore may start and the failure stays
        # latched.
        runtime = FakeRuntime(muted=False, fail_mute_read_number=1)

        class GuardUsingAbortRuntime(FakeRuntime):
            async def abort_failed_transition(
                self, request, snapshot, *, target_staged
            ):
                self.events.append("abort-verdict-returned")
                return {"restore": restore_request()}

        runtime = GuardUsingAbortRuntime(muted=False, fail_mute_read_number=1)
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        with self.assertRaises(PlaybackTransitionFailure) as cm:
            await coordinator.execute(spotify_request())

        self.assertNotIn("rate", runtime.events)
        self.assertTrue(coordinator.gate.failure_latched)
        self.assertTrue(coordinator.gate.closed)
        self.assertTrue(runtime.muted)
        self.assertTrue(cm.exception.failure_latched)
        self.assertTrue(coordinator.last_error["failure_latched"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
