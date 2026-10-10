#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""SRC selection restores playback through the production policy transaction."""
import asyncio
from contextlib import ExitStack
from dataclasses import replace
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from playback.player import MPVError
from audio import src_policy
from audio.samplerate import alignment
from playback.queue import PlaybackQueue
from playback.transition import PlaybackTransitionCoordinator, TransitionRequest
from playback_transition_test_support import make_transition_runtime


class Player:
    def __init__(self, paused=False, loaded=True):
        self.running = True
        self.state = {"playing": loaded and not paused, "paused": loaded and paused,
                      "current_file": "/music/current.flac" if loaded else None,
                      "file_loaded": loaded, "position": 123.5, "volume": 100}
        self.playlist = []

    def is_running(self):
        return self.running

    @property
    def _running(self):
        return self.running

    def clear_playlist(self):
        self.playlist = []

    def set_loop_file(self, enabled):
        self.loop_file = enabled

    def set_pause(self, paused):
        self.state.update(paused=bool(paused), playing=bool(self.state["current_file"] and not paused))

    def set_volume(self, volume):
        self.state["volume"] = volume

    def stop_playback(self):
        self.state.update(playing=False, paused=False, current_file=None, file_loaded=False)

    def stop(self):
        self.stop_playback()
        self.running = False

    def start(self):
        self.running = True

    def loadfile(self, url, mode="replace", start_paused=True):
        if mode == "append":
            self.playlist.append(url)
            return
        self.playlist = [url]
        self.state.update(current_file=url, file_loaded=True, position=0.0)
        self.set_pause(start_paused)

    def seek(self, position):
        self.state["position"] = position

    def get_property(self, name):
        return {"path": self.state["current_file"], "pause": self.state["paused"],
                "idle-active": not self.state["file_loaded"], "time-pos": self.state["position"],
                "audio-params": {"samplerate": 44100}}[name]

    def set_loop_playlist(self, enabled):
        self.loop = enabled

    def set_shuffle(self, enabled):
        self.shuffle = enabled

    def set_playlist_pos(self, index):
        self.state["current_file"] = self.playlist[index]


class SrcApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"XDG_CONFIG_HOME": directory.name}))
        self.stack.enter_context(patch.object(src_policy, "RUNTIME", Path(directory.name) / "runtime"))
        self.stack.enter_context(patch.object(src_policy, "runtime_error", return_value=None))
        self.stack.enter_context(patch.object(src_policy, "nodes", side_effect=lambda: [{
            "name": "test-hardware", "mode": src_policy.load_policy()["mode"],
            "state": "running", "error": 0, "bound": True, "generation": None}]))
        self.stack.enter_context(patch.object(main, "measurement_sr_session", SimpleNamespace(
            lock=asyncio.Lock(), has_active_jobs=False, owns_audio_graph=False)))
        self.stack.enter_context(patch.object(main, "_measurement_session_owns_live_rate", return_value=False))
        self.stack.enter_context(patch.object(main.runtime, "stdin_input", None))
        self.stack.enter_context(patch.object(main.samplerate, "_load_audio_source_selection", return_value={"mode": "app-playback"}))
        for provider in ("spotify", "qobuz"):
            self.stack.enter_context(patch.object(main, f"get_{provider}_ui_state", AsyncMock(return_value={"available": False, "status": "Stopped"})))
            self.stack.enter_context(patch.object(main.media_readiness, f"wait_for_pipewire_{provider}_release", AsyncMock(return_value=True)))
        self.stack.enter_context(patch.object(main.media_readiness, "wait_for_pipewire_mpv_release", AsyncMock(return_value=True)))
        self.stack.enter_context(patch.object(main.media_readiness, "wait_for_player_current_file", AsyncMock(return_value=True)))
        self.stack.enter_context(patch.object(main.media_readiness, "get_player_audio_samplerate", return_value=44100))
        self.stack.enter_context(patch.object(main.media_readiness, "wait_for_radio_live_rate_after_load", AsyncMock(return_value=44100)))
        self.stack.enter_context(patch.object(main, "_mark_player_state_authoritative"))
        self.stack.enter_context(patch.object(main.manager, "broadcast", AsyncMock()))
        self.stack.enter_context(patch.object(src_policy, "activate_environment"))
        self.force_rate = 48000
        self.overview = {"selected_output": {"key": "test", "target_name": "test", "supported_rates": [44100, 48000]},
                         "outputs": [{"key": "test"}], "output_mode": {"mode": "stereo", "effective_output_rate": 48000}}
        self.stack.enter_context(patch.object(main, "get_audio_output_overview", side_effect=lambda *a, **k: self.overview))
        self.stack.enter_context(patch.object(main, "get_samplerate_status", side_effect=lambda: {
            "active_rate": 48000, "force_rate": self.force_rate, "policy": {"mode": "fixed", "rate": 48000}}))
        self.stack.enter_context(patch.object(main.samplerate, "get_current_pipewire_force_rate", side_effect=lambda: self.force_rate))
        self.stack.enter_context(patch.object(main.samplerate, "set_pipewire_force_rate", side_effect=lambda value: setattr(self, "force_rate", value)))
        self.stack.enter_context(patch.object(main.samplerate, "prepare_audio_output_selection", return_value=self.overview["selected_output"]))
        self.stack.enter_context(patch.object(main.samplerate, "set_audio_output_selection", side_effect=lambda key: self.overview))
        self.stack.enter_context(patch.object(main.dsp_orchestrator, "sync_runtime", AsyncMock()))
        self.stack.enter_context(patch.object(main.asyncio, "create_subprocess_exec", AsyncMock(side_effect=self.restart_services)))
        main.samplerate.persist_sample_rate_policy({"mode": "fixed", "rate": 48000})

    async def restart_services(self, *args, **kwargs):
        self.assertFalse(self.player.state["playing"])
        self.assertFalse(self.player.running)
        self.force_rate = 0
        return SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"", b"")))

    def configure_playback(self, *, paused=False, loaded=True, source="local", native=False):
        self.player = Player(paused, loaded)
        self.stack.enter_context(patch.object(main.runtime, "player_instance", self.player))
        dsp = SimpleNamespace(stop=AsyncMock(), verify=AsyncMock(return_value=True))
        self.stack.enter_context(patch.object(main.runtime, "dsp_runtime", dsp))
        self.track = {"id": "current", "source": source, "url": "/music/current.flac", "sample_rate_hz": 44100}
        self.stack.enter_context(patch.object(main.playback_state, "current_track_info", self.track if loaded else None))
        self.stack.enter_context(patch.object(main.playback_state, "current_playback_owner", source if loaded else None))
        queue = PlaybackQueue(main.playback_queue.queue._deps)
        queue.tracks = [{"id": "previous", "source": "local", "url": "/music/previous.flac", "sample_rate_hz": 44100}, self.track]
        queue.index = 1
        queue.mode = "native_mpv" if native else "app_replace"
        queue.loop = True
        self.queue = queue
        self.stack.enter_context(patch.object(main.playback_queue, "queue", queue))
        adapter = make_transition_runtime()
        self.adapter = adapter
        self.muted = False
        async def mute(value, *args):
            self.muted = value
        async def verify(request):
            if request.source not in {"spotify", "qobuz"}:
                self.assertEqual(self.player.state["current_file"], request.target_url)
                self.assertEqual(self.player.state["playing"], request.should_play)
            return {"committed": True, "active_rate": 48000, "source_volume": self.player.state["volume"]}
        for name, replacement in {
            "read_hardware_mute": AsyncMock(side_effect=lambda: self.muted),
            "set_hardware_mute": mute,
            "read_sink_mute": AsyncMock(return_value=False), "set_sink_mute": AsyncMock(),
            "establish_effects_and_helper": AsyncMock(return_value={"dsp_reinitialized": True}),
            "reconcile_post_start_graph": AsyncMock(return_value={"graph_complete": True}),
            "verify_transition_graph": verify, "verify_committed_transition": verify,
            "verify_output_mode_runtime": AsyncMock(return_value={"committed": True}),
            "stabilize_effects_after_rate_change": AsyncMock(return_value={"stabilized": True}),
        }.items():
            self.stack.enter_context(patch.object(adapter, name, replacement))
        self.stack.enter_context(patch.object(main.playback_orchestration.configured(), "ensure_mpv_to_dsp_links", AsyncMock(return_value=True)))
        self.coordinator = PlaybackTransitionCoordinator(adapter, gate_settle_seconds=0)
        self.stack.enter_context(patch.object(main, "playback_transition_coordinator", self.coordinator))

    async def post(self, mode):
        request = SimpleNamespace(json=AsyncMock(return_value={"mode": mode}))
        return await main.save_audio_src_policy(request)

    async def test_each_quality_restores_playing_or_paused_media_and_queue(self):
        for source in ("local", "tidal", "radio"):
            for mode in ("low", "standard", "high"):
                for paused in (False, True):
                    with self.subTest(source=source, mode=mode, paused=paused):
                        self.configure_playback(source=source, paused=paused)
                        result = await self.post(mode)
                        self.assertEqual(result["mode"], mode)
                        self.assertTrue(result["available"])
                        self.assertEqual(self.player.state["current_file"], "/music/current.flac")
                        self.assertEqual(self.player.state["playing"], not paused)
                        self.assertEqual(self.player.state["paused"], paused)
                        self.assertEqual(self.player.state["volume"], 100)
                        self.assertEqual(self.player.state["position"], 0.0 if source == "radio" else 123.5)
                        self.assertEqual(main.playback_state.current_track_info, self.track)
                        self.assertEqual(main.playback_state.current_playback_owner, source)
                        self.assertEqual(len(self.queue.tracks), 2)
                        self.assertEqual(self.queue.index, 1)
                        self.assertFalse(self.muted)
                        self.assertEqual(main.samplerate.load_sample_rate_policy(), {"mode": "fixed", "rate": 48000})
                        self.assertEqual(self.force_rate, 48000)

    async def test_native_playlist_is_rebuilt_at_the_same_index(self):
        self.configure_playback(native=True)
        await self.post("high")
        self.assertEqual(self.player.playlist, ["/music/previous.flac", "/music/current.flac"])
        self.assertEqual(self.queue.index, 1)
        self.assertEqual(self.queue.mode, "native_mpv")
        self.assertEqual(self.player.state["position"], 123.5)
        self.assertTrue(self.player.state["playing"])

    async def test_restore_uses_live_position_instead_of_delayed_observer_cache(self):
        self.configure_playback(paused=True)
        self.player.state["position"] = 124.25
        read_property = self.player.get_property
        live = {"position": 123.5}
        seek = self.player.seek
        def seek_live(position):
            live["position"] = position
            seek(position)
        with (patch.object(self.player, "get_property", side_effect=lambda name: live["position"] if name == "time-pos" else read_property(name)),
              patch.object(self.player, "seek", side_effect=seek_live)):
            await self.post("high")
        self.assertEqual(self.player.state["position"], 123.5)
        self.assertTrue(self.player.state["paused"])

    async def test_src_change_recovers_a_dead_player_without_dead_ipc(self):
        self.configure_playback(native=True)
        self.player.running = False
        read_property = self.player.get_property
        def live_property(name):
            if not self.player.running:
                raise MPVError("MPV is not running")
            return read_property(name)
        with patch.object(self.player, "get_property", side_effect=live_property):
            result = await self.post("low")
        self.assertEqual(result["mode"], "low")
        self.assertEqual(src_policy.load_policy()["mode"], "low")
        self.assertTrue(self.player.running)
        self.assertTrue(self.player.state["playing"])
        self.assertEqual(self.player.state["position"], 123.5)
        self.assertEqual(self.queue.index, 1)
        self.assertFalse(self.muted)

    async def test_player_death_during_position_read_uses_cached_position(self):
        self.configure_playback(paused=True)
        read_property = self.player.get_property
        failed = False
        def live_property(name):
            nonlocal failed
            if name == "time-pos" and not failed:
                failed = True
                self.player.running = False
                raise MPVError("IPC connection reset")
            return read_property(name)
        with patch.object(self.player, "get_property", side_effect=live_property):
            await self.post("high")
        self.assertTrue(self.player.running)
        self.assertTrue(self.player.state["paused"])
        self.assertEqual(self.player.state["position"], 123.5)

    async def test_live_player_position_read_failure_rejects_without_mutation(self):
        self.configure_playback()
        with patch.object(self.player, "get_property", side_effect=MPVError("IPC timed out")):
            with self.assertRaises(main.HTTPException) as caught:
                await self.post("high")
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(src_policy.load_policy()["mode"], "standard")
        self.assertTrue(self.player.state["playing"])
        self.assertFalse(self.muted)

    async def test_same_rate_player_death_during_snapshot_reloads_at_cached_position(self):
        self.configure_playback(paused=True)
        read_property = self.player.get_property
        failed = False
        def live_property(name):
            nonlocal failed
            if name == "time-pos" and not failed:
                failed = True
                self.player.running = False
                raise MPVError("IPC connection reset")
            return read_property(name)
        request = SimpleNamespace(json=AsyncMock(return_value={"mode": "fixed", "rate": 48000}))
        with patch.object(self.player, "get_property", side_effect=live_property):
            await main.save_audio_samplerate_policy(request)
        self.assertTrue(self.player.running)
        self.assertTrue(self.player.state["paused"])
        self.assertEqual(self.player.state["current_file"], "/music/current.flac")
        self.assertEqual(self.player.state["position"], 123.5)

    async def test_radio_policy_restarts_player_before_live_rate_resolution(self):
        self.configure_playback(source="radio")
        self.player.running = False
        request = SimpleNamespace(json=AsyncMock(return_value={"mode": "fixed", "rate": 48000}))
        await main.save_audio_samplerate_policy(request)
        self.assertTrue(self.player.running)
        self.assertTrue(self.player.state["playing"])
        self.assertEqual(self.player.state["current_file"], "/music/current.flac")
        self.assertFalse(self.muted)

    async def test_same_rate_player_death_during_output_snapshot_reloads_media(self):
        self.configure_playback(paused=True, native=True)
        def overview_after_exit(*args):
            self.player.running = False
            return self.overview
        self.adapter._deps = replace(self.adapter._deps, get_audio_output_overview=overview_after_exit)
        request = SimpleNamespace(json=AsyncMock(return_value={"mode": "fixed", "rate": 48000}))
        await main.save_audio_samplerate_policy(request)
        self.assertTrue(self.player.running)
        self.assertTrue(self.player.state["paused"])
        self.assertEqual(self.player.playlist, ["/music/previous.flac", "/music/current.flac"])
        self.assertEqual(self.player.state["position"], 123.5)
        self.assertFalse(self.coordinator.gate.failure_latched)
        self.assertFalse(self.muted)

    async def test_radio_death_after_rate_staging_reloads_into_replacement_player(self):
        self.configure_playback(source="radio")
        async def helper_after_exit(request):
            self.player.stop_playback()
            self.player.running = False
            return {"dsp_reinitialized": True}
        with patch.object(self.adapter, "establish_effects_and_helper", side_effect=helper_after_exit):
            await self.post("high")
        self.assertTrue(self.player.running)
        self.assertEqual(self.player.state["current_file"], "/music/current.flac")
        self.assertTrue(self.player.state["playing"])
        self.assertFalse(self.coordinator.gate.failure_latched)
        self.assertFalse(self.muted)

    async def test_dead_radio_carrier_request_keeps_48000_without_loading_retained_media(self):
        self.configure_playback(source="radio")
        self.player.running = False
        main.samplerate.persist_sample_rate_policy({"mode": "auto", "rate": None})
        rate_targets = []
        async def establish(request):
            rate_targets.append(request.target_rate)
        request = TransitionRequest(operation="sample-rate-policy", source="radio", target_rate=48000,
                                    input_rate_hz=48000, rate_change=True, reload_source=False,
                                    should_play=False, sample_rate_policy={"mode": "auto", "rate": None})
        with patch.object(self.adapter, "establish_target_rate", side_effect=establish):
            result = await self.coordinator.execute(request)
        self.assertEqual(rate_targets, [48000])
        self.assertEqual(result.target_rate, 48000)
        self.assertFalse(self.player.running)
        self.assertEqual(main.samplerate.load_sample_rate_policy(), {"mode": "auto", "rate": None})
        self.assertFalse(self.coordinator.gate.failure_latched)
        self.assertFalse(self.muted)

    async def test_same_rate_policy_restarts_and_reloads_a_dead_player(self):
        self.configure_playback(native=True)
        self.player.running = False
        self.player.state.update(current_file="/music/uncommitted.flac", file_loaded=False)
        read_property = self.player.get_property
        def live_property(name):
            if not self.player.running:
                raise MPVError("MPV is not running")
            return read_property(name)
        request = SimpleNamespace(json=AsyncMock(return_value={"mode": "fixed", "rate": 48000}))
        with patch.object(self.player, "get_property", side_effect=live_property):
            await main.save_audio_samplerate_policy(request)
        self.assertTrue(self.player.running)
        self.assertEqual(self.player.state["current_file"], "/music/current.flac")
        self.assertTrue(self.player.state["playing"])
        self.assertEqual(self.queue.index, 1)
        self.assertFalse(self.muted)

    async def test_stopped_playback_stays_stopped(self):
        self.configure_playback(loaded=False)
        await self.post("low")
        self.assertIsNone(self.player.state["current_file"])
        self.assertFalse(self.player.state["playing"])
        self.assertFalse(self.player.state["paused"])
        self.assertIsNone(main.playback_state.current_playback_owner)

    async def test_auto_default_and_idle_handoffs_do_not_leave_a_force_pin(self):
        for loaded, rate in ((True, 48000), (False, 44100)):
            with self.subTest(loaded=loaded, rate=rate):
                self.configure_playback(loaded=loaded)
                main.samplerate.persist_sample_rate_policy({"mode": "auto", "rate": None})
                self.force_rate = 0
                status = lambda: {"active_rate": rate, "force_rate": self.force_rate, "default_rate": 48000}
                async def establish(request):
                    self.force_rate = request.target_rate
                with (patch.object(main, "get_samplerate_status", side_effect=status),
                      patch.object(alignment, "get_samplerate_status", side_effect=status),
                      patch.object(alignment, "set_pipewire_force_rate", side_effect=lambda value: setattr(self, "force_rate", value)),
                      patch.object(main.samplerate, "playback_target_rate", return_value=rate),
                      patch.object(self.adapter, "establish_target_rate", side_effect=establish)):
                    await self.post("high")
                self.assertEqual(self.force_rate, 0)
                self.assertEqual(main.samplerate.load_sample_rate_policy(), {"mode": "auto", "rate": None})

    async def test_queued_src_uses_the_sample_rate_policy_committed_before_admission(self):
        self.configure_playback()
        queued = asyncio.Event()
        run = main._run_coordinated_transition
        async def submit(request):
            queued.set()
            return await run(request)
        await self.coordinator.lock.acquire()
        with patch.object(main, "_run_coordinated_transition", side_effect=submit):
            task = asyncio.create_task(self.post("high"))
            try:
                await asyncio.wait_for(queued.wait(), 1)
                main.samplerate.persist_sample_rate_policy({"mode": "fixed", "rate": 44100})
            finally:
                self.coordinator.lock.release()
            with patch.object(self.adapter, "establish_target_rate", AsyncMock()):
                await task
        self.assertEqual(self.coordinator.last_result.target_rate, 44100)
        self.assertEqual(main.samplerate.load_sample_rate_policy(), {"mode": "fixed", "rate": 44100})

    async def test_tier_failure_also_restores_the_previous_src_mode(self):
        self.configure_playback()
        tiers = [{"id": "wide", "channels": 4, "rates": [44100]},
                 {"id": "narrow", "channels": 2, "rates": [48000]}]
        self.overview["selected_output"]["device_profile"] = {"active_tier": "wide", "tiers": tiers}
        src_rollbacks = []
        real_rollback = self.adapter.rollback_sample_rate_policy
        async def counting_rollback(request, snapshot):
            src_rollbacks.append(1)
            return await real_rollback(request, snapshot)
        async def rollback(*args):
            self.player.loadfile("/music/current.flac", start_paused=False)
            return True
        with (patch.object(self.adapter, "apply_channel_tier", AsyncMock(side_effect=RuntimeError("tier failed"))),
              patch.object(self.adapter, "rollback_channel_tier", side_effect=rollback),
              patch.object(self.adapter, "rollback_sample_rate_policy", side_effect=counting_rollback)):
            with self.assertRaises(main.HTTPException):
                await self.post("high")
        self.assertEqual(src_policy.load_policy()["mode"], "standard")
        self.assertEqual(len(src_rollbacks), 1)

    async def test_busy_dsp_rejection_keeps_stopped_transport_and_gate_unchanged(self):
        self.configure_playback(loaded=False)
        lock = asyncio.Lock()
        await lock.acquire()
        with patch.object(main, "_dsp_mutation_lock", return_value=lock):
            with self.assertRaises(main.HTTPException):
                await self.post("high")
        self.assertFalse(self.player.state["paused"])
        self.assertFalse(self.muted)
        self.assertFalse(self.coordinator.gate.failure_latched)

    async def test_measurement_waits_until_src_helper_and_transport_finish(self):
        self.configure_playback()
        helper_entered, measurement_entered = asyncio.Event(), asyncio.Event()
        finish_helper = asyncio.Event()
        lock = main.measurement_sr_session.lock
        async def helper(request):
            helper_entered.set()
            await finish_helper.wait()
            return {}
        async def measurement():
            async with lock:
                measurement_entered.set()
                async with self.coordinator.lock:
                    pass
        with patch.object(self.adapter, "establish_effects_and_helper", side_effect=helper):
            task = asyncio.create_task(self.post("high"))
            await asyncio.wait_for(helper_entered.wait(), 1)
            measure = asyncio.create_task(measurement())
            try:
                for _ in range(5):
                    await asyncio.sleep(0)
                self.assertFalse(measurement_entered.is_set())
            finally:
                finish_helper.set()
                await asyncio.wait_for(asyncio.gather(task, measure), 2)
        self.assertTrue(measurement_entered.is_set())
        self.assertTrue(self.player.state["playing"])

    async def test_failed_graph_restores_previous_mode_and_playback(self):
        self.configure_playback()
        with patch.object(self.adapter, "establish_effects_and_helper", AsyncMock(side_effect=[RuntimeError("new graph failed"), {}])):
            with self.assertRaises(main.HTTPException) as error:
                await self.post("high")
        self.assertIn("new graph failed", str(error.exception.detail))
        self.assertEqual(src_policy.load_policy()["mode"], "standard")
        self.assertTrue(self.player.state["playing"])
        self.assertEqual(self.player.state["position"], 123.5)
        self.assertFalse(self.coordinator.gate.failure_latched)

    async def test_failed_radio_src_change_reloads_media_after_rollback_restart(self):
        self.configure_playback(source="radio")
        with patch.object(self.adapter, "establish_effects_and_helper", AsyncMock(side_effect=[RuntimeError("new graph failed"), {}])):
            with self.assertRaises(main.HTTPException) as error:
                await self.post("high")
        self.assertIn("new graph failed", str(error.exception.detail))
        self.assertEqual(src_policy.load_policy()["mode"], "standard")
        self.assertTrue(self.player.running)
        self.assertEqual(self.player.state["current_file"], "/music/current.flac")
        self.assertTrue(self.player.state["playing"])
        self.assertFalse(self.coordinator.gate.failure_latched)
        self.assertFalse(self.muted)

    async def test_failed_auto_handoff_restores_the_original_unpinned_graph(self):
        self.configure_playback()
        main.samplerate.persist_sample_rate_policy({"mode": "auto", "rate": None})
        self.force_rate = 0
        async def establish(request):
            self.force_rate = request.target_rate
        with (patch.object(self.adapter, "establish_target_rate", side_effect=establish),
              patch.object(self.adapter, "establish_effects_and_helper", AsyncMock(side_effect=[RuntimeError("new graph failed"), {}]))):
            with self.assertRaises(main.HTTPException):
                await self.post("high")
        self.assertTrue(self.player.state["playing"])
        self.assertFalse(self.coordinator.gate.failure_latched)
        self.assertEqual(self.force_rate, 0)

    async def test_failed_paused_handoff_retains_the_user_hardware_mute(self):
        self.configure_playback(paused=True)
        self.muted = True
        with patch.object(self.adapter, "establish_effects_and_helper", AsyncMock(side_effect=[RuntimeError("new graph failed"), {}])):
            with self.assertRaises(main.HTTPException):
                await self.post("high")
        self.assertTrue(self.player.state["paused"])
        self.assertFalse(self.coordinator.gate.failure_latched)
        self.assertTrue(self.muted)

    async def test_policy_write_failure_restores_stopped_transport_without_a_latch(self):
        self.configure_playback(loaded=False)
        with patch.object(src_policy, "atomic_write_text", side_effect=OSError("Read-only file system")):
            with self.assertRaises(main.HTTPException):
                await self.post("high")
        self.assertIsNone(self.player.state["current_file"])
        self.assertFalse(self.player.state["paused"])
        self.assertFalse(self.muted)
        self.assertFalse(self.coordinator.gate.failure_latched)
        self.assertEqual(src_policy.load_policy()["mode"], "standard")

    async def test_stale_helper_recovery_reuses_policy_configuration_ownership(self):
        from dsp.orchestration import DspOrchestrator
        self.configure_playback()
        live = {"active_rate": 48000, "force_rate": 44100}
        async def sync(overview):
            live["active_rate"] = 44100
        dsp = SimpleNamespace(snapshot=lambda: {"active": True, "config": {"sample_rate": live["active_rate"]}},
                              sync=sync)
        recovery = DspOrchestrator(SimpleNamespace(
            measurement_audio_graph_owned=lambda: False, get_dsp_runtime=lambda: dsp,
            get_samplerate_status=lambda: dict(live), get_audio_output_overview=lambda: self.overview,
            get_measurement_sr_session=lambda: main.measurement_sr_session,
            wait_for_samplerate_alignment=AsyncMock(return_value=True),
            try_render_v2_target=None,
        ))
        self.adapter._deps = replace(self.adapter._deps,
            get_samplerate_status=lambda: dict(live),
            ensure_playback_samplerate_force=AsyncMock(return_value=False),
            trigger_idle_sink_renegotiation=AsyncMock(return_value=False),
            recover_stale_samplerate_helper=recovery.recover_stale_helper_samplerate)
        request = TransitionRequest(operation="sample-rate-policy", source="local",
                                    src_mode="high", target_rate=44100)
        async with self.adapter.transition_configuration(request):
            await asyncio.wait_for(self.adapter.establish_target_rate(request), 0.5)
        self.assertEqual(live["active_rate"], 44100)

    async def test_external_renderers_resume_their_own_track_and_position(self):
        import playback.runtime.source as source_runtime
        for provider in ("spotify", "qobuz"):
            for paused in (False, True):
                with self.subTest(provider=provider, paused=paused):
                    self.configure_playback(loaded=False)
                    transport = {"available": True, "status": "Paused" if paused else "Playing",
                                 "trackId": "42", "position": 123.5, "duration": 300, "sample_rate": 44100}
                    async def pause():
                        transport["status"] = "Paused"
                        return dict(transport)
                    async def play():
                        transport["status"] = "Playing"
                        return dict(transport)
                    self.stack.enter_context(patch.object(main.playback_state, "current_playback_owner", provider))
                    self.stack.enter_context(patch.object(main, f"get_{provider}_ui_state", AsyncMock(side_effect=lambda: dict(transport))))
                    self.stack.enter_context(patch.object(main, f"{provider}_pause", side_effect=pause))
                    self.stack.enter_context(patch.object(main, "qobuz_play", side_effect=play))
                    self.stack.enter_context(patch.object(main, "qobuz_loaded_track_id", AsyncMock(return_value=42)))
                    self.stack.enter_context(patch.object(source_runtime, "spotify_play", side_effect=play))
                    await self.post("high")
                    self.assertEqual(transport["status"], "Paused" if paused else "Playing")
                    self.assertEqual(transport["trackId"], "42")
                    self.assertEqual(transport["position"], 123.5)
                    self.assertEqual(main.playback_state.current_playback_owner, provider)

    async def test_unwritable_policy_does_not_restart_audio(self):
        self.configure_playback()
        with (patch.object(src_policy, "atomic_write_text", side_effect=OSError("Read-only file system")),
              patch.object(main.asyncio, "create_subprocess_exec", AsyncMock()) as restart):
            with self.assertRaises(main.HTTPException) as error:
                await self.post("high")
        self.assertIn("Cannot save SRC policy", str(error.exception.detail))
        restart.assert_not_awaited()
        self.assertEqual(src_policy.load_policy()["mode"], "standard")

    async def test_missing_runtime_rejects_without_pausing_playback(self):
        self.configure_playback()
        with patch.object(src_policy, "runtime_error", return_value="Common SRC runtime is missing"):
            with self.assertRaises(main.HTTPException) as error:
                await self.post("high")
        self.assertEqual(error.exception.status_code, 503)
        self.assertTrue(self.player.state["playing"])
        self.assertFalse(self.muted)

    async def test_busy_measurement_lock_rejects_without_waiting(self):
        self.configure_playback()
        await main.measurement_sr_session.lock.acquire()
        with self.assertRaises(main.HTTPException) as error:
            await asyncio.wait_for(self.post("high"), 1)
        self.assertEqual(error.exception.status_code, 423)
        self.assertTrue(self.player.state["playing"])
        self.assertEqual(src_policy.load_policy()["mode"], "standard")

    async def test_busy_dsp_lock_cannot_deadlock_the_policy_transaction(self):
        self.configure_playback()
        lock = asyncio.Lock()
        await lock.acquire()
        with patch.object(main, "_dsp_mutation_lock", return_value=lock):
            with self.assertRaises(main.HTTPException) as error:
                await asyncio.wait_for(self.post("high"), 1)
        self.assertIn("configuration is busy", str(error.exception.detail))
        self.assertEqual(src_policy.load_policy()["mode"], "standard")

    async def test_adapter_rebuild_waits_for_original_hardware_or_unique_successor(self):
        self.configure_playback()
        original = "alsa_output.usb-Original.stereo"
        unrelated = "alsa_output.pci-Onboard.stereo"
        for returned in (original, "alsa_output.usb-Original.pro-output-0"):
            with self.subTest(returned=returned):
                probes = 0
                selected = []
                def overview(*args, **kwargs):
                    nonlocal probes
                    probes += 1
                    outputs = [{"key": unrelated}]
                    if probes >= 3:
                        outputs.append({"key": returned})
                    return {"outputs": outputs, "selected_output": {"key": unrelated, "target_name": unrelated}}
                def select(key):
                    selected.append(key)
                    return {"selected_output": {"key": key, "target_name": key}}
                with (patch.object(main, "get_audio_output_overview", side_effect=overview),
                      patch.object(main.samplerate, "set_audio_output_selection", side_effect=select),
                      patch.object(main.asyncio, "sleep", AsyncMock())):
                    await main._restart_src_graph({"force_rate": 48000, "output_key": original})
                self.assertEqual(selected, [returned])
                self.assertEqual(self.force_rate, 48000)

    async def test_adapter_rebuild_rejects_an_unrelated_output(self):
        self.configure_playback()
        with (patch.object(main, "get_audio_output_overview", return_value={"outputs": [{"key": "unrelated"}]}),
              patch.object(main.asyncio, "sleep", AsyncMock()),
              patch.object(main.dsp_orchestrator, "sync_runtime", AsyncMock()) as sync):
            with self.assertRaisesRegex(RuntimeError, "selected audio output did not return"):
                await main._restart_src_graph({"force_rate": 48000, "output_key": "original"})
        sync.assert_not_awaited()

    async def test_measurement_and_input_ownership_reject_before_mutation(self):
        self.configure_playback()
        for selection, measurement, code in (("external-input", False, 409), ("stdin-input", False, 409), ("app-playback", True, 423)):
            with (patch.object(main.samplerate, "_load_audio_source_selection", return_value={"mode": selection}),
                  patch.object(main, "_measurement_session_owns_live_rate", return_value=measurement)):
                with self.assertRaises(main.HTTPException) as error:
                    await self.post("high")
                self.assertEqual(error.exception.status_code, code)
        self.assertEqual(src_policy.load_policy()["mode"], "standard")
        self.assertTrue(self.player.state["playing"])

    async def test_cancellation_drains_handoff_before_releasing_ownership(self):
        self.configure_playback()
        entered, finish = asyncio.Event(), asyncio.Event()
        async def restart(*args, **kwargs):
            entered.set()
            await finish.wait()
            return await self.restart_services(*args, **kwargs)
        with patch.object(main.asyncio, "create_subprocess_exec", side_effect=restart):
            task = asyncio.create_task(self.post("high"))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            await asyncio.sleep(0)
            self.assertTrue(self.coordinator.lock.locked())
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(self.coordinator.lock.locked())
        self.assertTrue(self.player.state["playing"])
        self.assertEqual(self.player.state["position"], 123.5)


if __name__ == "__main__":
    unittest.main()
