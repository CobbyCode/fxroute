#!/usr/bin/env python3
"""Focused tests for the full graph restore after a failed Spotify handoff
(P1-2 completion).

When Spotify fails before commit and a Local/Radio source was committed
before, abort_failed_transition physically restores that source AND its full
playback graph by running the same bounded Coordinator stage primitives under
the still-closed output gate (never a nested Coordinator transition):

  old rate -> effects/helper for the old rate -> source/queue transport
  (including a committed native MPV playlist) -> post-start graph reconcile
  -> staged graph readback -> DSP stabilization when the failed Spotify
  transition reinitialized the DSP -> final commit readback.

abort_failed_transition returns True only when every stage confirmed the old
source; any stage failure keeps the Coordinator failure latch as the safe
state.  The Coordinator then restores the output gate (no failure latch).
"""

from __future__ import annotations

import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import main
from playback_transition_test_support import make_transition_runtime
from playback_transition import (
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


class StageRecorder:
    """Collects per-stage call events and captured requests."""

    def __init__(self, runtime, *, effects_result=None, graph_result=None,
                 final_result=None, dsp_result=None, reconcile_result=None,
                 fail_stage=None):
        self.events: list[str] = []
        self.requests: dict[str, TransitionRequest] = {}
        self.effects_result = effects_result or {"dsp_reinitialized": True, "helper_rebuilt": True}
        self.graph_result = graph_result or {"committed": True}
        self.final_result = final_result or {"committed": True, "source_volume": 100}
        self.dsp_result = dsp_result or {"stabilized": True}
        self.reconcile_result = reconcile_result or {"graph_complete": True}
        self.fail_stage = fail_stage

        def _stage_factory(name, result=None):
            async def _mock(request, *args, **kwargs):
                if self.fail_stage == name:
                    raise RuntimeError(f"stage failed: {name}")
                self.events.append(name)
                self.requests[name] = request
                return result
            return _mock

        runtime.establish_target_rate = _stage_factory("establish_target_rate")
        runtime.establish_effects_and_helper = _stage_factory(
            "establish_effects_and_helper", self.effects_result
        )
        runtime.prepare_target_source = _stage_factory("prepare_target_source")
        runtime.start_target_source = _stage_factory("start_target_source")
        runtime.reconcile_post_start_graph = _stage_factory(
            "reconcile_post_start_graph", self.reconcile_result
        )
        runtime.verify_transition_graph = _stage_factory(
            "verify_transition_graph", self.graph_result
        )
        runtime.set_source_volume = _stage_factory("set_source_volume")
        runtime.stabilize_effects_after_rate_change = _stage_factory(
            "stabilize_effects_after_rate_change", self.dsp_result
        )
        runtime.verify_committed_transition = _stage_factory(
            "verify_committed_transition", self.final_result
        )


def _abort(runtime, snapshot, request=None, *, ensure_gate_closed=None):
    return runtime.abort_failed_transition(
        request or spotify_request(),
        snapshot,
        target_staged=False,
        ensure_gate_closed=ensure_gate_closed,
    )


class RestoreTestBase(unittest.IsolatedAsyncioTestCase):
    """Common patches: the live samplerate status and the Spotify release
    helper are always exercised by the restore and must never hit real
    subprocesses."""

    async def asyncSetUp(self):
        self._rate_patch = patch.object(
            main, "get_samplerate_status", return_value={"active_rate": 44_100}
        )
        self._rate_patch.start()
        self._release_patch = patch.object(
            main, "_wait_for_pipewire_spotify_release", AsyncMock(return_value=True)
        )
        self._release_patch.start()

    async def asyncTearDown(self):
        self._release_patch.stop()
        self._rate_patch.stop()


class FullGraphRestoreTests(RestoreTestBase):
    """The abort restore runs the complete stage sequence before True."""

    async def test_local_44100_to_48000_failure_restores_full_graph(self):
        runtime = make_transition_runtime()
        recorder = StageRecorder(runtime)
        with patch.object(main, "current_track_info", None), patch.object(
            main, "current_footer_owner", "spotify"
        ), patch.object(main, "_mark_player_state_authoritative"):
            restored = await _abort(runtime, local_snapshot())
            self.assertTrue(restored)
            self.assertEqual(main.current_track_info["source"], "local")
            self.assertEqual(main.current_footer_owner, "local")

        self.assertEqual(recorder.events, [
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
        rate_request = recorder.requests["establish_target_rate"]
        self.assertEqual(rate_request.target_rate, 44_100)
        self.assertEqual(rate_request.source, "local")
        self.assertTrue(rate_request.rate_change)
        self.assertEqual(rate_request.restore_position, 12.5)
        start_request = recorder.requests["start_target_source"]
        self.assertTrue(start_request.should_play)
        self.assertEqual(start_request.target_url, "/music/old.flac")

    async def test_same_rate_48k_to_48k_failure_keeps_authoritative_rate(self):
        # Local 48 kHz committed; Spotify also targets 48 kHz, so the failed
        # request carried rate_change=False.  The restore must still use the
        # authoritative committed active_rate and validate the full graph.
        runtime = make_transition_runtime()
        recorder = StageRecorder(
            runtime,
            effects_result={"dsp_reinitialized": False, "helper_rebuilt": False},
        )
        snapshot = local_snapshot(force_rate=None, active_rate=48_000)
        with patch.object(main, "current_track_info", None), patch.object(
            main, "current_footer_owner", "spotify"
        ), patch.object(main, "_mark_player_state_authoritative"), patch.object(
            main, "get_samplerate_status", return_value={"active_rate": 48_000}
        ):
            restored = await _abort(
                runtime, snapshot, request=spotify_request(rate_change=False)
            )
            self.assertTrue(restored)

        rate_request = recorder.requests["establish_target_rate"]
        self.assertEqual(rate_request.target_rate, 48_000)
        self.assertFalse(rate_request.rate_change)
        # Full graph validation still ran; no unnecessary rate rebuild/DSP.
        self.assertEqual(recorder.events[0], "establish_target_rate")
        self.assertIn("establish_effects_and_helper", recorder.events)
        self.assertIn("verify_transition_graph", recorder.events)
        self.assertEqual(recorder.events[-1], "verify_committed_transition")
        self.assertNotIn("stabilize_effects_after_rate_change", recorder.events)

    async def test_fixed_policy_track_rate_is_not_trusted_over_committed_active_rate(self):
        # Track metadata says 44.1 kHz but the committed hardware state was
        # 48 kHz (fixed policy): the restore must take the committed rate.
        runtime = make_transition_runtime()
        recorder = StageRecorder(
            runtime,
            effects_result={"dsp_reinitialized": False, "helper_rebuilt": False},
        )
        snapshot = local_snapshot(force_rate=None, active_rate=48_000)
        snapshot["current_track"]["sample_rate_hz"] = 44_100
        with patch.object(main, "current_track_info", None), patch.object(
            main, "current_footer_owner", "spotify"
        ), patch.object(main, "_mark_player_state_authoritative"), patch.object(
            main, "get_samplerate_status", return_value={"active_rate": 48_000}
        ):
            restored = await _abort(
                runtime, snapshot, request=spotify_request(rate_change=False)
            )
            self.assertTrue(restored)

        rate_request = recorder.requests["establish_target_rate"]
        self.assertEqual(rate_request.target_rate, 48_000)

    async def test_unknown_live_rate_is_treated_as_rate_change(self):
        # Same-rate 48k restore with unavailable live status: rate_change
        # must be conservative True so effects/helper are validated, while
        # establish_target_rate remains an idempotent no-op if the hardware
        # already stands at the committed rate.
        runtime = make_transition_runtime()
        recorder = StageRecorder(runtime)
        snapshot = local_snapshot(force_rate=None, active_rate=48_000)
        with patch.object(main, "current_track_info", None), patch.object(
            main, "current_footer_owner", "spotify"
        ), patch.object(main, "_mark_player_state_authoritative"), patch.object(
            main, "get_samplerate_status",
            Mock(side_effect=RuntimeError("samplerate status unavailable")),
        ):
            restored = await _abort(
                runtime, snapshot, request=spotify_request(rate_change=False)
            )
            self.assertTrue(restored)

        rate_request = recorder.requests["establish_target_rate"]
        self.assertEqual(rate_request.target_rate, 48_000)
        self.assertTrue(rate_request.rate_change)
        self.assertIn("establish_effects_and_helper", recorder.events)

    async def test_local_position_restored_for_playing_and_paused(self):
        runtime = make_transition_runtime()
        recorder = StageRecorder(runtime)
        with patch.object(main, "current_track_info", None), patch.object(
            main, "current_footer_owner", "spotify"
        ), patch.object(main, "_mark_player_state_authoritative"):
            restored = await _abort(
                runtime,
                local_snapshot(playing=True, position=37.25),
            )
            self.assertTrue(restored)

        self.assertEqual(
            recorder.requests["prepare_target_source"].restore_position, 37.25
        )

        runtime = make_transition_runtime()
        recorder = StageRecorder(runtime)
        with patch.object(main, "current_track_info", None), patch.object(
            main, "current_footer_owner", "spotify"
        ), patch.object(main, "_mark_player_state_authoritative"):
            restored = await _abort(
                runtime,
                local_snapshot(playing=False, position=37.25),
            )
            self.assertTrue(restored)

        self.assertEqual(
            recorder.requests["prepare_target_source"].restore_position, 37.25
        )
        self.assertFalse(recorder.requests["start_target_source"].should_play)

    async def test_radio_gets_no_position_restore(self):
        runtime = make_transition_runtime()
        recorder = StageRecorder(runtime)
        with patch.object(main, "current_track_info", None), patch.object(
            main, "current_footer_owner", "spotify"
        ), patch.object(main, "_mark_player_state_authoritative"):
            restored = await _abort(runtime, radio_snapshot())
            self.assertTrue(restored)

        self.assertIsNone(recorder.requests["prepare_target_source"].restore_position)

    async def test_spotify_sink_not_quiesced_aborts_restore_before_any_stage(self):
        # A verify failure after a successful Spotify start can leave the
        # sink active; without a confirmed release no old-graph stage runs.
        runtime = make_transition_runtime()
        recorder = StageRecorder(runtime)
        release_mock = AsyncMock(return_value=False)
        with patch.object(main, "current_track_info", None), patch.object(
            main, "current_footer_owner", "spotify"
        ), patch.object(main, "_mark_player_state_authoritative"), patch.object(
            main, "_wait_for_pipewire_spotify_release", release_mock
        ):
            restored = await _abort(runtime, local_snapshot())
            self.assertIsNone(restored)
            self.assertIsNone(main.current_track_info)

        release_mock.assert_awaited_once()
        self.assertEqual(recorder.events, [])

    async def test_spotify_release_confirmed_before_restore_stages(self):
        # Early start failure without an active sink: the bounded release
        # helper confirms quickly and the normal rollback runs afterwards.
        runtime = make_transition_runtime()
        recorder = StageRecorder(runtime)

        def record_release():
            recorder.events.insert(0, "spotify-release-confirmed")
            return True

        release_mock = AsyncMock(side_effect=record_release)
        with patch.object(main, "current_track_info", None), patch.object(
            main, "current_footer_owner", "spotify"
        ), patch.object(main, "_mark_player_state_authoritative"), patch.object(
            main, "_wait_for_pipewire_spotify_release", release_mock
        ):
            restored = await _abort(runtime, local_snapshot())
            self.assertTrue(restored)

        release_mock.assert_awaited_once()
        self.assertEqual(recorder.events[0], "spotify-release-confirmed")
        self.assertEqual(recorder.events[1], "establish_target_rate")

    async def test_final_readback_without_volume_100_aborts_restore(self):
        runtime = make_transition_runtime()
        StageRecorder(runtime, final_result={"committed": True, "source_volume": 0})
        with patch.object(main, "current_track_info", None), patch.object(
            main, "current_footer_owner", "spotify"
        ), patch.object(main, "_mark_player_state_authoritative"):
            restored = await _abort(runtime, local_snapshot(playing=False))
            self.assertIsNone(restored)
            self.assertIsNone(main.current_track_info)

    async def test_final_readback_without_volume_confirmation_aborts_restore(self):
        # source_volume missing/unusable is not a successful confirmation:
        # no recovery, no committed track metadata, failure latch stays.
        runtime = make_transition_runtime()
        StageRecorder(runtime, final_result={"committed": True, "source_volume": None})
        with patch.object(main, "current_track_info", None), patch.object(
            main, "current_footer_owner", "spotify"
        ), patch.object(main, "_mark_player_state_authoritative"):
            restored = await _abort(runtime, local_snapshot())
            self.assertIsNone(restored)
            self.assertIsNone(main.current_track_info)
            self.assertEqual(main.current_footer_owner, "spotify")

    async def test_radio_restore_runs_same_sequence_with_radio_request(self):
        runtime = make_transition_runtime()
        recorder = StageRecorder(runtime)
        with patch.object(main, "current_track_info", None), patch.object(
            main, "current_footer_owner", "spotify"
        ), patch.object(main, "_mark_player_state_authoritative"):
            restored = await _abort(runtime, radio_snapshot())
            self.assertTrue(restored)
            self.assertEqual(main.current_track_info["source"], "radio")

        self.assertEqual(recorder.events[-1], "verify_committed_transition")
        rate_request = recorder.requests["establish_target_rate"]
        self.assertEqual(rate_request.source, "radio")
        self.assertEqual(rate_request.target_rate, 44_100)
        self.assertEqual(
            recorder.requests["prepare_target_source"].native_queue, None
        )

    async def test_paused_restore_keeps_paused_with_volume_but_without_dsp_stage(self):
        runtime = make_transition_runtime()
        recorder = StageRecorder(runtime)
        with patch.object(main, "current_track_info", None), patch.object(
            main, "current_footer_owner", "spotify"
        ), patch.object(main, "_mark_player_state_authoritative"):
            restored = await _abort(
                runtime, local_snapshot(playing=False)
            )
            self.assertTrue(restored)

        start_request = recorder.requests["start_target_source"]
        self.assertFalse(start_request.should_play)
        # The source-volume invariant (MPV volume 100) holds also for a
        # paused restore; DSP stabilization keeps its rate/DSP condition.
        self.assertIn("set_source_volume", recorder.events)
        self.assertNotIn("stabilize_effects_after_rate_change", recorder.events)
        self.assertEqual(recorder.events[-1], "verify_committed_transition")

    async def test_no_rate_change_skips_unnecessary_dsp_stabilization(self):
        runtime = make_transition_runtime()
        recorder = StageRecorder(
            runtime, effects_result={"dsp_reinitialized": False, "helper_rebuilt": False}
        )
        with patch.object(main, "current_track_info", None), patch.object(
            main, "current_footer_owner", "spotify"
        ), patch.object(main, "_mark_player_state_authoritative"):
            restored = await _abort(
                runtime, local_snapshot(), request=spotify_request(rate_change=False)
            )
            self.assertTrue(restored)

        # Same-rate: the authoritative rate stage still runs as a no-op and
        # the full graph is validated; only the DSP stabilization is skipped.
        self.assertEqual(recorder.events[0], "establish_target_rate")
        self.assertIn("establish_effects_and_helper", recorder.events)
        self.assertIn("verify_transition_graph", recorder.events)
        self.assertNotIn("stabilize_effects_after_rate_change", recorder.events)
        self.assertFalse(recorder.requests["start_target_source"].rate_change)

    async def test_stage_failure_keeps_latch_semantics(self):
        runtime = make_transition_runtime()
        StageRecorder(runtime, fail_stage="prepare_target_source")
        with patch.object(main, "current_track_info", None), patch.object(
            main, "current_footer_owner", "spotify"
        ), patch.object(main, "_mark_player_state_authoritative"):
            restored = await _abort(runtime, local_snapshot())
            self.assertIsNone(restored)
            self.assertIsNone(main.current_track_info)
            self.assertEqual(main.current_footer_owner, "spotify")

    async def test_final_readback_failure_keeps_latch(self):
        runtime = make_transition_runtime()
        StageRecorder(runtime, final_result={"committed": False})
        with patch.object(main, "current_track_info", None), patch.object(
            main, "current_footer_owner", "spotify"
        ), patch.object(main, "_mark_player_state_authoritative"):
            restored = await _abort(runtime, local_snapshot())
            self.assertIsNone(restored)
            self.assertIsNone(main.current_track_info)


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


class RestoreGateBoundaryTests(RestoreTestBase):
    """The restore re-confirms the physical gate at every critical boundary."""

    async def test_gate_guard_runs_at_all_critical_boundaries(self):
        runtime = make_transition_runtime()
        recorder = StageRecorder(runtime)
        guard_calls: list[str] = []

        async def gate_guard(*, stage):
            guard_calls.append(stage)

        with patch.object(main, "current_track_info", None), patch.object(
            main, "current_footer_owner", "spotify"
        ), patch.object(main, "_mark_player_state_authoritative"):
            restored = await _abort(
                runtime, local_snapshot(), ensure_gate_closed=gate_guard
            )
            self.assertTrue(restored)

        self.assertEqual(guard_calls, [
            "spotify-abort-restore-before-rate",
            "spotify-abort-restore-after-rate",
            "spotify-abort-restore-after-effects-helper",
            "spotify-abort-restore-before-start",
            "spotify-abort-restore-before-volume",
            "spotify-abort-restore-after-dsp",
        ])

    async def test_initial_gate_guard_failure_aborts_before_any_mutating_stage(self):
        runtime = make_transition_runtime()
        recorder = StageRecorder(runtime)

        async def failing_guard(*, stage):
            if stage == "spotify-abort-restore-before-rate":
                raise RuntimeError("output gate could not be confirmed closed")

        with patch.object(main, "current_track_info", None), patch.object(
            main, "current_footer_owner", "spotify"
        ), patch.object(main, "_mark_player_state_authoritative"):
            restored = await _abort(
                runtime, local_snapshot(), ensure_gate_closed=failing_guard
            )
            self.assertIsNone(restored)

        # No single mutating restore stage ran: no rate, effects/helper, MPV
        # load, graph or volume mutation under an unverified gate.
        self.assertEqual(recorder.events, [])
        self.assertIsNone(main.current_track_info)

    async def test_gate_loss_before_volume_aborts_restore_without_volume_change(self):
        runtime = make_transition_runtime()
        recorder = StageRecorder(runtime)

        async def failing_guard(*, stage):
            if stage == "spotify-abort-restore-before-volume":
                raise RuntimeError("output gate lost before volume restore")

        with patch.object(main, "current_track_info", None), patch.object(
            main, "current_footer_owner", "spotify"
        ), patch.object(main, "_mark_player_state_authoritative"):
            restored = await _abort(
                runtime, local_snapshot(), ensure_gate_closed=failing_guard
            )
            self.assertIsNone(restored)

        # No source volume may ever be set under an unconfirmed gate.
        self.assertNotIn("set_source_volume", recorder.events)
        self.assertIsNone(main.current_track_info)

    async def test_gate_guard_after_volume_runs_in_no_dsp_and_paused_paths(self):
        # Same-rate/no-DSP: the after-volume gate re-check still runs, like
        # the normal Coordinator after-dsp-stabilization boundary.
        runtime = make_transition_runtime()
        recorder = StageRecorder(
            runtime,
            effects_result={"dsp_reinitialized": False, "helper_rebuilt": False},
        )
        guard_calls: list[str] = []

        async def gate_guard(*, stage):
            guard_calls.append(stage)

        with patch.object(main, "current_track_info", None), patch.object(
            main, "current_footer_owner", "spotify"
        ), patch.object(main, "_mark_player_state_authoritative"):
            restored = await _abort(
                runtime,
                local_snapshot(),
                request=spotify_request(rate_change=False),
                ensure_gate_closed=gate_guard,
            )
            self.assertTrue(restored)

        self.assertEqual(guard_calls[-1], "spotify-abort-restore-after-dsp")
        self.assertNotIn("stabilize_effects_after_rate_change", recorder.events)

        # Paused/no-DSP: same gate re-check after the volume restore.
        runtime = make_transition_runtime()
        recorder = StageRecorder(
            runtime,
            effects_result={"dsp_reinitialized": False, "helper_rebuilt": False},
        )
        guard_calls = []

        with patch.object(main, "current_track_info", None), patch.object(
            main, "current_footer_owner", "spotify"
        ), patch.object(main, "_mark_player_state_authoritative"):
            restored = await _abort(
                runtime, local_snapshot(playing=False), ensure_gate_closed=gate_guard
            )
            self.assertTrue(restored)

        self.assertEqual(guard_calls[-1], "spotify-abort-restore-after-dsp")
        self.assertNotIn("stabilize_effects_after_rate_change", recorder.events)

    async def test_gate_loss_after_volume_aborts_restore_before_final_readback(self):
        runtime = make_transition_runtime()
        recorder = StageRecorder(runtime)

        async def failing_guard(*, stage):
            if stage == "spotify-abort-restore-after-dsp":
                raise RuntimeError("output gate lost after volume restore")

        with patch.object(main, "current_track_info", None), patch.object(
            main, "current_footer_owner", "spotify"
        ), patch.object(main, "_mark_player_state_authoritative"):
            restored = await _abort(
                runtime, local_snapshot(), ensure_gate_closed=failing_guard
            )
            self.assertIsNone(restored)

        self.assertIn("set_source_volume", recorder.events)
        self.assertNotIn("verify_committed_transition", recorder.events)
        self.assertIsNone(main.current_track_info)


class PositionRestoreOrderTests(unittest.IsolatedAsyncioTestCase):
    """Local position restore happens paused/under the gate before start."""

    async def test_position_restore_orders_load_paused_seek_then_start(self):
        player = RecordingPlayer()
        runtime = make_transition_runtime()
        with patch.object(main, "player_instance", player), patch.object(
            main, "_load_player_paused",
            side_effect=lambda path: player.set_pause(True) or player._state.update(current_file=path),
        ), patch.object(
            main, "_wait_for_player_current_file", AsyncMock(return_value=True)
        ), patch.object(main, "_ensure_mpv_to_easyeffects_links", AsyncMock(return_value=True)):
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
                detail="spotify-abort-restore",
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
        with patch.object(main, "player_instance", player), patch.object(
            main, "_load_player_paused",
            side_effect=lambda path: player.set_pause(True) or player._state.update(current_file=path),
        ), patch.object(
            main, "_wait_for_player_current_file", AsyncMock(return_value=True)
        ), patch.object(main, "_ensure_mpv_to_easyeffects_links", AsyncMock(return_value=True)):
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
                detail="spotify-abort-restore",
            )
            await runtime.prepare_target_source(request)
            await runtime.start_target_source(request)
            # The source-volume invariant applies to the paused restore too:
            # MPV volume 100 under the confirmed closed gate.
            await runtime.set_source_volume(100, "spotify-abort-restore")

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


class NativeQueueRestoreTests(RestoreTestBase):
    """A committed native MPV queue is rebuilt before the restore is confirmed."""

    def _queue_state(self):
        queue = [
            {"source": "local", "url": "/music/a.flac", "id": "a"},
            {"source": "local", "url": "/music/b.flac", "id": "b"},
            {"source": "local", "url": "/music/c.flac", "id": "c"},
        ]
        return queue

    async def test_native_queue_restore_rebuilds_full_playlist_with_index(self):
        queue = self._queue_state()
        runtime = make_transition_runtime()
        recorder = StageRecorder(runtime)
        with patch.object(main, "current_track_info", None), patch.object(
            main, "current_footer_owner", "spotify"
        ), patch.object(main, "_mark_player_state_authoritative"), patch.object(
            main, "playback_queue", [dict(item) for item in queue]
        ), patch.object(main, "playback_queue_index", 1), patch.object(
            main, "playback_queue_mode", "native_mpv"
        ), patch.object(main, "playback_queue_loop", True), patch.object(
            main, "playback_queue_shuffle", False
        ), patch.object(main, "single_track_loop", False):
            restored = await _abort(runtime, local_snapshot())
            self.assertTrue(restored)
            self.assertEqual(main.playback_queue_mode, "native_mpv")

        prepare_request = recorder.requests["prepare_target_source"]
        self.assertEqual(len(prepare_request.native_queue), 3)
        self.assertEqual(prepare_request.native_queue[0]["url"], "/music/a.flac")
        self.assertEqual(prepare_request.native_queue[2]["url"], "/music/c.flac")
        self.assertEqual(prepare_request.native_queue_index, 1)
        self.assertTrue(prepare_request.native_queue_loop)
        self.assertIsNone(prepare_request.native_queue_jump)

    async def test_native_queue_restore_failure_normalizes_to_app_replace(self):
        queue = self._queue_state()
        runtime = make_transition_runtime()
        recorder = StageRecorder(runtime, fail_stage="verify_transition_graph")
        with patch.object(main, "current_track_info", None), patch.object(
            main, "current_footer_owner", "spotify"
        ), patch.object(main, "_mark_player_state_authoritative"), patch.object(
            main, "playback_queue", [dict(item) for item in queue]
        ), patch.object(main, "playback_queue_index", 1), patch.object(
            main, "playback_queue_mode", "native_mpv"
        ), patch.object(main, "playback_queue_loop", False), patch.object(
            main, "playback_queue_shuffle", False
        ), patch.object(main, "single_track_loop", False), patch.object(
            main, "_reduce_native_mpv_playlist_to_current"
        ), patch.object(main, "_reset_mpv_loop_state"):
            restored = await _abort(runtime, local_snapshot())
            self.assertIsNone(restored)
            self.assertEqual(main.playback_queue_mode, "app_replace")
            main._reduce_native_mpv_playlist_to_current.assert_called_once()
            main._reset_mpv_loop_state.assert_called_once()


class CoordinatorGateRestoreTests(unittest.IsolatedAsyncioTestCase):
    """The Coordinator opens the gate only after a recovered abort."""

    async def test_coordinator_opens_gate_when_abort_recovers_source(self):
        runtime = FakeRuntime(muted=False, fail_stage="start")

        class RecoveredAbortRuntime(FakeRuntime):
            async def abort_failed_transition(
                self, request, snapshot, *, target_staged, ensure_gate_closed=None
            ):
                self.events.append("abort-restore-complete")
                return True

        runtime = RecoveredAbortRuntime(muted=False, fail_stage="start")
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        with self.assertRaises(PlaybackTransitionFailure) as cm:
            await coordinator.execute(spotify_request())

        # The restored source is fully confirmed inside the abort hook before
        # the Coordinator opens the output gate (mute:False).
        self.assertLess(
            runtime.events.index("abort-restore-complete"),
            runtime.events.index("mute:False"),
        )
        self.assertFalse(coordinator.gate.failure_latched)
        self.assertFalse(coordinator.gate.closed)
        self.assertFalse(runtime.muted)
        self.assertFalse(runtime.easyeffects_muted)
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
                self, request, snapshot, *, target_staged, ensure_gate_closed=None
            ):
                self.events.append("abort-restore-complete")
                self.muted = False
                return True

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
                self, request, snapshot, *, target_staged, ensure_gate_closed=None
            ):
                return True

        runtime = RecoveredAbortRuntime(muted=False, fail_stage="start")
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
        real_read = runtime.read_hardware_mute
        fail_reads = {"enabled": False}

        async def controlled_read():
            if fail_reads["enabled"]:
                raise RuntimeError("hardware mute read failed")
            return await real_read()

        runtime.read_hardware_mute = controlled_read

        async def abort_hook(
            request, snapshot, *, target_staged, ensure_gate_closed=None
        ):
            fail_reads["enabled"] = True
            return True

        runtime.abort_failed_transition = abort_hook
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
        # abort's initial gate guard cannot confirm a closed gate, so no
        # rate/graph restore may start and the failure stays latched.
        runtime = FakeRuntime(muted=False, fail_mute_read_number=1)

        class GuardUsingAbortRuntime(FakeRuntime):
            async def abort_failed_transition(
                self, request, snapshot, *, target_staged, ensure_gate_closed=None
            ):
                try:
                    await ensure_gate_closed(stage="spotify-abort-restore-before-rate")
                except Exception:
                    return False
                self.events.append("restore-stages-ran")
                return True

        runtime = GuardUsingAbortRuntime(muted=False, fail_mute_read_number=1)
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        with self.assertRaises(PlaybackTransitionFailure) as cm:
            await coordinator.execute(spotify_request())

        self.assertNotIn("restore-stages-ran", runtime.events)
        self.assertTrue(coordinator.gate.failure_latched)
        self.assertTrue(coordinator.gate.closed)
        self.assertTrue(runtime.muted)
        self.assertTrue(cm.exception.failure_latched)
        self.assertTrue(coordinator.last_error["failure_latched"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
