#!/usr/bin/env python3
"""Failed same-tier handoffs resume the external renderer they paused."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playback.queue import PlaybackQueue
from playback.runtime import FxrouteTransitionRuntime
from playback.transition import (
    PlaybackTransitionCoordinator,
    PlaybackTransitionFailure,
    TransitionRequest,
)


class PlayerDouble:
    def __init__(self):
        self.state = {
            "current_file": None, "playing": False, "paused": True,
            "ended": False, "position": 0.0, "volume": 100,
        }
        self.quiet_fails = False

    def set_volume(self, volume):
        self.state["volume"] = volume

    def set_pause(self, paused):
        if paused and self.quiet_fails and self.state["playing"]:
            raise RuntimeError("MPV pause failed")
        self.state.update(paused=paused, playing=not paused)

    def stop_playback(self):
        if self.quiet_fails:
            raise RuntimeError("MPV stop failed")
        self.state.update(current_file=None, playing=False, paused=True)

    def set_loop_playlist(self, _enabled):
        pass


class HandoffFixture:
    """Double only the physical transports, graph, and gate boundaries."""

    def __init__(self, source, *, playing=True, player_available=True,
                 fail_stage="graph", target="tidal", restore_fails=False,
                 target_release_fails=False, mpv_quiet_fails=False,
                 restore_graph_fails=False, target_rate=None,
                 mpv_release_fails=False):
        self.source = source
        self.target = target
        self.fail_stage = fail_stage
        self.restore_fails = restore_fails
        self.target_release_fails = target_release_fails
        self.mpv_quiet_fails = mpv_quiet_fails
        self.restore_graph_fails = restore_graph_fails
        self.mpv_release_fails = mpv_release_fails
        self.player = PlayerDouble() if player_available else None
        self.track = None
        self.owner = source
        self.rate = 96_000 if source == "qobuz" else 44_100
        self.target_rate = target_rate
        self.rate_changes = []
        self.muted = False
        self.dsp_muted = False
        self.resume_requests = []
        self.stream_rates = []
        self.states = {
            "spotify": {"available": True, "status": "Paused", "position": 83.25},
            "qobuz": {"available": True, "status": "Paused", "position": 83.25,
                      "sample_rate": self.rate, "trackId": 123},
        }
        self.states[source]["status"] = "Playing" if playing else "Paused"
        self.queue = PlaybackQueue(SimpleNamespace(player=lambda: self.player))
        self.runtime = FxrouteTransitionRuntime(SimpleNamespace(
            player=lambda: self.player, dsp_manager=lambda: None, dsp_runtime=lambda: None,
            get_current_track_info=lambda: self.track,
            set_current_track_info=self.set_track, set_playback_owner=self.set_owner,
            get_playback_intent_generation=lambda: 0, get_transition_epoch=lambda: 0,
            get_samplerate_status=lambda: {"active_rate": self.rate, "force_rate": self.rate},
            get_audio_output_overview=lambda *_args: {},
            queue=lambda: self.queue, player_is_running=lambda: self.player is not None,
            drain_worker=self.drain_worker, mark_player_state_authoritative=lambda _state: None,
            get_spotify_ui_state=lambda: self.state("spotify"),
            get_qobuz_ui_state=lambda: self.state("qobuz"),
            is_spotify_playback_active=lambda state: state.get("status") == "Playing",
            is_qobuz_playback_active=lambda state: state.get("status") == "Playing",
            pause_spotify_for_local_playback_broadcast=lambda: self.pause("spotify"),
            spotify_pause=lambda: self.pause("spotify"),
            qobuz_pause=lambda: self.pause("qobuz"), qobuz_play=lambda: self.resume("qobuz"),
            qobuz_loaded_track_id=self.qobuz_loaded_track_id,
            wait_for_pipewire_spotify_release=lambda: self.release("spotify"),
            wait_for_pipewire_qobuz_release=lambda: self.release("qobuz"),
            wait_for_pipewire_mpv_release=self.mpv_release,
            load_player_paused=self.load, wait_for_player_current_file=self.true,
            ensure_mpv_to_dsp_links=self.true,
            get_player_audio_samplerate=lambda: self.rate,
            wait_for_radio_live_rate_after_load=lambda *_args: self.stream_rate(expected_rate=self.rate),
            coordinator_source_rate=lambda source, track: (
                track.get("sample_rate_hz", 44_100) if source == "qobuz" else 44_100
            ),
            coordinator_target_rate=lambda *_args: self.rate,
            ensure_playback_samplerate_force=self.force_rate,
            coordinator_establish_effects_and_helper=self.effects,
            coordinator_reconcile_post_start_graph=self.reconcile,
            playback_graph_links_complete=self.links_complete,
            wait_for_spotify_sink_input_samplerate=self.stream_rate,
            wait_for_qobuz_sink_input_samplerate=self.stream_rate,
        ))
        # The Coordinator still owns all real gate decisions and readbacks.
        self.runtime.read_hardware_mute = self.read_mute
        self.runtime.set_hardware_mute = self.set_mute
        self.runtime.read_sink_mute = self.read_dsp_mute
        self.runtime.set_sink_mute = self.set_dsp_mute
        self.coordinator = PlaybackTransitionCoordinator(self.runtime, gate_settle_seconds=0)

    def set_track(self, track):
        self.track = track

    def set_owner(self, owner):
        self.owner = owner

    async def drain_worker(self, callback, *args, **kwargs):
        return callback(*args, **kwargs)

    async def state(self, source):
        return dict(self.states[source])

    async def qobuz_loaded_track_id(self):
        return int(self.states["qobuz"]["trackId"])

    async def pause(self, source):
        self.states[source]["status"] = "Paused"

    async def resume(self, source):
        if self.restore_fails and source == self.source:
            raise RuntimeError("external resume failed")
        self.states[source]["status"] = "Playing"
        return dict(self.states[source])

    async def release(self, source):
        if self.target_release_fails and source == self.target:
            return False
        if self.fail_stage == "release" and source == self.source:
            return False
        return self.states[source]["status"] != "Playing"

    async def mpv_release(self):
        if self.mpv_release_fails:
            return False
        return self.player is None or not self.player.state["playing"]

    async def force_rate(self, target_rate, *_args, **_kwargs):
        self.rate_changes.append(target_rate)
        self.rate = target_rate
        return True

    async def true(self, *_args, **_kwargs):
        return True

    async def effects(self, request):
        if request.source == self.target and self.fail_stage == "effects":
            raise RuntimeError("target effects failed")
        return {"dsp_reinitialized": False, "helper_rebuilt": False}

    async def reconcile(self, request):
        if request.source == self.source:
            self.resume_requests.append(request)
        return {"graph_complete": True}

    async def links_complete(self, **kwargs):
        if self.player is not None and self.mpv_quiet_fails and kwargs.get("source") == self.target:
            self.player.quiet_fails = True
        if self.restore_graph_fails and kwargs.get("source") == self.source:
            return False
        return kwargs.get("source") != self.target

    async def stream_rate(self, *, expected_rate):
        self.stream_rates.append(expected_rate)
        # Qobuz discovery reads a renderer rate without a catalog expectation.
        return expected_rate if expected_rate is not None else self.states["qobuz"]["sample_rate"]

    def load(self, url):
        self.player.state.update(current_file=url, paused=True, playing=False)

    async def read_mute(self):
        return self.muted

    async def set_mute(self, muted, _transition_id):
        self.muted = muted

    async def read_dsp_mute(self, _sink):
        return self.dsp_muted

    async def set_dsp_mute(self, _sink, muted, _transition_id):
        self.dsp_muted = muted

    async def fail_handoff(self):
        request = TransitionRequest(
            operation="play", source=self.target, target_rate=self.target_rate or self.rate,
            target_url=("/cache/failed.mp4" if self.target == "tidal" else
                        "https://radio.example/failed" if self.target == "radio" else None),
            target_track={"source": self.target, "id": "failed"},
            should_play=True, reload_source=True,
            rate_change=self.target_rate is not None and self.target_rate != self.rate,
        )
        with patch("playback.runtime.source.spotify_play", lambda: self.resume("spotify")):
            try:
                await self.coordinator.execute(request)
            except PlaybackTransitionFailure as exc:
                return exc
        raise AssertionError("The target handoff must fail")


class ExternalSourceFailureRestoreTests(unittest.IsolatedAsyncioTestCase):
    def assert_resumed(self, fixture, error):
        self.assertEqual(fixture.states[fixture.source]["status"], "Playing")
        self.assertEqual(fixture.states[fixture.source]["position"], 83.25)
        self.assertEqual(fixture.owner, fixture.source)
        self.assertIsNone(fixture.track)
        if fixture.player is not None:
            self.assertIsNone(fixture.player.state["current_file"])
            self.assertFalse(fixture.player.state["playing"])
        self.assertFalse(error.failure_latched)
        self.assertFalse(fixture.coordinator.gate.closed)
        self.assertFalse(fixture.coordinator.transition_blocked)
        self.assertFalse(fixture.muted)
        self.assertFalse(fixture.dsp_muted)
        self.assertEqual(len(fixture.resume_requests), 1)
        restored = fixture.resume_requests[0]
        self.assertEqual(restored.source, fixture.source)
        self.assertEqual(restored.target_rate, fixture.rate)
        self.assertTrue(restored.should_play)
        self.assertFalse(restored.reload_source)
        self.assertIsNone(restored.target_url)
        self.assertIsNone(restored.restore_position)
        self.assertFalse(restored.native_queue)
        self.assertFalse(restored.channel_tier)

    async def test_staged_target_failure_resumes_spotify_and_qobuz(self):
        for source in ("spotify", "qobuz"):
            with self.subTest(source=source):
                fixture = HandoffFixture(source)
                error = await fixture.fail_handoff()
                self.assertEqual(error.stage, "staged-graph-readback")
                self.assert_resumed(fixture, error)

    async def test_failure_before_staging_resumes_spotify_and_qobuz(self):
        for source in ("spotify", "qobuz"):
            with self.subTest(source=source):
                fixture = HandoffFixture(source, fail_stage="effects")
                error = await fixture.fail_handoff()
                self.assertEqual(error.stage, "effects-helper-links")
                self.assert_resumed(fixture, error)

    async def test_staged_radio_failure_resumes_spotify_and_qobuz(self):
        for source in ("spotify", "qobuz"):
            with self.subTest(source=source):
                fixture = HandoffFixture(source, target="radio")
                error = await fixture.fail_handoff()
                self.assertEqual(error.stage, "staged-graph-readback")
                self.assert_resumed(fixture, error)

    async def test_quieted_external_renderer_wins_over_retained_mpv_context(self):
        for source in ("spotify", "qobuz"):
            with self.subTest(source=source):
                fixture = HandoffFixture(source)
                fixture.player.state.update(current_file="/music/retained.flac", position=40.0)
                fixture.track = {"source": "local", "url": "/music/retained.flac"}
                error = await fixture.fail_handoff()
                self.assert_resumed(fixture, error)

    async def test_qobuz_native_rate_is_not_replaced_by_the_fixed_graph_rate(self):
        fixture = HandoffFixture("qobuz")
        fixture.rate = 44_100
        error = await fixture.fail_handoff()
        self.assert_resumed(fixture, error)
        self.assertEqual(fixture.stream_rates, [None, 96_000])

    async def test_same_tier_rate_change_restores_the_committed_external_graph_rate(self):
        for source, old_rate in (("spotify", 44_100), ("qobuz", 96_000)):
            with self.subTest(source=source):
                fixture = HandoffFixture(source, target_rate=48_000)
                error = await fixture.fail_handoff()
                self.assert_resumed(fixture, error)
                self.assertEqual(fixture.rate, old_rate)
                self.assertEqual(fixture.rate_changes, [48_000, old_rate])

    async def test_release_failure_still_resumes_the_renderer_already_paused(self):
        for source in ("spotify", "qobuz"):
            with self.subTest(source=source):
                fixture = HandoffFixture(source, fail_stage="release")
                error = await fixture.fail_handoff()
                self.assertEqual(error.stage, "quiet-old-source")
                self.assert_resumed(fixture, error)

    async def test_cross_external_failure_resumes_the_previous_renderer(self):
        for source, target in (("spotify", "qobuz"), ("qobuz", "spotify")):
            with self.subTest(source=source):
                fixture = HandoffFixture(source, target=target, fail_stage="effects")
                error = await fixture.fail_handoff()
                self.assert_resumed(fixture, error)
                self.assertEqual(fixture.states[target]["status"], "Paused")

    async def test_post_start_cross_external_failure_resumes_only_the_previous_renderer(self):
        for source, target in (("spotify", "qobuz"), ("qobuz", "spotify")):
            with self.subTest(source=source):
                fixture = HandoffFixture(source, target=target)
                error = await fixture.fail_handoff()
                self.assertEqual(error.stage, "staged-graph-readback")
                self.assert_resumed(fixture, error)
                self.assertEqual(fixture.states[target]["status"], "Paused")

    async def test_unreleased_external_target_keeps_gate_closed_without_resuming_old_source(self):
        for source, target in (("spotify", "qobuz"), ("qobuz", "spotify")):
            with self.subTest(source=source):
                fixture = HandoffFixture(source, target=target, target_release_fails=True)
                error = await fixture.fail_handoff()
                self.assertEqual(error.stage, "staged-graph-readback")
                self.assertEqual(fixture.states[source]["status"], "Paused")
                self.assertTrue(error.failure_latched)
                self.assertTrue(fixture.muted)
                self.assertEqual(fixture.resume_requests, [])

    async def test_failed_mpv_quiet_keeps_gate_closed_without_resuming_old_source(self):
        for source in ("spotify", "qobuz"):
            with self.subTest(source=source):
                fixture = HandoffFixture(source, mpv_quiet_fails=True)
                error = await fixture.fail_handoff()
                self.assertEqual(error.stage, "staged-graph-readback")
                self.assertEqual(fixture.states[source]["status"], "Paused")
                self.assertIsNone(fixture.owner)
                self.assertTrue(error.failure_latched)
                self.assertTrue(fixture.muted)
                self.assertEqual(fixture.resume_requests, [])

    async def test_unconfirmed_mpv_release_keeps_gate_closed_without_resuming_old_source(self):
        for source in ("spotify", "qobuz"):
            with self.subTest(source=source):
                fixture = HandoffFixture(source, mpv_release_fails=True)
                error = await fixture.fail_handoff()
                self.assertEqual(fixture.states[source]["status"], "Paused")
                self.assertIsNone(fixture.owner)
                self.assertTrue(error.failure_latched)
                self.assertTrue(fixture.muted)
                self.assertEqual(fixture.resume_requests, [])

    async def test_restore_graph_failure_does_not_publish_owner_or_open_gate(self):
        for source in ("spotify", "qobuz"):
            with self.subTest(source=source):
                fixture = HandoffFixture(source, restore_graph_fails=True)
                error = await fixture.fail_handoff()
                self.assertIsNone(fixture.owner)
                self.assertTrue(error.failure_latched)
                self.assertTrue(fixture.muted)

    async def test_missing_mpv_does_not_block_external_restore(self):
        for source in ("spotify", "qobuz"):
            with self.subTest(source=source):
                fixture = HandoffFixture(source, player_available=False, fail_stage="effects")
                error = await fixture.fail_handoff()
                self.assert_resumed(fixture, error)

    async def test_previously_paused_external_source_is_not_started(self):
        for source in ("spotify", "qobuz"):
            with self.subTest(source=source):
                fixture = HandoffFixture(source, playing=False)
                error = await fixture.fail_handoff()
                self.assertEqual(fixture.states[source]["status"], "Paused")
                self.assertTrue(error.failure_latched)
                self.assertTrue(fixture.muted)
                self.assertFalse(fixture.dsp_muted)
                self.assertEqual(fixture.resume_requests, [])

    async def test_failed_external_resume_keeps_gate_latched_and_owner_unpublished(self):
        for source in ("spotify", "qobuz"):
            with self.subTest(source=source):
                fixture = HandoffFixture(source, restore_fails=True)
                error = await fixture.fail_handoff()
                self.assertEqual(fixture.states[source]["status"], "Paused")
                self.assertIsNone(fixture.owner)
                self.assertTrue(error.failure_latched)
                self.assertTrue(fixture.coordinator.gate.closed)
                self.assertTrue(fixture.muted)
                self.assertFalse(fixture.dsp_muted)


if __name__ == "__main__":
    unittest.main(verbosity=2)
