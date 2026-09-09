#!/usr/bin/env python3
"""Focused tests for the P1-6 fix: manual transport endpoints must not
mutate transport/queue state while a PlaybackTransitionCoordinator transition
is active.

While a transition is active (coordinator.transition_active), the transport
endpoints /api/pause, /api/playback/toggle, /api/stop and /api/playback/seek
must be rejected early with HTTP 409 before any player command, queue
mutation, track-context change, persistence or broadcast.  Once the
transition is no longer active, the endpoints keep their normal semantics.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import playback.queue as playback_queue
import main
import playback.media_readiness as media_readiness
from fastapi import HTTPException
from playback_queue_test_support import queue_state, restore_queue_state


class PlayerDouble:
    _running = True

    def __init__(self, *, paused: bool = False) -> None:
        self._state = {
            "current_file": "/music/current.flac",
            "playing": not paused,
            "paused": paused,
            "ended": False,
            "volume": 73,
            "position": 12.0,
        }
        self.pause = Mock(side_effect=self._toggle_pause)
        self.stop_playback = Mock()
        self.seek = Mock(side_effect=lambda pos: self._state.update(position=pos))

    @property
    def state(self) -> dict:
        return self._state

    def _toggle_pause(self) -> None:
        paused = not bool(self._state.get("paused"))
        self._state["paused"] = paused
        self._state["playing"] = not paused


def _seek_request(position: float = 30.0):
    return SimpleNamespace(json=AsyncMock(return_value={"position": position}))


def _transition_coordinator(active: bool):
    return SimpleNamespace(transition_active=active)


def _base_patches(player, coordinator=None):
    """Shared patch set; returns (ExitStack, authoritative_mock, intent_mock)."""
    stack = ExitStack()
    stack.enter_context(patch.object(main.runtime, "player_instance", player))
    stack.enter_context(patch.object(main, "build_playback_payload", side_effect=dict))
    mark_authoritative = stack.enter_context(
        patch.object(main, "_mark_player_state_authoritative")
    )
    mark_intent = stack.enter_context(patch.object(main, "_mark_playback_intent_changed"))
    stack.enter_context(patch.object(main, "_can_send_play_command", return_value=True))
    if coordinator is not None:
        stack.enter_context(patch.object(main, "playback_transition_coordinator", coordinator))
    return stack, mark_authoritative, mark_intent


class ActiveTransitionGuardTests(unittest.IsolatedAsyncioTestCase):
    """All four transport endpoints are rejected before any mutation."""

    async def test_pause_during_active_transition_is_conflict_and_noop(self):
        player = PlayerDouble()
        coordinator = _transition_coordinator(True)
        stack, mark_authoritative, mark_intent = _base_patches(player, coordinator)
        with stack:
            with self.assertRaises(HTTPException) as cm:
                await main.pause_playback()
            self.assertEqual(cm.exception.status_code, 409)
            player.pause.assert_not_called()
            self.assertFalse(player.state["paused"])
            self.assertEqual(player.state["position"], 12.0)
            mark_authoritative.assert_not_called()
            mark_intent.assert_not_called()

    async def test_toggle_pause_branch_during_active_transition_is_conflict(self):
        player = PlayerDouble()
        coordinator = _transition_coordinator(True)
        track = {"source": "local", "url": "/music/current.flac"}
        with patch.object(main.runtime, "player_instance", player), patch.object(
            main, "playback_transition_coordinator", coordinator
        ), patch.object(main.playback_state, "current_track_info", track), patch.object(
            main, "_can_send_play_command", return_value=True
        ), patch.object(main, "_mark_player_state_authoritative"), patch.object(
            main, "_mark_playback_intent_changed"
        ):
            with self.assertRaises(HTTPException) as cm:
                await main.toggle_playback()
            self.assertEqual(cm.exception.status_code, 409)
            player.pause.assert_not_called()
            self.assertFalse(player.state["paused"])

    async def test_toggle_resume_branch_during_active_transition_is_conflict(self):
        player = PlayerDouble(paused=True)
        coordinator = _transition_coordinator(True)
        track = {"source": "radio", "url": "https://radio.example/live"}
        transition_run = AsyncMock(side_effect=AssertionError("resume entered coordinator"))
        with patch.object(main.runtime, "player_instance", player), patch.object(
            main, "playback_transition_coordinator", coordinator
        ), patch.object(main.playback_state, "current_track_info", track), patch.object(
            main, "_can_send_play_command", return_value=True
        ), patch.object(main, "_run_coordinated_transition", transition_run):
            with self.assertRaises(HTTPException) as cm:
                await main.toggle_playback()
            self.assertEqual(cm.exception.status_code, 409)
            transition_run.assert_not_awaited()
            self.assertTrue(player.state["paused"])

    async def test_stop_during_active_transition_preserves_queue_and_context(self):
        player = PlayerDouble()
        coordinator = _transition_coordinator(True)
        track = {"source": "radio", "id": "radio_1", "url": "https://radio.example/live"}
        previous_radio = {"source": "radio", "id": "radio_0", "url": "https://radio.example/old"}
        queue = [{"id": "a"}, {"id": "b"}]
        saved_queue = queue_state()
        try:
            playback_queue.queue.tracks = [dict(item) for item in queue]
            with patch.object(main.runtime, "player_instance", player), patch.object(
                main, "playback_transition_coordinator", coordinator
            ), patch.object(main.playback_state, "current_track_info", track), patch.object(
                main.playback_state, "last_radio_track_info", previous_radio
            ), patch.object(main.radio_reconnect, "attempts", 3), patch.object(
                main.radio_reconnect, "url", "https://radio.example/reconnect"
            ), patch.object(main.radio_reconnect, "active_since", 5.0):
                with self.assertRaises(HTTPException) as cm:
                    await main.stop_playback()
                self.assertEqual(cm.exception.status_code, 409)
                player.stop_playback.assert_not_called()
                self.assertEqual(len(playback_queue.queue.tracks), 2)
                self.assertEqual(playback_queue.queue.tracks[0]["id"], "a")
                self.assertEqual(main.playback_state.current_track_info, track)
                self.assertEqual(main.playback_state.last_radio_track_info, previous_radio)
                self.assertEqual(main.radio_reconnect.attempts, 3)
                self.assertEqual(main.radio_reconnect.url, "https://radio.example/reconnect")
                self.assertEqual(main.radio_reconnect.active_since, 5.0)
        finally:
            restore_queue_state(saved_queue)

    async def test_clear_queue_during_active_transition_is_conflict_and_noop(self):
        player = PlayerDouble()
        coordinator = _transition_coordinator(True)
        queue = [{"id": "a"}, {"id": "b"}]
        saved_queue = queue_state()
        try:
            playback_queue.queue.tracks = [dict(item) for item in queue]
            with patch.object(main.runtime, "player_instance", player), patch.object(
                main, "playback_transition_coordinator", coordinator
            ), patch.object(
                main, "build_playback_payload", side_effect=dict
            ), patch.object(main.manager, "broadcast", new=AsyncMock()) as broadcast:
                with self.assertRaises(HTTPException) as cm:
                    await main.clear_playback_queue()

            self.assertEqual(cm.exception.status_code, 409)
            self.assertEqual(playback_queue.queue.tracks, queue)
            broadcast.assert_not_awaited()
        finally:
            restore_queue_state(saved_queue)

    async def test_seek_during_active_transition_is_conflict_before_body_read(self):
        player = PlayerDouble()
        coordinator = _transition_coordinator(True)
        request = _seek_request()
        with patch.object(main.runtime, "player_instance", player), patch.object(
            main, "playback_transition_coordinator", coordinator
        ), patch.object(main, "_can_send_play_command", return_value=True):
            with self.assertRaises(HTTPException) as cm:
                await main.seek_playback(request)
            self.assertEqual(cm.exception.status_code, 409)
            player.seek.assert_not_called()
            request.json.assert_not_awaited()
            self.assertEqual(player.state["position"], 12.0)

    async def test_seek_transition_starts_during_body_read_is_conflict(self):
        player = PlayerDouble()
        coordinator = _transition_coordinator(False)
        mark_intent = Mock()

        async def body_with_transition_start():
            # The transition begins while the request body is being read, i.e.
            # after the early guard and before the re-check.
            coordinator.transition_active = True
            return {"position": 30.0}

        request = SimpleNamespace(json=AsyncMock(side_effect=body_with_transition_start))
        with patch.object(main.runtime, "player_instance", player), patch.object(
            main, "playback_transition_coordinator", coordinator
        ), patch.object(main, "_can_send_play_command", return_value=True), patch.object(
            main, "build_playback_payload", side_effect=dict
        ), patch.object(main, "_mark_playback_intent_changed", mark_intent):
            with self.assertRaises(HTTPException) as cm:
                await main.seek_playback(request)
            self.assertEqual(cm.exception.status_code, 409)
            player.seek.assert_not_called()
            mark_intent.assert_not_called()
            self.assertEqual(player.state["position"], 12.0)


class InactiveTransitionSemanticsTests(unittest.IsolatedAsyncioTestCase):
    """Without an active transition the endpoints keep their normal semantics."""

    async def test_pause_works_when_transition_inactive(self):
        player = PlayerDouble()
        with patch.object(main.runtime, "player_instance", player), patch.object(
            main, "build_playback_payload", side_effect=dict
        ), patch.object(main, "_mark_player_state_authoritative"), patch.object(
            main, "_mark_playback_intent_changed"
        ):
            result = await main.pause_playback()

        self.assertEqual(result["status"], "paused")
        player.pause.assert_called_once_with()

    async def test_pause_holds_coordinator_lock_until_mpv_command_finishes(self):
        player = PlayerDouble()
        command_started = threading.Event()
        command_release = threading.Event()
        transition_entered = asyncio.Event()

        def blocking_pause():
            command_started.set()
            command_release.wait(1.0)
            player._toggle_pause()

        player.pause.side_effect = blocking_pause

        class Coordinator:
            def __init__(self):
                self.lock = asyncio.Lock()

            @property
            def transition_active(self):
                return self.lock.locked()

        coordinator = Coordinator()

        async def competing_transition():
            async with coordinator.lock:
                transition_entered.set()

        with patch.object(main.runtime, "player_instance", player), patch.object(
            main, "playback_transition_coordinator", coordinator
        ), patch.object(
            main, "build_playback_payload", side_effect=dict
        ), patch.object(
            main, "_mark_player_state_authoritative"
        ), patch.object(main, "_mark_playback_intent_changed"):
            pause_task = asyncio.create_task(main.pause_playback())
            self.assertTrue(await asyncio.to_thread(command_started.wait, 1.0))
            transition_task = asyncio.create_task(competing_transition())
            await asyncio.sleep(0)
            try:
                self.assertFalse(transition_entered.is_set())
            finally:
                command_release.set()
            await pause_task
            await transition_task

        self.assertTrue(transition_entered.is_set())

    async def test_toggle_works_when_transition_inactive(self):
        player = PlayerDouble()
        track = {"source": "local", "url": "/music/current.flac"}
        with patch.object(main.runtime, "player_instance", player), patch.object(
            main.playback_state, "current_track_info", track
        ), patch.object(main, "_can_send_play_command", return_value=True), patch.object(
            main, "build_playback_payload", side_effect=dict
        ), patch.object(main, "_mark_player_state_authoritative"), patch.object(
            main, "_mark_playback_intent_changed"
        ):
            result = await main.toggle_playback()

        self.assertEqual(result["status"], "paused")
        player.pause.assert_called_once_with()

    async def test_external_owner_toggle_runs_under_coordinator_lock(self):
        player = PlayerDouble()

        class Coordinator:
            def __init__(self):
                self.lock = asyncio.Lock()

            @property
            def transition_active(self):
                return self.lock.locked()

        coordinator = Coordinator()

        async def routed_control(action, request=None):
            self.assertTrue(coordinator.lock.locked())
            return {"routed": "spotify"}

        with patch.object(main.runtime, "player_instance", player), patch.object(
            main, "playback_transition_coordinator", coordinator
        ), patch.object(main, "_route_global_control", new=routed_control):
            result = await main.toggle_playback()

        self.assertEqual(result, {"routed": "spotify"})
        player.pause.assert_not_called()

    async def test_toggle_resume_rejected_when_transition_completes_during_rate_calc(self):
        # The resume fast path computes the rate in a worker thread before its
        # guard: a transition that starts and completes during that await must
        # invalidate the stale toggle instead of unpausing the new context.
        player = PlayerDouble(paused=True)
        track = {"source": "local", "url": "/music/current.flac", "sample_rate_hz": 44100}

        def rate_change_with_transition(_target):
            main.playback_state.begin_transition_attempt()
            main.playback_state.end_transition_attempt()
            return False

        with patch.object(main.runtime, "player_instance", player), patch.object(
            main.playback_state, "current_track_info", track
        ), patch.object(
            main, "_coordinator_rate_change", new=rate_change_with_transition
        ), patch.object(
            main, "_can_send_play_command", return_value=True
        ), patch.object(
            main, "build_playback_payload", side_effect=dict
        ), patch.object(main, "_mark_player_state_authoritative"), patch.object(
            main, "_mark_playback_intent_changed"
        ):
            with self.assertRaises(HTTPException) as cm:
                await main.toggle_playback()

        self.assertEqual(cm.exception.status_code, 409)
        self.assertTrue(player.state["paused"])

    async def test_stop_works_when_transition_inactive(self):
        player = PlayerDouble()
        track = {"source": "radio", "id": "radio_1", "url": "https://radio.example/live"}
        queue = [{"id": "a"}, {"id": "b"}]
        saved_queue = queue_state()
        try:
            playback_queue.queue.tracks = [dict(item) for item in queue]
            playback_queue.queue.original = [dict(item) for item in queue]
            playback_queue.queue.index = 0
            playback_queue.queue.mode = "app_replace"
            playback_queue.queue.loop = False
            playback_queue.queue.shuffle = False
            playback_queue.queue.single_track_loop = False
            with patch.object(main.runtime, "player_instance", player), patch.object(
                main.playback_state, "current_track_info", track
            ), patch.object(main.playback_state, "last_radio_track_info", {}), patch.object(
                main.radio_reconnect, "attempts", 0
            ), patch.object(main.radio_reconnect, "url", None), patch.object(
                main.radio_reconnect, "active_since", 0.0
            ), patch.object(main, "_mark_player_state_authoritative"), patch.object(
                main, "_mark_playback_intent_changed"
            ):
                result = await main.stop_playback()
                self.assertEqual(result["status"], "stopped")
                player.stop_playback.assert_called_once_with()
                self.assertEqual(playback_queue.queue.tracks, [])
                self.assertIsNone(main.playback_state.current_track_info)
        finally:
            restore_queue_state(saved_queue)

    async def test_stop_pauses_spotify_owner_before_clearing_context(self):
        player = PlayerDouble()
        self._orig_latest_spotify_state = main.playback_state.latest_spotify_state
        main.playback_state.latest_spotify_state = {"status": "Playing"}

        async def pause_spotify():
            self.assertEqual(main.playback_state.current_playback_owner, "spotify")
            return {"status": "Paused"}

        release = AsyncMock(return_value=True)
        try:
            with patch.object(main.runtime, "player_instance", player), patch.object(
                main.playback_state, "current_playback_owner", "spotify"
            ), patch.object(
                main.playback_state, "current_track_info", {"source": "spotify", "id": "track-1"}
            ), patch.object(
                main, "spotify_pause", new=AsyncMock(side_effect=pause_spotify)
            ) as pause, patch.object(
                media_readiness, "wait_for_pipewire_spotify_release", release
            ), patch.object(
                main, "_mark_player_state_authoritative"
            ), patch.object(
                main, "_mark_playback_intent_changed"
            ), patch.object(
                main.radio_reconnect, "reset"
            ), patch.object(
                main.playback_queue.queue, "reset"
            ), patch.object(
                main.playback_queue.queue, "reset_mpv_loop_state"
            ), patch.object(
                main, "get_samplerate_status", return_value={"active_rate": 44100}
            ), patch.object(main.samplerate, "clear_auto_policy_force_rate"):
                result = await main.stop_playback()

            self.assertEqual(result["status"], "stopped")
            pause.assert_awaited_once_with()
            release.assert_awaited_once_with()
            self.assertEqual(
                main.playback_state.latest_spotify_state, {"status": "Paused"}
            )
            self.assertIsNone(main.playback_state.current_playback_owner)
            player.stop_playback.assert_called_once_with()
        finally:
            main.playback_state.latest_spotify_state = self._orig_latest_spotify_state

    async def test_stop_pauses_qobuz_owner_before_clearing_context(self):
        player = PlayerDouble()

        async def pause_qobuz():
            self.assertEqual(main.playback_state.current_playback_owner, "qobuz")
            return {"status": "Paused"}

        release = AsyncMock(return_value=True)
        with patch.object(main.runtime, "player_instance", player), patch.object(
            main.playback_state, "current_playback_owner", "qobuz"
        ), patch.object(
            main.playback_state, "current_track_info", {"source": "qobuz", "id": "track-1"}
        ), patch.object(
            main, "qobuz_pause", new=AsyncMock(side_effect=pause_qobuz)
        ) as pause, patch.object(
            media_readiness, "wait_for_pipewire_qobuz_release", release
        ), patch.object(
            main, "_mark_player_state_authoritative"
        ), patch.object(
            main, "_mark_playback_intent_changed"
        ), patch.object(
            main.radio_reconnect, "reset"
        ), patch.object(
            main.playback_queue.queue, "reset"
        ), patch.object(
            main.playback_queue.queue, "reset_mpv_loop_state"
        ), patch.object(
            main, "get_samplerate_status", return_value={"active_rate": 44100}
        ), patch.object(main.samplerate, "clear_auto_policy_force_rate"):
            result = await main.stop_playback()

        self.assertEqual(result["status"], "stopped")
        pause.assert_awaited_once_with()
        release.assert_awaited_once_with()
        self.assertIsNone(main.playback_state.current_playback_owner)
        player.stop_playback.assert_called_once_with()

    async def test_seek_works_when_transition_inactive(self):
        player = PlayerDouble()
        request = _seek_request()
        with patch.object(main.runtime, "player_instance", player), patch.object(
            main, "_can_send_play_command", return_value=True
        ), patch.object(main, "build_playback_payload", side_effect=dict), patch.object(
            main, "_mark_playback_intent_changed"
        ):
            result = await main.seek_playback(request)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["position"], 30.0)
        player.seek.assert_called_once_with(30.0)
        self.assertEqual(player.state["position"], 30.0)


class TransitionEndRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """After the transition ends the same endpoints work immediately again."""

    async def test_race_transition_active_then_ends_pause_recovers(self):
        player = PlayerDouble()
        coordinator = _transition_coordinator(True)
        stack, _, _ = _base_patches(player, coordinator)
        with stack:
            with self.assertRaises(HTTPException) as cm:
                await main.pause_playback()
            self.assertEqual(cm.exception.status_code, 409)
            player.pause.assert_not_called()

            coordinator.transition_active = False

            result = await main.pause_playback()
            self.assertEqual(result["status"], "paused")
            player.pause.assert_called_once_with()

    async def test_race_transition_active_then_ends_stop_recovers(self):
        player = PlayerDouble()
        coordinator = _transition_coordinator(True)
        track = {"source": "local", "url": "/music/current.flac"}
        queue = [{"id": "a"}]
        saved_queue = queue_state()
        try:
            playback_queue.queue.tracks = [dict(item) for item in queue]
            playback_queue.queue.original = [dict(item) for item in queue]
            playback_queue.queue.index = 0
            playback_queue.queue.mode = "app_replace"
            playback_queue.queue.loop = False
            playback_queue.queue.shuffle = False
            playback_queue.queue.single_track_loop = False
            with patch.object(main.runtime, "player_instance", player), patch.object(
                main, "playback_transition_coordinator", coordinator
            ), patch.object(main.playback_state, "current_track_info", track), patch.object(
                main.radio_reconnect, "attempts", 0
            ), patch.object(main.radio_reconnect, "url", None), patch.object(
                main.radio_reconnect, "active_since", 0.0
            ), patch.object(main, "_mark_player_state_authoritative"), patch.object(
                main, "_mark_playback_intent_changed"
            ):
                with self.assertRaises(HTTPException) as cm:
                    await main.stop_playback()
                self.assertEqual(cm.exception.status_code, 409)
                player.stop_playback.assert_not_called()
                self.assertEqual(len(playback_queue.queue.tracks), 1)

                coordinator.transition_active = False

                result = await main.stop_playback()
                self.assertEqual(result["status"], "stopped")
                player.stop_playback.assert_called_once_with()
                self.assertEqual(playback_queue.queue.tracks, [])
        finally:
            restore_queue_state(saved_queue)


if __name__ == "__main__":
    unittest.main(verbosity=2)
