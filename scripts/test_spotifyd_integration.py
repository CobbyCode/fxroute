#!/usr/bin/env python3
"""Focused tests for the spotifyd integration.

Covers instance-suffixed MPRIS detection (``spotifyd.instance<PID>``),
PID-change handling (no cached bus names), desktop-before-spotifyd priority,
the connect-state semantics (ready/connected/offline), TransferPlayback via
``rs.spotifyd.Controls``, transport over spotifyd, the MPRIS watch player
selection and the PipeWire sink-input matching for spotifyd streams.

No real playerctl, busctl, gdbus or spotifyd is required; the subprocess
helpers are patched at the boundary.
"""

import asyncio
import contextlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main
from playback.spotify_watch import SpotifyPlayerctlWatch, SpotifyWatchDependencies
from streaming.spotify import mpris
from streaming.spotify.mpris import (
    SPOTIFYD_CONTROLS_PREFIX,
    detect_backend,
    detect_running_backend,
    is_spotifyd_player,
    player_name,
    spotifyd_pid_from_name,
    transfer_playback,
)
from streaming.spotify.provider import SpotifyProvider


def _players(names):
    async def fake_list_players(timeout=2.0):
        return list(names)
    return fake_list_players


class SpotifydDetectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_mpris_spotifyd_wins_over_failed_process_probe(self):
        with mock.patch(
            "streaming.spotify.mpris.list_players",
            new=_players(["spotifyd.instance7"]),
        ), mock.patch(
            "streaming.spotify.mpris.spotifyd_process_running", return_value=False
        ):
            self.assertEqual(await detect_backend(), "spotifyd")

    async def test_running_backend_matches_instance_suffixed_spotifyd(self):
        with mock.patch("streaming.spotify.mpris.list_players", new=_players(["spotifyd.instance1234"])):
            self.assertEqual(await detect_running_backend(), "spotifyd")

    async def test_running_backend_bare_spotifyd_still_recognized(self):
        with mock.patch("streaming.spotify.mpris.list_players", new=_players(["spotifyd"])):
            self.assertEqual(await detect_running_backend(), "spotifyd")

    async def test_desktop_wins_over_instance_spotifyd(self):
        with mock.patch(
            "streaming.spotify.mpris.list_players",
            new=_players(["spotify", "spotifyd.instance909"]),
        ):
            self.assertEqual(await detect_running_backend(), "desktop")

    async def test_none_when_no_active_mpris_player(self):
        with mock.patch("streaming.spotify.mpris.list_players", new=_players([])):
            self.assertIsNone(await detect_running_backend())

    async def test_pid_change_is_detected_without_stale_cache(self):
        # Two consecutive calls with different PIDs: the instance suffix must
        # never be cached; the second (newer) PID is recognised transparently.
        with mock.patch(
            "streaming.spotify.mpris.list_players",
            new=_players(["spotifyd.instance100"]),
        ):
            self.assertEqual(await detect_running_backend(), "spotifyd")
        with mock.patch(
            "streaming.spotify.mpris.list_players",
            new=_players(["spotifyd.instance200"]),
        ):
            self.assertEqual(await detect_running_backend(), "spotifyd")

    def test_is_spotifyd_player_helpers(self):
        self.assertTrue(is_spotifyd_player("spotifyd"))
        self.assertTrue(is_spotifyd_player("spotifyd.instance42"))
        self.assertFalse(is_spotifyd_player("spotify"))
        self.assertFalse(is_spotifyd_player("spotifydinstance42"))
        self.assertFalse(is_spotifyd_player(""))

    def test_player_name_maps_spotifyd(self):
        self.assertEqual(player_name("spotifyd"), "spotifyd")
        self.assertEqual(player_name("desktop"), "spotify")

    def test_spotifyd_pid_from_name(self):
        self.assertEqual(spotifyd_pid_from_name("rs.spotifyd.instance1234"), 1234)
        self.assertIsNone(spotifyd_pid_from_name("rs.spotifyd.instance"))
        self.assertIsNone(spotifyd_pid_from_name("rs.spotifyd.instanceabc"))
        self.assertIsNone(spotifyd_pid_from_name("org.freedesktop.DBus"))


class SpotifydConnectStateTests(unittest.IsolatedAsyncioTestCase):
    """Provider status connect-state semantics for the spotifyd lifecycle."""

    async def test_ready_when_daemon_up_but_never_paired(self):
        # spotifyd installed and running (process up) but no MPRIS name yet.
        # spotifyd publishes no D-Bus name before the first pairing, so the
        # ready state is derived from process presence, not bus names.
        with mock.patch(
            "streaming.spotify.mpris.list_players", new=_players([])
        ), mock.patch(
            "streaming.spotify.mpris._spotifyd_installed", return_value=True
        ), mock.patch(
            "streaming.spotify.mpris._spotify_desktop_installed", return_value=False
        ), mock.patch(
            "streaming.spotify.mpris.playerctl_available", return_value=True
        ), mock.patch(
            "streaming.spotify.mpris.spotifyd_process_running", return_value=True
        ), mock.patch(
            "streaming.spotify.mpris.spotifyd_control_names", return_value=[]
        ), mock.patch(
            "streaming.spotify.mpris._run", return_value=None
        ):
            status = await SpotifyProvider().status()

        self.assertEqual(status["backend"], "spotifyd")
        self.assertEqual(status["connect_state"], "ready")
        self.assertTrue(status["ready"])
        self.assertEqual(status["status"], "Stopped")

    async def test_connected_when_controls_present_but_mpris_is_absent(self):
        # Controls means spotifyd is paired; without MPRIS it is not the
        # active playback device.
        with mock.patch(
            "streaming.spotify.mpris.list_players",
            new=_players([]),
        ), mock.patch(
            "streaming.spotify.mpris.spotifyd_control_names",
            return_value=["rs.spotifyd.instance5"],
        ), mock.patch(
            "streaming.spotify.mpris.spotifyd_process_running", return_value=True
        ), mock.patch(
            "streaming.spotify.mpris.playerctl_available", return_value=True
        ), mock.patch(
            "streaming.spotify.mpris._run", return_value=None
        ):
            status = await SpotifyProvider().status()

        self.assertEqual(status["backend"], "spotifyd")
        self.assertEqual(status["connect_state"], "connected")
        self.assertFalse(status["ready"])

    async def test_mpris_stopped_without_controls_is_ready_not_connected(self):
        with mock.patch(
            "streaming.spotify.mpris.list_players",
            new=_players(["spotifyd.instance5"]),
        ), mock.patch(
            "streaming.spotify.mpris.spotifyd_control_names",
            return_value=[],
        ), mock.patch(
            "streaming.spotify.mpris.playerctl_available", return_value=True
        ), mock.patch(
            "streaming.spotify.mpris._run", return_value=None
        ), mock.patch(
            "streaming.spotify.mpris.spotifyd_process_running", return_value=True
        ):
            status = await SpotifyProvider().status()

        self.assertEqual(status["connect_state"], "ready")

    async def test_playing_metadata_without_mpris_is_not_active_device(self):
        async def fake_run(*args, timeout=4.0):
            if args and args[1:2] == ("metadata",) and "--format" in args:
                return "Playing|Artist S|Title S|Album S|200000000|spotify:track:sd1"
            return None

        with mock.patch(
            "streaming.spotify.mpris.list_players", new=_players([])
        ), mock.patch(
            "streaming.spotify.mpris.spotifyd_control_names",
            return_value=["rs.spotifyd.instance7"],
        ), mock.patch(
            "streaming.spotify.mpris.playerctl_available", return_value=True
        ), mock.patch(
            "streaming.spotify.mpris._run", side_effect=fake_run
        ), mock.patch(
            "streaming.spotify.mpris.spotifyd_process_running", return_value=True
        ):
            status = await SpotifyProvider().status()

        self.assertEqual(status["status"], "Playing")
        self.assertEqual(status["connect_state"], "connected")

    async def test_offline_when_daemon_not_running(self):
        with mock.patch(
            "streaming.spotify.mpris.list_players", new=_players([])
        ), mock.patch(
            "streaming.spotify.mpris._spotifyd_installed", return_value=True
        ), mock.patch(
            "streaming.spotify.mpris._spotify_desktop_installed", return_value=False
        ), mock.patch(
            "streaming.spotify.mpris.playerctl_available", return_value=True
        ), mock.patch(
            "streaming.spotify.mpris.spotifyd_process_running", return_value=False
        ), mock.patch(
            "streaming.spotify.mpris._run", return_value=None
        ):
            status = await SpotifyProvider().status()

        self.assertEqual(status["backend"], "spotifyd")
        self.assertEqual(status["connect_state"], "offline")

    async def test_unavailable_when_playerctl_missing(self):
        with mock.patch("streaming.spotify.mpris.playerctl_available", return_value=False):
            status = await SpotifyProvider().status()
        self.assertFalse(status["available"])
        self.assertEqual(status["connect_state"], "unavailable")

    async def test_playing_when_spotifyd_is_active(self):
        async def fake_run(*args, timeout=4.0):
            if args and args[1:2] == ("metadata",) and "--format" in args:
                return "Playing|Artist S|Title S|Album S|200000000|spotify:track:sd1"
            return None

        with mock.patch(
            "streaming.spotify.mpris.list_players",
            new=_players(["spotifyd.instance7"]),
        ), mock.patch(
            "streaming.spotify.mpris.spotifyd_process_running", return_value=True
        ), mock.patch(
            "streaming.spotify.mpris.playerctl_available", return_value=True
        ), mock.patch(
            "streaming.spotify.mpris._run", side_effect=fake_run
        ):
            status = await SpotifyProvider().status()

        self.assertEqual(status["backend"], "spotifyd")
        self.assertEqual(status["connect_state"], "playing")
        self.assertEqual(status["status"], "Playing")
        self.assertEqual(status["title"], "Title S")
        self.assertEqual(status["trackId"], "spotify:track:sd1")

    async def test_mpris_active_is_not_degraded_to_offline_by_process_probe(self):
        with mock.patch(
            "streaming.spotify.mpris.list_players",
            new=_players(["spotifyd.instance7"]),
        ), mock.patch(
            "streaming.spotify.mpris.spotifyd_process_running", return_value=False
        ), mock.patch(
            "streaming.spotify.mpris.spotifyd_control_names", return_value=[]
        ), mock.patch(
            "streaming.spotify.mpris.playerctl_available", return_value=True
        ), mock.patch(
            "streaming.spotify.mpris._run", return_value=None
        ):
            status = await SpotifyProvider().status()

        self.assertNotEqual(status["connect_state"], "offline")


class SpotifydTransferPlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_transfer_playback_calls_controls_method_on_resolved_name(self):
        calls = []

        async def fake_run_checked(*args, timeout=4.0):
            calls.append(args)
            return "()"

        with mock.patch(
            "streaming.spotify.mpris.spotifyd_control_names",
            return_value=["rs.spotifyd.instance999"],
        ), mock.patch("streaming.spotify.mpris._run_checked", side_effect=fake_run_checked):
            result = await transfer_playback()

        self.assertTrue(result)
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertIn("gdbus", call)
        self.assertIn("rs.spotifyd.instance999", call)
        index = call.index("--method")
        self.assertEqual(call[index + 1], "rs.spotifyd.Controls.TransferPlayback")
        self.assertIn("/rs/spotifyd/Controls", call)

    async def test_transfer_playback_picks_latest_pid(self):
        calls = []

        async def fake_run_checked(*args, timeout=4.0):
            calls.append(args)
            return "()"

        with mock.patch(
            "streaming.spotify.mpris.spotifyd_control_names",
            return_value=["rs.spotifyd.instance100", "rs.spotifyd.instance250"],
        ), mock.patch("streaming.spotify.mpris._run_checked", side_effect=fake_run_checked):
            result = await transfer_playback()

        self.assertTrue(result)
        self.assertIn("rs.spotifyd.instance250", calls[0])

    async def test_transfer_playback_false_without_controls_name(self):
        with mock.patch("streaming.spotify.mpris.spotifyd_control_names", return_value=[]):
            self.assertFalse(await transfer_playback())

    async def test_transfer_playback_false_on_gdbus_failure(self):
        with mock.patch(
            "streaming.spotify.mpris.spotifyd_control_names",
            return_value=["rs.spotifyd.instance1"],
        ), mock.patch("streaming.spotify.mpris._run_checked", return_value=None):
            self.assertFalse(await transfer_playback())

    async def test_spotifyd_control_names_filters_bus_names(self):
        with mock.patch(
            "streaming.spotify.mpris._list_session_bus_names",
            return_value=[
                "org.freedesktop.DBus",
                "rs.spotifyd.instance42",
                "org.mpris.MediaPlayer2.spotify",
                ":1.77",
            ],
        ):
            names = await mpris.spotifyd_control_names()
        self.assertEqual(names, ["rs.spotifyd.instance42"])

    async def test_spotifyd_process_running_detects_daemon(self):
        # Pre-pairing spotifyd publishes no bus name; the daemon lifecycle is
        # observed via process presence instead.
        with mock.patch(
            "streaming.spotify.mpris._run_checked", return_value="1284917\n"
        ):
            self.assertTrue(await mpris.spotifyd_process_running())
        with mock.patch("streaming.spotify.mpris._run_checked", return_value=""):
            self.assertFalse(await mpris.spotifyd_process_running())
        with mock.patch("streaming.spotify.mpris._run_checked", return_value=None):
            self.assertFalse(await mpris.spotifyd_process_running())

    def test_control_prefix_matches_constants(self):
        self.assertTrue("rs.spotifyd.instance1".startswith(SPOTIFYD_CONTROLS_PREFIX))


class SpotifydTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_commands_target_spotifyd_player(self):
        run_args: list[tuple] = []

        async def fake_run(*args, timeout=4.0):
            run_args.append(args)
            return "Playing|A|T|" + "|".join(["", "", ""])

        provider = SpotifyProvider()
        with mock.patch("streaming.spotify.mpris.list_players", new=_players(["spotifyd.instance3"])), \
             mock.patch("streaming.spotify.mpris.playerctl_available", return_value=True), \
             mock.patch("streaming.spotify.mpris._run", side_effect=fake_run):
            await provider.next()

        self.assertTrue(run_args)
        self.assertIn("--player=spotifyd", run_args[0])


class SpotifydWatchTests(unittest.IsolatedAsyncioTestCase):
    def _deps(self):
        return SpotifyWatchDependencies(
            get_playback_state=lambda: SimpleNamespace(current_playback_owner=None),
            get_spotify_ui_state=lambda: {},
            list_spotify_sink_inputs=lambda: [],
            spotify_sink_input_observation=lambda *a, **k: None,
            request_coordinated_recovery=lambda *a, **k: None,
            schedule_spotify_state_refresh=lambda reason: None,
            claim_spotify_playback=lambda reason: None,
        )

    async def test_watch_spawns_playerctl_for_both_backends(self):
        spawned: list[tuple] = []

        class DummyProc:
            returncode = 1

            def __init__(self):
                self._stdout = asyncio.StreamReader()
                self._stdout.feed_eof()
                self._stderr = asyncio.StreamReader()
                self._stderr.feed_eof()

            @property
            def stdout(self):
                return self._stdout

            @property
            def stderr(self):
                return self._stderr

        async def fake_spawn(*args, **kwargs):
            spawned.append(args)
            return DummyProc()

        watch = SpotifyPlayerctlWatch(self._deps())
        with mock.patch("playback.spotify_watch.spotify_installed", return_value=True), \
             mock.patch("playback.spotify_watch.shutil.which", return_value="/usr/bin/playerctl"), \
             mock.patch("playback.spotify_watch._stop_process", new=AsyncMock()), \
             mock.patch.object(asyncio, "create_subprocess_exec", side_effect=fake_spawn):
            task = asyncio.create_task(watch.run_watch_loop(), name="watch-test")
            for _ in range(100):
                if spawned:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(spawned, "watch loop must spawn playerctl")
            self.assertIn("--player=spotify,spotifyd", spawned[0])
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def test_no_player_restart_gap_logs_info_once_then_debug(self):
        spawned = 0

        class DummyProc:
            returncode = 1

            def __init__(self):
                self._stdout = asyncio.StreamReader()
                self._stdout.feed_eof()
                self._stderr = asyncio.StreamReader()
                self._stderr.feed_eof()

            @property
            def stdout(self):
                return self._stdout

            @property
            def stderr(self):
                return self._stderr

        async def fake_spawn(*args, **kwargs):
            nonlocal spawned
            spawned += 1
            return DummyProc()

        async def stop_after_two_restarts(_delay):
            if spawned >= 2:
                raise asyncio.CancelledError

        watch = SpotifyPlayerctlWatch(self._deps())
        with mock.patch("playback.spotify_watch.spotify_installed", return_value=True), \
             mock.patch("playback.spotify_watch.shutil.which", return_value="/usr/bin/playerctl"), \
             mock.patch("playback.spotify_watch._stop_process", new=AsyncMock()), \
             mock.patch.object(asyncio, "create_subprocess_exec", side_effect=fake_spawn), \
             mock.patch("playback.spotify_watch.asyncio.sleep", new=stop_after_two_restarts), \
             mock.patch("playback.spotify_watch.logger.info") as log_info, \
             mock.patch("playback.spotify_watch.logger.debug") as log_debug:
            with contextlib.suppress(asyncio.CancelledError):
                await watch.run_watch_loop()

        self.assertEqual(spawned, 2)
        gap_info = [call for call in log_info.call_args_list if "no connected player yet" in str(call)]
        gap_debug = [call for call in log_debug.call_args_list if "still without a connected player" in str(call)]
        self.assertEqual(len(gap_info), 1)
        self.assertEqual(len(gap_debug), 1)


class SpotifydSinkInputTests(unittest.TestCase):
    def test_spotifyd_sink_inputs_are_matched(self):
        entries = [
            # Desktop favourites stay matched.
            {"id": "1", "properties": {"application.name": "spotify", "media.name": "Spotify"}},
            # spotifyd process/nodes (instance variants covered).
            {"id": "2", "properties": {"application.name": "spotifyd", "media.name": "Spotify"}},
            {"id": "3", "properties": {"node.name": "spotifyd", "media.name": "Spotify"}},
            {"id": "7", "properties": {"application.process.binary": "spotifyd-helper"}},
            # spotifyd librespot pulse stream: application/node/media names are
            # all empty; only the process binary names the owner.
            {"id": "6", "properties": {"application.process.binary": "spotifyd"}},
            # Unrelated streams must not be captured.
            {"id": "4", "properties": {"application.name": "mpv", "media.name": "local audio"}},
            {"id": "5", "properties": {"application.name": "qbzd", "media.name": "Qobuz"}},
        ]
        with mock.patch.object(main, "_list_sink_inputs", return_value=entries):
            matched = main._list_spotify_sink_inputs()

        ids = {entry["id"] for entry in matched}
        self.assertEqual(ids, {"1", "2", "3", "6"})


class SpotifydUiStartGuardTests(unittest.IsolatedAsyncioTestCase):
    """The explicit start endpoints handle spotifyd lifecycle states cleanly."""

    def _patched_status(self, **overrides):
        data = {
            "backend": "spotifyd",
            "connect_state": "ready",
            "status": "Stopped",
            "available": True,
            "installed": True,
            "source": "spotify",
            "title": "",
            "artist": "",
            "album": "",
            "trackId": "",
            "artUrl": "",
        }
        data.update(overrides)
        return data

    def _cached(self, data):
        """Seed the cached spotify state that the play guard reads."""
        previous = main.playback_state.latest_spotify_state
        main.playback_state.latest_spotify_state = data
        self.addCleanup(type(self)._restore_cached, previous)

    @staticmethod
    def _restore_cached(previous):
        main.playback_state.latest_spotify_state = previous

    async def test_play_ready_holds_without_transition(self):
        state = self._patched_status(connect_state="ready")
        transition = AsyncMock()
        broadcast = AsyncMock(return_value={"ok": True})
        self._cached(state)
        with mock.patch.object(main, "get_spotify_ui_state", AsyncMock(return_value=state)), \
             mock.patch.object(main, "broadcast_spotify_state", broadcast), \
             mock.patch.object(main, "_run_coordinated_transition", transition):
            result = await main.api_spotify_play()

        self.assertEqual(result, {"ok": True})
        transition.assert_not_awaited()
        broadcast.assert_awaited_once_with(state)
        self.assertEqual(main.playback_state.latest_spotify_state, state)

    async def test_play_connected_issues_transfer_then_transition(self):
        state = self._patched_status(connect_state="connected")
        transfer = AsyncMock(return_value=True)
        run = AsyncMock(return_value=SimpleNamespace(committed=True, transition_id="tr-1"))
        broadcast = AsyncMock(return_value={"ok": True})
        self._cached(state)
        with mock.patch.object(main, "get_spotify_ui_state", AsyncMock(return_value=state)), \
             mock.patch.object(main, "broadcast_spotify_state", broadcast), \
             mock.patch.object(main, "spotify_transfer_playback", transfer), \
             mock.patch.object(main.spotify_mpris, "detect_running_backend", AsyncMock(return_value="spotifyd")), \
             mock.patch.object(main, "_coordinator_target_rate", lambda *a, **k: 44100), \
             mock.patch.object(main, "_coordinator_rate_change", lambda *a, **k: False), \
             mock.patch.object(main, "_run_coordinated_transition", run), \
             mock.patch.object(main, "_publish_committed_playback_owner", AsyncMock()):
            result = await main.api_spotify_play()

        transfer.assert_awaited_once()
        run.assert_awaited_once()
        request = run.await_args.args[0]
        self.assertEqual(request.source, "spotify")
        self.assertEqual(request.operation, "spotify-play")
        self.assertEqual(result, {"ok": True})

    async def test_play_offline_holds_without_transition(self):
        state = self._patched_status(connect_state="offline")
        transition = AsyncMock()
        broadcast = AsyncMock(return_value={"ok": True})
        with mock.patch.object(main, "get_spotify_ui_state", AsyncMock(return_value=state)), \
             mock.patch.object(main, "broadcast_spotify_state", broadcast), \
             mock.patch.object(main, "_run_coordinated_transition", transition):
            result = await main.api_spotify_play()

        self.assertEqual(result, {"ok": True})
        transition.assert_not_awaited()

    async def test_play_transfer_failure_holds_without_transition(self):
        state = self._patched_status(connect_state="connected")
        transfer = AsyncMock(return_value=False)
        transition = AsyncMock()
        broadcast = AsyncMock(return_value={"ok": True})
        with mock.patch.object(main, "get_spotify_ui_state", AsyncMock(return_value=state)), \
             mock.patch.object(main, "spotify_transfer_playback", transfer), \
             mock.patch.object(main, "broadcast_spotify_state", broadcast), \
             mock.patch.object(main, "_run_coordinated_transition", transition):
            result = await main.api_spotify_play()

        self.assertEqual(result, {"ok": True})
        transfer.assert_awaited_once()
        transition.assert_not_awaited()

    async def test_play_waits_for_active_spotifyd_after_transfer(self):
        state = self._patched_status(connect_state="connected")
        transfer = AsyncMock(return_value=True)
        run = AsyncMock(return_value=SimpleNamespace(committed=True, transition_id="tr-wait"))
        broadcast = AsyncMock(return_value={"ok": True})
        active = mock.AsyncMock(side_effect=[None, "spotifyd"])
        with mock.patch.object(main, "get_spotify_ui_state", AsyncMock(return_value=state)), \
             mock.patch.object(main, "spotify_transfer_playback", transfer), \
             mock.patch.object(main.spotify_mpris, "detect_running_backend", active), \
             mock.patch.object(main, "_SPOTIFY_TRANSFER_WAIT_TIMEOUT_SECONDS", 0.1), \
             mock.patch.object(main, "_SPOTIFY_TRANSFER_WAIT_POLL_SECONDS", 0):
            with mock.patch.object(main, "broadcast_spotify_state", broadcast), \
                 mock.patch.object(main, "_coordinator_target_rate", lambda *a, **k: 44100), \
                 mock.patch.object(main, "_coordinator_rate_change", lambda *a, **k: False), \
                 mock.patch.object(main, "_run_coordinated_transition", run), \
                 mock.patch.object(main, "_publish_committed_playback_owner", AsyncMock()):
                await main.api_spotify_play()

        self.assertEqual(active.await_count, 2)
        run.assert_awaited_once()

    async def test_spotify_player_present_matches_instance_name(self):
        with mock.patch.object(
            main.spotify_mpris,
            "list_players",
            new=_players(["spotifyd.instance123"]),
        ):
            self.assertTrue(await main._spotify_player_present())

    async def test_play_refreshes_stale_ready_cache_before_guard(self):
        cached = self._patched_status(connect_state="ready")
        fresh = self._patched_status(connect_state="connected")
        transfer = AsyncMock(return_value=True)
        run = AsyncMock(return_value=SimpleNamespace(committed=True, transition_id="tr-fresh"))
        broadcast = AsyncMock(return_value={"ok": True})
        self._cached(cached)
        with mock.patch.object(main, "get_spotify_ui_state", AsyncMock(return_value=fresh)), \
             mock.patch.object(main, "broadcast_spotify_state", broadcast), \
             mock.patch.object(main, "spotify_transfer_playback", transfer), \
             mock.patch.object(main.spotify_mpris, "detect_running_backend", AsyncMock(return_value="spotifyd")), \
             mock.patch.object(main, "_coordinator_target_rate", lambda *a, **k: 44100), \
             mock.patch.object(main, "_coordinator_rate_change", lambda *a, **k: False), \
             mock.patch.object(main, "_run_coordinated_transition", run), \
             mock.patch.object(main, "_publish_committed_playback_owner", AsyncMock()):
            await main.api_spotify_play()

        transfer.assert_awaited_once()
        run.assert_awaited_once()

    async def test_play_desktop_unchanged(self):
        state = self._patched_status(backend="desktop", connect_state="idle")
        transfer = AsyncMock(return_value=True)
        run = AsyncMock(return_value=SimpleNamespace(committed=True, transition_id="tr-d"))
        broadcast = AsyncMock(return_value={"ok": True})
        self._cached(state)
        with mock.patch.object(main, "get_spotify_ui_state", AsyncMock(return_value=state)), \
             mock.patch.object(main, "broadcast_spotify_state", broadcast), \
             mock.patch.object(main, "spotify_transfer_playback", transfer), \
             mock.patch.object(main, "_coordinator_target_rate", lambda *a, **k: 44100), \
             mock.patch.object(main, "_coordinator_rate_change", lambda *a, **k: False), \
             mock.patch.object(main, "_run_coordinated_transition", run), \
             mock.patch.object(main, "_publish_committed_playback_owner", AsyncMock()):
            result = await main.api_spotify_play()

        transfer.assert_not_awaited()
        run.assert_awaited_once()
        self.assertEqual(run.await_args.args[0].operation, "spotify-play")
        self.assertEqual(result, {"ok": True})

    async def test_toggle_ready_holds_without_transition(self):
        state = self._patched_status(connect_state="ready")
        transition = AsyncMock()
        broadcast = AsyncMock(return_value={"ok": True})
        with mock.patch.object(main, "get_spotify_ui_state", AsyncMock(return_value=state)), \
             mock.patch.object(main, "broadcast_spotify_state", broadcast), \
             mock.patch.object(main, "_run_coordinated_transition", transition):
            result = await main.api_spotify_toggle()

        self.assertEqual(result, {"ok": True})
        transition.assert_not_awaited()

    async def test_guard_uses_provider_module_function(self):
        import streaming.spotify.provider as provider_module

        async def fake(*args, **kwargs):
            return True

        with mock.patch.object(provider_module, "_default", return_value=SimpleNamespace(transfer_playback=fake)):
            self.assertTrue(await provider_module.transfer_playback())


if __name__ == "__main__":
    unittest.main()
