#!/usr/bin/env python3
"""Focused tests for same-source transport versus Coordinator handoffs."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from playback_transition_test_support import make_transition_runtime
from playback_transition import TransitionRequest


class PlayerDouble:
    _running = True

    def __init__(self, *, paused: bool = False) -> None:
        self._state = {
            "current_file": "/music/current.flac",
            "playing": not paused,
            "paused": paused,
            "ended": False,
            "volume": 73,
        }
        self.pause = Mock(side_effect=self._toggle_pause)
        self.stop_playback = Mock()

    @property
    def state(self) -> dict:
        return self._state

    def _toggle_pause(self) -> None:
        paused = not bool(self._state.get("paused"))
        self._state["paused"] = paused
        self._state["playing"] = not paused


class TransportContractTests(unittest.IsolatedAsyncioTestCase):
    def _payload(self, state):
        return dict(state)

    async def test_api_pause_restores_v094_toggle_without_coordinator(self):
        player = PlayerDouble()
        coordinator = AsyncMock(side_effect=AssertionError("transport entered coordinator"))
        with patch.object(main, "player_instance", player), patch.object(
            main, "_run_coordinated_transition", coordinator
        ), patch.object(main, "build_playback_payload", side_effect=self._payload), patch.object(
            main, "_mark_player_state_authoritative"
        ), patch.object(main, "_mark_playback_intent_changed"):
            first = await main.pause_playback()
            second = await main.pause_playback()

        self.assertEqual(first["status"], "paused")
        self.assertEqual(second["status"], "playing")
        self.assertEqual(player.pause.call_count, 2)
        coordinator.assert_not_awaited()

    async def test_local_toggle_pause_is_transport_only(self):
        player = PlayerDouble()
        coordinator = AsyncMock(side_effect=AssertionError("pause entered coordinator"))
        track = {"source": "local", "url": "/music/current.flac", "sample_rate_hz": 44100}
        with patch.object(main, "player_instance", player), patch.object(
            main, "current_track_info", track
        ), patch.object(main, "_can_send_play_command", return_value=True), patch.object(
            main, "_run_coordinated_transition", coordinator
        ), patch.object(main, "build_playback_payload", side_effect=self._payload), patch.object(
            main, "_mark_player_state_authoritative"
        ), patch.object(main, "_mark_playback_intent_changed"):
            result = await main.toggle_playback()

        self.assertEqual(result["status"], "paused")
        player.pause.assert_called_once_with()
        coordinator.assert_not_awaited()

    async def test_local_toggle_resume_remains_a_coordinator_play(self):
        player = PlayerDouble(paused=True)
        async def run(request):
            player._state["paused"] = False
            player._state["playing"] = True
            return SimpleNamespace(target_rate=44100)

        commit = Mock()
        track = {"source": "radio", "url": "https://radio.example/live", "sample_rate_hz": 44100}
        request_rate_change = Mock(return_value=False)
        run_mock = AsyncMock(side_effect=run)
        with patch.object(main, "player_instance", player), patch.object(
            main, "current_track_info", track
        ), patch.object(main, "_can_send_play_command", return_value=True), patch.object(
            main, "_run_coordinated_transition", run_mock
        ), patch.object(main, "_coordinator_rate_change", request_rate_change), patch.object(
            main, "_commit_coordinated_track", commit
        ), patch.object(main, "build_playback_payload", side_effect=self._payload):
            result = await main.toggle_playback()

        self.assertEqual(result["status"], "playing")
        player.pause.assert_not_called()
        request = run_mock.await_args.args[0]
        self.assertIsInstance(request, TransitionRequest)
        self.assertEqual(request.operation, "resume")
        self.assertEqual(request.source, "radio")
        self.assertTrue(request.should_play)
        commit.assert_called_once()

    async def test_spotify_transport_commands_do_not_touch_local_context(self):
        player = PlayerDouble()
        local_track = {"source": "local", "url": "/music/current.flac"}
        queue = [{"id": "a"}, {"id": "b"}]
        coordinator = AsyncMock(side_effect=AssertionError("Spotify transport entered coordinator"))
        broadcast = AsyncMock(side_effect=lambda data: data)
        actions = (
            (main.api_spotify_pause, "spotify_pause", {"status": "Paused"}),
            (main.api_spotify_next, "spotify_next", {"status": "Playing", "title": "next"}),
            (main.api_spotify_previous, "spotify_previous", {"status": "Playing", "title": "previous"}),
        )
        for endpoint, action, response in actions:
            with self.subTest(action=action):
                transport = AsyncMock(return_value=response)
                with patch.object(main, "player_instance", player), patch.object(
                    main, "current_track_info", local_track
                ), patch.object(main, "playback_queue", queue), patch.object(
                    main, action, transport
                ), patch.object(main, "_run_coordinated_transition", coordinator), patch.object(
                    main, "broadcast_spotify_state", broadcast
                ):
                    before_track = dict(main.current_track_info)
                    before_queue = list(main.playback_queue)
                    await endpoint()
                    after_track = dict(main.current_track_info)
                    after_queue = list(main.playback_queue)

                transport.assert_awaited_once()
                self.assertEqual(after_track, before_track)
                self.assertEqual(after_queue, before_queue)
                player.stop_playback.assert_not_called()
        coordinator.assert_not_awaited()

    async def test_spotify_toggle_playing_only_pauses_spotify(self):
        player = PlayerDouble()
        pause = AsyncMock(return_value={"status": "Paused"})
        coordinator = AsyncMock(side_effect=AssertionError("Spotify pause entered coordinator"))
        broadcast = AsyncMock(side_effect=lambda data: data)
        with patch.object(main, "player_instance", player), patch.object(
            main, "current_track_info", {"source": "local", "url": "/music/current.flac"}
        ), patch.object(
            main, "get_spotify_ui_state", new=AsyncMock(return_value={"status": "Playing"})
        ), patch.object(main, "spotify_pause", pause), patch.object(
            main, "_run_coordinated_transition", coordinator
        ), patch.object(main, "broadcast_spotify_state", broadcast):
            await main.api_spotify_toggle()

        pause.assert_awaited_once()
        coordinator.assert_not_awaited()
        player.stop_playback.assert_not_called()

    async def test_spotify_toggle_start_uses_coordinator_source_handoff(self):
        run = AsyncMock()
        states = iter(({"status": "Paused"}, {"status": "Playing"}))
        with patch.object(
            main, "get_spotify_ui_state", new=AsyncMock(side_effect=lambda: next(states))
        ), patch.object(main, "_coordinator_rate_change", return_value=False), patch.object(
            main, "_run_coordinated_transition", run
        ), patch.object(main, "broadcast_spotify_state", new=AsyncMock(side_effect=lambda data: data)):
            await main.api_spotify_toggle()

        request = run.await_args.args[0]
        self.assertEqual(request.operation, "spotify-toggle")
        self.assertEqual(request.source, "spotify")
        self.assertTrue(request.should_play)
        self.assertTrue(request.reload_source)


class FooterOwnershipContractTests(unittest.TestCase):
    def test_active_spotify_wins_over_loaded_paused_local_context(self):
        player = SimpleNamespace(
            state={
                "current_file": "/music/paused.flac",
                "playing": False,
                "paused": True,
                "ended": False,
            }
        )
        local_track = {"source": "local", "url": "/music/paused.flac"}
        with patch.object(main, "player_instance", player), patch.object(
            main, "current_track_info", local_track
        ), patch.object(main, "current_footer_owner", "local"):
            owner = main._get_authoritative_footer_owner(
                spotify_state={"available": True, "status": "Playing"}
            )

        self.assertEqual(owner, "spotify")

    def test_loaded_paused_spotify_does_not_hide_active_local_playback(self):
        player = SimpleNamespace(
            state={
                "current_file": "/music/active.flac",
                "playing": True,
                "paused": False,
                "ended": False,
            }
        )
        local_track = {"source": "local", "url": "/music/active.flac"}
        with patch.object(main, "player_instance", player), patch.object(
            main, "current_track_info", local_track
        ), patch.object(main, "current_footer_owner", "spotify"):
            owner = main._get_authoritative_footer_owner(
                spotify_state={"available": True, "status": "Paused"}
            )

        self.assertEqual(owner, "local")


class QuietSourceContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_local_source_does_not_quiet_paused_spotify(self):
        player = SimpleNamespace(
            state={"current_file": "/music/current.flac", "playing": True, "paused": False},
            set_volume=Mock(),
            set_pause=Mock(),
            stop_playback=Mock(),
        )
        pause_spotify = AsyncMock()
        request = TransitionRequest(
            operation="play",
            source="local",
            target_rate=44100,
            target_url="/music/new.flac",
            should_play=True,
            reload_source=True,
        )
        with patch.object(main, "player_instance", player), patch.object(
            main, "current_track_info", {"source": "local", "url": "/music/current.flac"}
        ), patch.object(main, "get_spotify_ui_state", new=AsyncMock(return_value={"status": "Paused"})), patch.object(
            main, "pause_spotify_for_local_playback_broadcast", pause_spotify
        ), patch.object(main, "_player_is_running", return_value=True), patch.object(
            main, "_wait_for_pipewire_mpv_release", new=AsyncMock(return_value=True)
        ):
            await make_transition_runtime().quiet_old_source(request)

        pause_spotify.assert_not_awaited()
        player.set_volume.assert_called_once_with(0)
        player.set_pause.assert_called_once_with(True)

    async def test_local_source_change_quiets_playing_spotify(self):
        player = SimpleNamespace(
            state={"current_file": "/music/new.flac", "playing": True, "paused": False},
            set_volume=Mock(),
            set_pause=Mock(),
            stop_playback=Mock(),
        )
        pause_spotify = AsyncMock()
        request = TransitionRequest(
            operation="play",
            source="local",
            target_rate=44100,
            target_url="/music/new.flac",
            should_play=True,
            reload_source=True,
        )
        with patch.object(main, "player_instance", player), patch.object(
            main, "current_track_info", {"source": "local", "url": "/music/old.flac"}
        ), patch.object(main, "get_spotify_ui_state", new=AsyncMock(return_value={
            "available": True, "status": "Playing"
        })), patch.object(main, "pause_spotify_for_local_playback_broadcast", pause_spotify), patch.object(
            main, "_wait_for_pipewire_spotify_release", new=AsyncMock(return_value=True)
        ) as release, patch.object(main, "_player_is_running", return_value=True
        ):
            await make_transition_runtime().quiet_old_source(request)

        pause_spotify.assert_awaited_once()
        release.assert_awaited_once()

    async def test_spotify_release_ignores_corked_historical_input(self):
        entries = iter((
            [{
                "id": "active",
                "corked": False,
                "muted": False,
                "volume_percent": 100,
            }, {
                "id": "old",
                "corked": True,
                "muted": False,
                "volume_percent": 100,
            }],
            [{
                "id": "old",
                "corked": True,
                "muted": False,
                "volume_percent": 100,
            }],
        ))
        with patch.object(main, "_list_spotify_sink_inputs", side_effect=lambda: next(entries)), patch.object(
            main.asyncio, "sleep", new=AsyncMock()
        ):
            released = await main._wait_for_pipewire_spotify_release(timeout_ms=100)

        self.assertTrue(released)

    async def test_local_handoff_aborts_before_mpv_mutation_when_spotify_stays_active(self):
        player = SimpleNamespace(
            state={"current_file": "/music/new.flac", "playing": True, "paused": False},
            set_volume=Mock(),
            set_pause=Mock(),
            stop_playback=Mock(),
        )
        pause_spotify = AsyncMock()
        request = TransitionRequest(
            operation="play",
            source="local",
            target_rate=44100,
            target_url="/music/new.flac",
            should_play=True,
            reload_source=True,
        )
        with patch.object(main, "player_instance", player), patch.object(
            main, "current_track_info", {"source": "local", "url": "/music/old.flac"}
        ), patch.object(main, "get_spotify_ui_state", new=AsyncMock(return_value={
            "available": True, "status": "Playing"
        })), patch.object(main, "pause_spotify_for_local_playback_broadcast", pause_spotify), patch.object(
            main, "_wait_for_pipewire_spotify_release", new=AsyncMock(return_value=False)
        ), patch.object(main, "_player_is_running", return_value=True
        ):
            with self.assertRaisesRegex(RuntimeError, "did not quiesce"):
                await make_transition_runtime().quiet_old_source(request)

        pause_spotify.assert_awaited_once()
        player.set_volume.assert_not_called()
        player.set_pause.assert_not_called()


if __name__ == "__main__":
    unittest.main()
