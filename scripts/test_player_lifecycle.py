#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
#
# MPV lifecycle regressions: the FXRoute mpv invocation must never
# autoconnect to the hardware sink, and orphan FXRoute mpv processes from
# killed service runs must be cleaned up on the next start without ever
# touching unrelated user mpv processes.

import os
import signal
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playback.mpv_process import (
    _fxroute_mpv_pids,
    _is_fxroute_mpv_cmdline,
    _stop_orphan_mpv_processes,
)
from playback.player import (
    MPVDisabledError,
    MPVError,
    MPVNotInstalledError,
    MPVWrapper,
)

SOCKET = "/tmp/mpv.sock"
FXROUTE_CMDLINE = (
    "mpv --idle=yes --input-ipc-server=/tmp/mpv.sock --no-video --quiet "
    "--network-timeout=15 --audio-device=pipewire/fxroute_dsp_sink "
    "--stream-lavf-o=reconnect=1"
)


class MPVCmdlineTests(unittest.TestCase):
    def test_fxroute_mpv_command_pins_audio_to_the_ingress_sink(self):
        command = MPVWrapper._mpv_command(SOCKET)
        self.assertIn("--audio-device=pipewire/fxroute_dsp_sink", command)
        self.assertEqual(command[0], "mpv")
        self.assertIn("--idle=yes", command)
        self.assertIn("--input-ipc-server=" + SOCKET, command)

    def test_fxroute_cmdline_marker_matches_own_process(self):
        self.assertTrue(_is_fxroute_mpv_cmdline(FXROUTE_CMDLINE, SOCKET))

    def test_unrelated_mpv_processes_never_match(self):
        foreign = [
            "mpv --idle=yes --input-ipc-server=/tmp/other.sock --no-video",
            "mpv /home/user/music/track.flac --no-video",
            "mpv --idle=yes",
            "vlc --intf dummy",
        ]
        for cmdline in foreign:
            self.assertFalse(
                _is_fxroute_mpv_cmdline(cmdline, SOCKET),
                f"foreign cmdline matched: {cmdline}",
            )

    def test_fxroute_cmdline_with_different_socket_does_not_match(self):
        self.assertFalse(_is_fxroute_mpv_cmdline(FXROUTE_CMDLINE, "/tmp/other.sock"))

    def test_pid_scan_returns_fxroute_mpv_pids_only(self):
        with unittest.mock.patch("playback.mpv_process.os.listdir", return_value=["1", "2", "3", "abc"]):
            with unittest.mock.patch("playback.mpv_process.open") as mock_open:
                def fake_open(path, *_args, **_kwargs):
                    handle = unittest.mock.MagicMock()
                    contents = {
                        "/proc/1/cmdline": b"mpv\x00--idle=yes\x00--input-ipc-server=/tmp/mpv.sock\x00--no-video",
                        "/proc/2/cmdline": b"mpv\x00/home/user/song.mp3\x00--no-video",
                        "/proc/3/cmdline": b"systemd\x00--user",
                    }
                    handle.read.return_value = contents.get(str(path), b"")
                    handle.__enter__.return_value = handle
                    return handle
                mock_open.side_effect = fake_open
                self.assertEqual(_fxroute_mpv_pids(SOCKET), [1])

    def test_orphan_cleanup_terminates_then_kills_survivors(self):
        signalled = []
        with unittest.mock.patch("playback.mpv_process._fxroute_mpv_pids",
                                 side_effect=lambda _socket: [1, 2] if not signalled else [2]):
            with unittest.mock.patch("playback.mpv_process.os.kill") as mock_kill:
                with unittest.mock.patch(
                    "playback.mpv_process._ORPHAN_SIGTERM_GRACE_SECONDS", 0.01
                ):
                    def recording_kill(pid, sig):
                        signalled.append((pid, sig))
                        if sig == signal.SIGTERM and pid == 1:
                            raise ProcessLookupError(pid)
                    mock_kill.side_effect = recording_kill
                    _stop_orphan_mpv_processes(SOCKET, own_pid=None)
        self.assertIn((1, signal.SIGTERM), signalled)
        self.assertIn((2, signal.SIGTERM), signalled)
        self.assertIn((2, signal.SIGKILL), signalled)
        self.assertNotIn((1, signal.SIGKILL), signalled)

    def test_orphan_cleanup_never_touches_own_process(self):
        signalled = []
        with unittest.mock.patch("playback.mpv_process._fxroute_mpv_pids", return_value=[7, 8]):
            with unittest.mock.patch("playback.mpv_process.os.kill") as mock_kill:
                with unittest.mock.patch(
                    "playback.mpv_process._ORPHAN_SIGTERM_GRACE_SECONDS", 0.01
                ):
                    mock_kill.side_effect = lambda pid, sig: signalled.append((pid, sig))
                    _stop_orphan_mpv_processes(SOCKET, own_pid=7)
        self.assertEqual(signalled, [(8, signal.SIGTERM), (8, signal.SIGKILL)])

    def test_default_socket_and_enabled_unchanged(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MPV_SOCKET_PATH", None)
            os.environ.pop("MPV_ENABLED", None)
            wrapper = MPVWrapper()
        self.assertEqual(wrapper.socket_path, "/tmp/mpv.sock")

    def test_socket_path_env_override_feeds_ipc_and_cleanup(self):
        with unittest.mock.patch.dict(os.environ, {"MPV_SOCKET_PATH": "/tmp/view-mpv.sock"}):
            wrapper = MPVWrapper()
            self.assertEqual(wrapper.socket_path, "/tmp/view-mpv.sock")
            with unittest.mock.patch("playback.player._stop_orphan_mpv_processes") as cleanup:
                with unittest.mock.patch("playback.player.subprocess.run"):
                    with unittest.mock.patch("playback.player.subprocess.Popen") as popen:
                        with unittest.mock.patch("playback.player.os.path.exists", return_value=True):
                            with unittest.mock.patch("playback.player.threading.Thread"):
                                wrapper.start()
        self.assertEqual(cleanup.call_args.args[0], "/tmp/view-mpv.sock")
        command = popen.call_args.args[0]
        self.assertIn("--input-ipc-server=/tmp/view-mpv.sock", command)
        self.assertIn("--audio-device=pipewire/fxroute_dsp_sink", command)
        self.assertTrue(wrapper._running)

    def test_mpv_disabled_raises_and_never_touches_processes(self):
        with unittest.mock.patch.dict(os.environ, {"MPV_ENABLED": "0"}):
            wrapper = MPVWrapper()
            with unittest.mock.patch("playback.player._stop_orphan_mpv_processes") as cleanup:
                with unittest.mock.patch("playback.player.subprocess.run") as probe:
                    with self.assertRaises(MPVDisabledError):
                        wrapper.start()
        cleanup.assert_not_called()
        probe.assert_not_called()
        self.assertFalse(wrapper._running)

    def test_mpv_disabled_error_is_reported_as_not_installed(self):
        self.assertTrue(issubclass(MPVDisabledError, MPVNotInstalledError))

    def test_mpv_disabled_parsing_matches_off_like_values(self):
        for value in ("0", "false", "no", "off", "OFF", "False"):
            with self.subTest(value=value):
                with unittest.mock.patch.dict(os.environ, {"MPV_ENABLED": value}):
                    wrapper = MPVWrapper()
                    with unittest.mock.patch("playback.player.subprocess.run") as probe:
                        with self.assertRaises(MPVDisabledError):
                            wrapper.start()
                    probe.assert_not_called()
        for value in ("1", "true", "yes", "on", ""):
            with self.subTest(value=value):
                with unittest.mock.patch.dict(os.environ, {"MPV_ENABLED": value}):
                    wrapper = MPVWrapper()
                    with unittest.mock.patch("playback.player.subprocess.run", side_effect=AssertionError):
                        with self.assertRaises(AssertionError):
                            wrapper.start()

    def test_orphan_cleanup_noop_when_no_orphans(self):
        with unittest.mock.patch("playback.mpv_process._fxroute_mpv_pids", return_value=[]):
            with unittest.mock.patch("playback.mpv_process.os.kill") as mock_kill:
                _stop_orphan_mpv_processes(SOCKET)
        mock_kill.assert_not_called()


class MPVLivenessTests(unittest.TestCase):
    """A crashed mpv must not keep reporting itself as available.

    Regression: mpv died on its own, the wrapper still had _running set from
    the last successful start(), and every IPC call then failed on a missing
    socket. The output-state transition quiets MPV before switching the
    output mode, so a dead player failed the whole apply with HTTP 500
    instead of simply having nothing to quiet.
    """

    @staticmethod
    def _wrapper(running: bool, returncode=None) -> MPVWrapper:
        wrapper = MPVWrapper()
        wrapper._running = running
        process = unittest.mock.Mock()
        # poll() reaps the exited child; None means the process is alive.
        process.poll.return_value = returncode
        process.returncode = returncode
        wrapper.process = process
        return wrapper

    def test_live_process_reports_running(self):
        wrapper = self._wrapper(running=True, returncode=None)
        self.assertTrue(wrapper.is_running())
        self.assertTrue(wrapper._running)

    def test_exited_process_reports_not_running_and_clears_state(self):
        wrapper = self._wrapper(running=True, returncode=1)
        self.assertFalse(wrapper.is_running())
        self.assertFalse(wrapper._running, "an exited process must not leave _running set")
        self.assertTrue(wrapper._listener_stop_event.is_set())

    def test_missing_process_reports_not_running(self):
        wrapper = MPVWrapper()
        wrapper._running = True
        wrapper.process = None
        self.assertFalse(wrapper.is_running())

    def test_never_started_reports_not_running(self):
        self.assertFalse(MPVWrapper().is_running())

    def test_exited_process_is_reaped_so_it_cannot_linger_as_a_zombie(self):
        wrapper = self._wrapper(running=True, returncode=1)
        wrapper.is_running()
        wrapper.process.poll.assert_called()

    def test_send_command_reports_not_running_for_an_exited_process(self):
        """The IPC path must fail with the clean MPVError, not a socket error."""
        wrapper = self._wrapper(running=True, returncode=1)
        with self.assertRaises(MPVError) as caught:
            wrapper.set_volume(0)
        self.assertIn("not running", str(caught.exception))


class CrashedPlayerApiGuardTests(unittest.IsolatedAsyncioTestCase):
    """Every transport guard must follow the live mpv process.

    Regression: ``pkill -9`` of mpv left ``player_instance._running`` True, so
    /api/stop passed its guard and died inside the IPC send, surfacing as an
    unhandled MPVError (HTTP 500).  The documented contract for an unavailable
    player is a clean 503.
    """

    class CrashedPlayer:
        _running = True
        state = {"current_file": "/music/current.flac", "playing": True, "paused": False}

        def is_running(self):
            return False

        def stop_playback(self):
            raise AssertionError("transport reached a crashed mpv")

        def pause(self):
            raise AssertionError("transport reached a crashed mpv")

        def set_volume(self, _volume):
            raise AssertionError("transport reached a crashed mpv")

    async def test_transport_endpoints_report_503_for_a_crashed_player(self):
        import main
        from fastapi import HTTPException

        player = self.CrashedPlayer()
        cases = (
            ("pause", lambda: main.pause_playback()),
            ("stop", lambda: main.stop_playback()),
            ("clear-queue", lambda: main.clear_playback_queue()),
            ("volume", lambda: main.set_volume(None)),
        )
        with unittest.mock.patch.object(main.runtime, "player_instance", player):
            for name, call in cases:
                with self.subTest(endpoint=name):
                    with self.assertRaises(HTTPException) as caught:
                        await call()
                    self.assertEqual(caught.exception.status_code, 503)
                    self.assertEqual(caught.exception.detail, "Player not available")

    async def test_play_reports_503_when_the_player_cannot_be_restarted(self):
        import main
        from fastapi import HTTPException

        player = self.CrashedPlayer()
        with unittest.mock.patch.object(main.runtime, "player_instance", player), unittest.mock.patch.object(
            main, "_ensure_player_running", return_value=False
        ):
            with self.assertRaises(HTTPException) as caught:
                await main.play_track(main.PlayRequest(source="local", track_id="1"))
        self.assertEqual(caught.exception.status_code, 503)


class EnsurePlayerRunningTests(unittest.TestCase):
    """A crashed player must be restarted before a transport press fails.

    ``_running`` only records the last successful start(); the restart check
    must use the same live probe as the API guards.
    """

    class _Player:
        def __init__(self, *, intent: bool, alive: bool):
            self._running = intent
            self.alive = alive
            self.started = 0

        def is_running(self):
            return self.alive

        def start(self):
            self.started += 1
            self._running = True
            self.alive = True

    def test_stale_start_intent_for_a_dead_process_is_restarted(self):
        import main

        player = self._Player(intent=True, alive=False)
        with unittest.mock.patch.object(main.runtime, "player_instance", player), unittest.mock.patch.object(
            main, "_player_restart_cooldown_until", 0.0
        ):
            self.assertTrue(main._ensure_player_running())
        self.assertEqual(player.started, 1)

    def test_live_process_is_not_restarted(self):
        import main

        player = self._Player(intent=True, alive=True)
        with unittest.mock.patch.object(main.runtime, "player_instance", player), unittest.mock.patch.object(
            main, "_player_restart_cooldown_until", 0.0
        ):
            self.assertTrue(main._ensure_player_running())
        self.assertEqual(player.started, 0)

    def test_start_failure_arms_the_restart_cooldown(self):
        import main

        class Player:
            _running = False

            def is_running(self):
                return False

            def start(self):
                raise RuntimeError("mpv binary missing")

        with unittest.mock.patch.object(main.runtime, "player_instance", Player()), unittest.mock.patch.object(
            main, "_player_restart_cooldown_until", 0.0
        ):
            self.assertFalse(main._ensure_player_running())
            # The cooldown is armed: the next press must not probe again.
            self.assertFalse(main._ensure_player_running())


class RestartedPlayerStateTests(unittest.TestCase):
    """A restarted mpv must not inherit the dead process's file.

    Regression: ``/api/play`` after a crash saw the stale ``current_file``,
    took the same-target fast path (``reload_source=False``) and failed the
    target-source-prepare stage with "target source to DSP links were not
    confirmed" while the fresh mpv had nothing loaded.
    """

    def _start_with_mocks(self, wrapper):
        with unittest.mock.patch("playback.player._stop_orphan_mpv_processes"), unittest.mock.patch(
            "playback.player.subprocess.run"
        ), unittest.mock.patch("playback.player.subprocess.Popen"), unittest.mock.patch(
            "playback.player.os.path.exists", return_value=True
        ), unittest.mock.patch("playback.player.threading.Thread"):
            wrapper.start()

    def test_restart_forgets_the_dead_process_file_state(self):
        wrapper = MPVWrapper()
        wrapper._running = False
        wrapper._state.update(current_file="/music/old.flac", playing=True, duration=300.0)
        wrapper._state_snapshot = wrapper._state.copy()

        self._start_with_mocks(wrapper)

        self.assertIsNone(wrapper.state["current_file"])
        self.assertFalse(wrapper.state["playing"])
        self.assertFalse(wrapper.state["file_loaded"])

    def test_initial_start_keeps_the_empty_state(self):
        wrapper = MPVWrapper()
        self._start_with_mocks(wrapper)
        self.assertIsNone(wrapper.state["current_file"])
        self.assertTrue(wrapper._running)


class PlayerAvailabilityAdapterTests(unittest.TestCase):
    """main._player_is_running must follow the real player, not stale state."""

    def test_adapter_prefers_the_live_check_over_the_start_intent(self):
        import main

        class CrashedPlayer:
            _running = True

            def is_running(self):
                return False

        self.assertFalse(main._player_is_running(CrashedPlayer()))

    def test_adapter_keeps_working_with_doubles_that_only_expose_running(self):
        import main

        class LegacyDouble:
            _running = True

        self.assertTrue(main._player_is_running(LegacyDouble()))
        self.assertFalse(main._player_is_running(type("D", (), {"_running": False})()))

    def test_adapter_reports_a_missing_player_as_unavailable(self):
        import main

        # runtime.player_instance is the real player on the host; patch it so
        # the assertion does not depend on whether mpv currently runs.
        with unittest.mock.patch.object(main.runtime, "player_instance", None):
            self.assertFalse(main._player_is_running())


if __name__ == "__main__":
    unittest.main()
