#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Source-mode / playback-transition races: generation-scoped commits.

Covers the P1 race set between external/bluetooth source switches and the
PlaybackTransitionCoordinator:

- an in-flight or queued play/queue/provider transition is discarded once a
  source switch committed (coordinator skips before target start and before
  the commit readback; the app publish validates the captured intent);
- the external/bluetooth pause never stops a newer committed context, while
  still stopping pre-switch leaks (including targets staged by a discarded
  transition, which never app-commit);
- provider claims noted before the switch do not reactivate afterwards;
- cancellation during the source pause still leaves routing, generations,
  transports and published owner/queue consistent;
- switching back to app playback accepts new intents again.
"""

from __future__ import annotations

import asyncio
import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import audio.bluetooth as bluetooth_module
from audio.bluetooth import BluetoothInputDependencies, BluetoothInputMonitor
from playback.transition import PlaybackTransitionCoordinator, TransitionRequest
from playback_transition_test_support import MainCoreTransitionRuntime


def _snapshot_playback_state() -> dict:
    return copy.deepcopy(main.playback_state.__dict__)


def _restore_playback_state(snapshot: dict) -> None:
    main.playback_state.__dict__.clear()
    main.playback_state.__dict__.update(copy.deepcopy(snapshot))


class SourceRaceCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._state = _snapshot_playback_state()
        self._source_lock = main.runtime.source_transition_lock
        self._coordinator = main.playback_transition_coordinator
        main.runtime.source_transition_lock = asyncio.Lock()
        main.playback_state.source_mode = "app-playback"

    def tearDown(self) -> None:
        _restore_playback_state(self._state)
        main.runtime.source_transition_lock = self._source_lock
        main.playback_transition_coordinator = self._coordinator

    def _aligned_samplerate(self):
        return patch.object(
            main, "get_samplerate_status",
            return_value={"active_rate": 44100, "force_rate": 0},
        )

    def _make_coordinator(self, runtime, **kwargs):
        return PlaybackTransitionCoordinator(
            runtime,
            gate_settle_seconds=0,
            get_source_generation=lambda: main.playback_state.source_generation,
            **kwargs,
        )

    def _play_request(self, **overrides):
        payload = {
            "operation": "play",
            "source": "local",
            "target_rate": 44100,
            "target_url": "/music/target.flac",
            "target_track": {"source": "local", "url": "/music/target.flac"},
            "should_play": True,
            "rate_change": False,
            "reload_source": True,
            "detail": "source-race-test",
            "source_generation": main.playback_state.source_generation,
        }
        payload.update(overrides)
        return TransitionRequest(**payload)


class CoordinatorSourceDiscardTests(SourceRaceCase):
    async def test_switch_during_quiet_starts_no_target(self):
        entered_quiet = asyncio.Event()
        release_quiet = asyncio.Event()
        with self._aligned_samplerate():
            runtime = MainCoreTransitionRuntime(
                target_rate=44100,
                generation=main.playback_state.playback_transition_epoch,
                use_core=False,
            )
            real_quiet = runtime.quiet_old_source

            async def blocking_quiet(request):
                entered_quiet.set()
                await release_quiet.wait()
                await real_quiet(request)

            runtime.quiet_old_source = blocking_quiet
            coordinator = self._make_coordinator(runtime)
            task = asyncio.create_task(coordinator.execute(self._play_request()))
            try:
                await asyncio.wait_for(entered_quiet.wait(), timeout=2)
                main.playback_state.note_source_selection("external-input")
                release_quiet.set()
                result = await asyncio.wait_for(task, timeout=5)
            finally:
                if not task.done():
                    task.cancel()
            self.assertFalse(result.committed)
            self.assertTrue((result.state or {}).get("skipped"))
            self.assertEqual((result.state or {}).get("reason"), "source-changed")
            self.assertNotIn("start", runtime.events)
            self.assertFalse(coordinator.gate.closed)
            self.assertFalse(coordinator.gate.failure_latched)
            self.assertIsNone(coordinator.last_error)

    async def test_switch_after_target_start_discards_before_commit(self):
        entered_prepare = asyncio.Event()
        release_prepare = asyncio.Event()
        with self._aligned_samplerate():
            runtime = MainCoreTransitionRuntime(
                target_rate=44100,
                generation=main.playback_state.playback_transition_epoch,
                use_core=False,
            )
            real_prepare = runtime.prepare_target_source

            async def blocking_prepare(request):
                entered_prepare.set()
                await release_prepare.wait()
                await real_prepare(request)

            runtime.prepare_target_source = blocking_prepare
            coordinator = self._make_coordinator(runtime)
            task = asyncio.create_task(coordinator.execute(self._play_request()))
            try:
                await asyncio.wait_for(entered_prepare.wait(), timeout=2)
                main.playback_state.note_source_selection("bluetooth-input")
                release_prepare.set()
                result = await asyncio.wait_for(task, timeout=5)
            finally:
                if not task.done():
                    task.cancel()
            self.assertFalse(result.committed)
            self.assertEqual((result.state or {}).get("reason"), "source-changed")
            self.assertFalse(coordinator.gate.closed)
            self.assertFalse(coordinator.gate.failure_latched)
            # The staged target is quieted again instead of left audible.
            self.assertIn("pause-after-failure", runtime.events)

    async def test_current_generation_commits_normally(self):
        with self._aligned_samplerate():
            runtime = MainCoreTransitionRuntime(
                target_rate=44100,
                generation=main.playback_state.playback_transition_epoch,
                use_core=False,
            )
            coordinator = self._make_coordinator(runtime)
            result = await coordinator.execute(self._play_request())
            self.assertTrue(result.committed)
            self.assertIn("start", runtime.events)

    async def test_unstamped_request_is_exempt(self):
        main.playback_state.note_source_selection("external-input")
        with self._aligned_samplerate():
            runtime = MainCoreTransitionRuntime(
                target_rate=44100,
                generation=main.playback_state.playback_transition_epoch,
                use_core=False,
            )
            coordinator = self._make_coordinator(runtime)
            request = self._play_request()
            unstamped = TransitionRequest(
                operation=request.operation,
                source=request.source,
                target_rate=request.target_rate,
                target_url=request.target_url,
                target_track=dict(request.target_track),
                should_play=request.should_play,
                rate_change=request.rate_change,
                reload_source=request.reload_source,
                detail=request.detail,
            )
            result = await coordinator.execute(unstamped)
            self.assertTrue(result.committed)


class AppPublishGuardTests(SourceRaceCase):
    async def test_stale_play_intent_rejected_before_queue_commit(self):
        captured = main.playback_state.capture_source_intent()
        main.playback_state.note_source_selection("external-input")
        with self.assertRaises(main.HTTPException) as raised:
            main._ensure_source_intent_current(captured)
        self.assertEqual(raised.exception.status_code, 409)

    async def test_current_intent_passes_after_switch_back(self):
        main.playback_state.note_source_selection("external-input")
        main.playback_state.note_source_selection("app-playback")
        captured = main.playback_state.capture_source_intent()
        try:
            main._ensure_source_intent_current(captured)
        except main.HTTPException:
            self.fail("fresh intent after switch-back must validate")

    async def test_note_source_selection_bumps_generation_and_intent(self):
        before_gen = main.playback_state.source_generation
        before_intent = main.playback_state.playback_intent_generation
        main.playback_state.note_source_selection("app-playback")
        self.assertEqual(main.playback_state.source_generation, before_gen + 1)
        self.assertEqual(main.playback_state.playback_intent_generation, before_intent + 1)


class ScopedPauseTests(SourceRaceCase):
    def _player(self, **state):
        player = Mock()
        player._running = True
        player.state = {
            "current_file": "/music/old.flac",
            "playing": True,
            "paused": False,
            "ended": False,
        }
        player.state.update(state)
        player.stop_playback = Mock()
        return player

    async def test_pause_stops_pre_switch_playback(self):
        player = self._player()
        with patch.object(main.runtime, "player_instance", player), patch.object(
            main, "get_spotify_ui_state", AsyncMock(return_value={"status": "Paused", "available": True})
        ), patch.object(main, "get_qobuz_ui_state", AsyncMock(return_value={"status": "Stopped", "available": True})), patch.object(
            main, "_coordinator_lock_for_source_pause", return_value=None
        ), patch.object(main.media_readiness, "wait_for_pipewire_mpv_release", AsyncMock(return_value=True)):
            await main._pause_all_app_playback_for_external_input()
        player.stop_playback.assert_called_once_with()

    async def test_pause_skips_newer_local_commit(self):
        player = self._player()
        lock = asyncio.Lock()
        await lock.acquire()
        with patch.object(main.runtime, "player_instance", player), patch.object(
            main, "get_spotify_ui_state", AsyncMock(return_value={"status": "Paused", "available": True})
        ), patch.object(main, "get_qobuz_ui_state", AsyncMock(return_value={"status": "Stopped", "available": True})), patch.object(
            main, "_coordinator_lock_for_source_pause", return_value=lock
        ), patch.object(main.media_readiness, "wait_for_pipewire_mpv_release", AsyncMock(return_value=True)):
            pause = asyncio.create_task(main._pause_all_app_playback_for_external_input())
            try:
                await asyncio.sleep(0)
                self.assertFalse(pause.done())
                # A newer local commit lands while the pause waits for the lock.
                main.playback_state.current_playback_owner = "local"
                main.playback_state.publish_playback_context_commit("tr-newer")
                lock.release()
                await asyncio.wait_for(pause, timeout=2)
            finally:
                if not pause.done():
                    pause.cancel()
                if lock.locked():
                    lock.release()
        player.stop_playback.assert_not_called()

    async def test_pause_skips_newer_spotify_but_stops_old_spotify_leak(self):
        player = self._player()
        lock = asyncio.Lock()
        await lock.acquire()
        spotify_pause = AsyncMock(return_value={"status": "Paused", "available": True})
        with patch.object(main.runtime, "player_instance", player), patch.object(
            main, "get_spotify_ui_state", AsyncMock(return_value={"status": "Playing", "trackId": "new", "available": True})
        ), patch.object(main, "spotify_pause", spotify_pause), patch.object(
            main, "broadcast_spotify_state", AsyncMock()
        ), patch.object(main, "get_qobuz_ui_state", AsyncMock(return_value={"status": "Stopped", "available": True})), patch.object(
            main, "_coordinator_lock_for_source_pause", return_value=lock
        ), patch.object(main.media_readiness, "wait_for_pipewire_mpv_release", AsyncMock(return_value=True)):
            pause = asyncio.create_task(main._pause_all_app_playback_for_external_input())
            try:
                await asyncio.sleep(0)
                self.assertFalse(pause.done())
                main.playback_state.current_playback_owner = "spotify"
                main.playback_state.publish_playback_context_commit("tr-spotify-new")
                lock.release()
                await asyncio.wait_for(pause, timeout=2)
            finally:
                if not pause.done():
                    pause.cancel()
                if lock.locked():
                    lock.release()
        # Old MPV file is stopped (no newer MPV context), new Spotify survives.
        player.stop_playback.assert_called_once_with()
        spotify_pause.assert_not_called()

    async def test_pause_stops_old_spotify_leak_after_newer_local_commit(self):
        player = self._player(current_file=None, playing=False)
        lock = asyncio.Lock()
        await lock.acquire()
        spotify_pause = AsyncMock(return_value={"status": "Paused", "available": True})
        with patch.object(main.runtime, "player_instance", player), patch.object(
            main, "get_spotify_ui_state", AsyncMock(return_value={"status": "Playing", "trackId": "old", "available": True})
        ), patch.object(main, "spotify_pause", spotify_pause), patch.object(
            main, "broadcast_spotify_state", AsyncMock()
        ), patch.object(main, "get_qobuz_ui_state", AsyncMock(return_value={"status": "Stopped", "available": True})), patch.object(
            main, "_coordinator_lock_for_source_pause", return_value=lock
        ), patch.object(main.media_readiness, "wait_for_pipewire_mpv_release", AsyncMock(return_value=True)):
            pause = asyncio.create_task(main._pause_all_app_playback_for_external_input())
            try:
                await asyncio.sleep(0)
                self.assertFalse(pause.done())
                # Newer commit is local: the still-playing old Spotify is a leak.
                main.playback_state.current_playback_owner = "local"
                main.playback_state.publish_playback_context_commit("tr-local-new")
                lock.release()
                await asyncio.wait_for(pause, timeout=2)
            finally:
                if not pause.done():
                    pause.cancel()
                if lock.locked():
                    lock.release()
        spotify_pause.assert_called_once_with()

    async def test_pause_aborts_when_source_switches_again(self):
        player = self._player()
        entered_release = asyncio.Event()
        release_release = asyncio.Event()

        async def blocking_release():
            entered_release.set()
            await release_release.wait()
            return True

        spotify_pause = AsyncMock()
        with patch.object(main.runtime, "player_instance", player), patch.object(
            main, "get_spotify_ui_state", AsyncMock(return_value={"status": "Playing", "available": True})
        ), patch.object(main, "spotify_pause", spotify_pause), patch.object(
            main, "get_qobuz_ui_state", AsyncMock(return_value={"status": "Stopped", "available": True})
        ), patch.object(main, "_coordinator_lock_for_source_pause", return_value=None
        ), patch.object(main.media_readiness, "wait_for_pipewire_mpv_release", blocking_release):
            pause = asyncio.create_task(main._pause_all_app_playback_for_external_input())
            try:
                await asyncio.wait_for(entered_release.wait(), timeout=2)
                main.playback_state.note_source_selection("bluetooth-input")
                release_release.set()
                await asyncio.wait_for(pause, timeout=2)
            finally:
                if not pause.done():
                    pause.cancel()
        player.stop_playback.assert_called_once_with()
        spotify_pause.assert_not_called()


class ClaimInvalidationTests(SourceRaceCase):
    async def test_noted_spotify_claim_does_not_reactivate_after_switch(self):
        entered_transition = asyncio.Event()
        release_transition = asyncio.Event()

        async def blocking_transition(request):
            entered_transition.set()
            await release_transition.wait()
            return SimpleNamespace(
                committed=True,
                transition_id="tr-claim",
                target_rate=44100,
                state={"committed": True},
            )

        with patch.object(main, "get_spotify_ui_state", AsyncMock(
            return_value={"status": "Playing", "trackId": "abc", "title": "Late", "available": True}
        )), patch.object(main, "_run_coordinated_transition", blocking_transition):
            claim = asyncio.create_task(main._claim_spotify_playback())
            try:
                await asyncio.wait_for(entered_transition.wait(), timeout=2)
                main.playback_state.note_source_selection("external-input")
                release_transition.set()
                state = await asyncio.wait_for(claim, timeout=5)
            finally:
                if not claim.done():
                    claim.cancel()
            self.assertEqual(main.playback_state.current_playback_owner, None)
            self.assertEqual(state.get("trackId"), "abc")

    async def test_source_skipped_claim_stays_unpublished(self):
        async def skipped_transition(request):
            return SimpleNamespace(
                committed=False,
                transition_id="tr-claim-skip",
                target_rate=44100,
                state={"committed": False, "skipped": True, "reason": "source-changed"},
            )

        with patch.object(main, "get_spotify_ui_state", AsyncMock(
            return_value={"status": "Playing", "trackId": "abc", "title": "Late", "available": True}
        )), patch.object(main, "_run_coordinated_transition", skipped_transition):
            state = await main._claim_spotify_playback()
            self.assertEqual(main.playback_state.current_playback_owner, None)
            self.assertEqual(state.get("trackId"), "abc")

    async def test_noted_qobuz_claim_does_not_reactivate_after_switch(self):
        entered_transition = asyncio.Event()
        release_transition = asyncio.Event()

        async def blocking_transition(request):
            entered_transition.set()
            await release_transition.wait()
            return SimpleNamespace(
                committed=True,
                transition_id="tr-qobuz-claim",
                target_rate=44100,
                state={"committed": True},
            )

        with patch.object(main, "get_qobuz_ui_state", AsyncMock(
            return_value={"status": "Playing", "trackId": "q-1", "available": True}
        )), patch.object(main, "_run_coordinated_transition", blocking_transition):
            claim = asyncio.create_task(main._claim_qobuz_playback())
            try:
                await asyncio.wait_for(entered_transition.wait(), timeout=2)
                main.playback_state.note_source_selection("bluetooth-input")
                release_transition.set()
                await asyncio.wait_for(claim, timeout=5)
            finally:
                if not claim.done():
                    claim.cancel()
            self.assertEqual(main.playback_state.current_playback_owner, None)


class SourceRouteTests(SourceRaceCase):
    def _request(self, mode, input_key=None):
        request = Mock()
        payload = {"mode": mode}
        if input_key is not None:
            payload["inputKey"] = input_key
        request.json = AsyncMock(return_value=payload)
        return request

    def _external_overview(self, key="line-in"):
        return {"mode": "external-input", "selected_input": {"key": key}}

    async def test_monitor_peak_publish_cannot_follow_newer_source_switch(self):
        persisted = {"mode": "bluetooth-input", "selected_input_key": None}
        entered = asyncio.Event()
        release = asyncio.Event()
        peak_modes = []

        async def monitor_peak(overview):
            if overview["mode"] == "bluetooth-input":
                entered.set()
                await release.wait()
            peak_modes.append(overview["mode"])

        async def route_peak(overview):
            peak_modes.append(overview["mode"])

        monitor = BluetoothInputMonitor(BluetoothInputDependencies(
            sync_peak_monitor_for_source_mode_state=monitor_peak,
            get_persisted_source_mode=lambda: persisted["mode"],
            get_source_transition_lock=lambda: main._source_transition_lock(),
        ))

        def select(mode, input_key=None):
            persisted.update(mode=mode, selected_input_key=input_key)
            return self._external_overview(input_key or "line-in") if mode == "external-input" else {"mode": mode}

        with patch.object(main.samplerate, "_load_audio_source_selection", side_effect=lambda: dict(persisted)), patch.object(
            main, "set_audio_source_selection", side_effect=select
        ), patch.object(main, "bluetooth_input", monitor), patch.object(
            main.external_input, "sync", AsyncMock(side_effect=lambda overview: overview)
        ), patch.object(monitor, "sync", AsyncMock(side_effect=lambda overview: overview)), patch.object(
            bluetooth_module, "get_audio_source_overview", side_effect=lambda: {"mode": persisted["mode"]}
        ), patch.object(main, "_pause_all_app_playback_for_external_input", AsyncMock()), patch.object(
            main.peak_monitor_coordinator, "sync_source_mode_state", route_peak
        ):
            monitor.monitor_task = asyncio.create_task(monitor.run_monitor_loop())
            try:
                await asyncio.wait_for(entered.wait(), 2)
                switch = asyncio.create_task(main.save_audio_source_selection_route(
                    self._request("external-input", "line-in")
                ))
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assertFalse(switch.done())
                self.assertEqual(peak_modes, [])
                release.set()
                await asyncio.wait_for(switch, 2)
                await asyncio.sleep(0)
                self.assertEqual(peak_modes[-1], "external-input")
            finally:
                release.set()
                await monitor.stop()

    async def test_older_bluetooth_sync_cannot_relink_after_newer_external_switch(self):
        persisted = {"mode": "bluetooth-input", "selected_input_key": None}
        entered = asyncio.Event()
        release = asyncio.Event()
        monitor = BluetoothInputMonitor(BluetoothInputDependencies(
            sync_peak_monitor_for_source_mode_state=AsyncMock(),
            get_persisted_source_mode=lambda: persisted["mode"],
        ))

        async def slow_agent():
            entered.set()
            await release.wait()

        def select(mode, input_key=None):
            persisted.update(mode=mode, selected_input_key=input_key)
            if mode == "external-input":
                return self._external_overview(input_key or "line-in")
            return {"mode": mode, "bluetooth": {"selectable": True}}

        with patch.object(main.samplerate, "_load_audio_source_selection", side_effect=lambda: dict(persisted)), patch.object(
            main, "set_audio_source_selection", side_effect=select
        ), patch.object(main, "bluetooth_input", monitor), patch.object(
            main.external_input, "sync", AsyncMock(side_effect=lambda overview: overview)
        ), patch.object(monitor, "_ensure_agent", slow_agent), patch.object(
            monitor, "_link_source_to_dsp", AsyncMock()
        ), patch.object(bluetooth_module, "input_links_present", AsyncMock(return_value=True)), patch.object(
            monitor, "stop_agent", AsyncMock()
        ), patch.object(
            bluetooth_module, "get_bluetooth_audio_overview",
            return_value={"receiver_session": {"source_name": "bluez-old"}},
        ), patch.object(
            bluetooth_module, "get_audio_source_overview",
            side_effect=lambda: {"mode": persisted["mode"]},
        ), patch.object(bluetooth_module, "set_bluetooth_receiver_enabled"), patch.object(
            bluetooth_module, "disconnect_connected_bluetooth_audio_sources", return_value=[]
        ), patch.object(main, "_pause_all_app_playback_for_external_input", AsyncMock()), patch.object(
            main.peak_monitor_coordinator, "sync_source_mode_state", AsyncMock()
        ):
            older = asyncio.create_task(monitor.sync({"mode": "bluetooth-input", "bluetooth": {"selectable": True}}))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                newer = asyncio.create_task(main.save_audio_source_selection_route(
                    self._request("external-input", "line-in")
                ))
                await asyncio.sleep(0)
                release.set()
                await asyncio.wait_for(older, 2)
                await asyncio.wait_for(newer, 2)
                self.assertGreater(main.playback_state.source_generation, self._state["source_generation"])
                self.assertIsNone(monitor.input_source_name)
            finally:
                release.set()
                if not older.done():
                    older.cancel()
                await asyncio.gather(older, return_exceptions=True)

    async def test_switch_bumps_generation_and_switch_back_restores_play(self):
        persisted = {"mode": "app-playback", "selected_input_key": None}

        def fake_load():
            return dict(persisted)

        def fake_set(mode, input_key=None):
            persisted["mode"] = mode
            persisted["selected_input_key"] = input_key
            if mode == "external-input":
                return self._external_overview(input_key or "line-in")
            return {"mode": mode, "selected_input": None}

        with patch.object(main.samplerate, "_load_audio_source_selection", side_effect=fake_load), patch.object(
            main, "set_audio_source_selection", side_effect=fake_set
        ), patch.object(main.external_input, "sync", AsyncMock(side_effect=lambda overview: overview)
        ), patch.object(main.bluetooth_input, "sync", AsyncMock(side_effect=lambda overview: overview)
        ), patch.object(main, "_pause_all_app_playback_for_external_input", AsyncMock()
        ), patch.object(main.peak_monitor_coordinator, "sync_source_mode_state", AsyncMock()):
            before = main.playback_state.source_generation
            result = await main.save_audio_source_selection_route(self._request("external-input", "line-in"))
            self.assertEqual(result["mode"], "external-input")
            self.assertEqual(main.playback_state.source_generation, before + 1)
            self.assertEqual(main.playback_state.source_mode, "external-input")
            result = await main.save_audio_source_selection_route(self._request("app-playback"))
            self.assertEqual(main.playback_state.source_generation, before + 2)
            self.assertEqual(main.playback_state.source_mode, "app-playback")

            # A fresh intent after the switch-back validates and commits.
            captured = main.playback_state.capture_source_intent()
            try:
                main._ensure_source_intent_current(captured)
            except main.HTTPException:
                self.fail("fresh intent after switch-back must validate")

    async def test_cancellation_during_pause_leaves_consistent_state(self):
        entered_pause = asyncio.Event()
        release_pause = asyncio.Event()
        calls: list[str] = []

        async def blocking_release():
            entered_pause.set()
            await release_pause.wait()
            return True

        player = Mock()
        player._running = True
        player.state = {"current_file": "/music/old.flac", "playing": True, "paused": False, "ended": False}
        player.stop_playback = Mock(side_effect=lambda: calls.append("mpv-stop"))

        async def fake_broadcast(message):
            calls.append("broadcast")

        with patch.object(main.samplerate, "_load_audio_source_selection", return_value={
            "mode": "app-playback", "selected_input_key": None
        }), patch.object(main, "set_audio_source_selection", return_value=self._external_overview()
        ), patch.object(main.external_input, "sync", AsyncMock(side_effect=lambda overview: overview)
        ), patch.object(main.bluetooth_input, "sync", AsyncMock(side_effect=lambda overview: overview)
        ), patch.object(main.runtime, "player_instance", player), patch.object(
            main, "manager", SimpleNamespace(broadcast=fake_broadcast)
        ), patch.object(main.media_readiness, "wait_for_pipewire_mpv_release", blocking_release), patch.object(
            main, "get_spotify_ui_state", AsyncMock(return_value={"status": "Paused"})
        ), patch.object(main, "get_qobuz_ui_state", AsyncMock(return_value={"status": "Stopped"})), patch.object(
            main.peak_monitor_coordinator, "sync_source_mode_state", AsyncMock(side_effect=lambda overview: calls.append("peak-sync"))
        ), patch.object(main, "_coordinator_lock_for_source_pause", return_value=None):
            switch = asyncio.create_task(
                main.save_audio_source_selection_route(self._request("external-input", "line-in"))
            )
            try:
                await asyncio.wait_for(entered_pause.wait(), timeout=2)
                switch.cancel()
                release_pause.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(switch, timeout=5)
            finally:
                if not switch.done():
                    switch.cancel()
            # Routing committed, generations advanced, pause ran to completion
            # despite the caller cancellation, peak state synced.
            self.assertIn("mpv-stop", calls)
            self.assertIn("peak-sync", calls)
            self.assertEqual(main.playback_state.source_mode, "external-input")
            self.assertEqual(main.playback_state.source_generation, self._state["source_generation"] + 1)
            # The lock is free again for the switch-back.
            self.assertFalse(main.runtime.source_transition_lock.locked())

    async def test_same_mode_reselect_keeps_generation(self):
        with patch.object(main.samplerate, "_load_audio_source_selection", return_value={
            "mode": "app-playback", "selected_input_key": None
        }), patch.object(main, "set_audio_source_selection", return_value={
            "mode": "app-playback", "selected_input": None
        }), patch.object(main.external_input, "sync", AsyncMock(side_effect=lambda overview: overview)
        ), patch.object(main.bluetooth_input, "sync", AsyncMock(side_effect=lambda overview: overview)
        ), patch.object(main.peak_monitor_coordinator, "sync_source_mode_state", AsyncMock()):
            before = main.playback_state.source_generation
            await main.save_audio_source_selection_route(self._request("app-playback"))
            self.assertEqual(main.playback_state.source_generation, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
