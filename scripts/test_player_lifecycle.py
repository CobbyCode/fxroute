#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
#
# MPV lifecycle regressions: the FXRoute mpv invocation must never
# autoconnect to the hardware sink, and orphan FXRoute mpv processes from
# killed service runs must be cleaned up on the next start without ever
# touching unrelated user mpv processes.

import signal
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from player import (MPVWrapper, _fxroute_mpv_pids, _is_fxroute_mpv_cmdline,
                    _stop_orphan_mpv_processes)

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
        with unittest.mock.patch("player.os.listdir", return_value=["1", "2", "3", "abc"]):
            with unittest.mock.patch("player.open") as mock_open:
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
        with unittest.mock.patch("player._fxroute_mpv_pids",
                                 side_effect=lambda _socket: [1, 2] if not signalled else [2]):
            with unittest.mock.patch("player.os.kill") as mock_kill:
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
        with unittest.mock.patch("player._fxroute_mpv_pids", return_value=[7, 8]):
            with unittest.mock.patch("player.os.kill") as mock_kill:
                mock_kill.side_effect = lambda pid, sig: signalled.append((pid, sig))
                _stop_orphan_mpv_processes(SOCKET, own_pid=7)
        self.assertEqual(signalled, [(8, signal.SIGTERM), (8, signal.SIGKILL)])

    def test_orphan_cleanup_noop_when_no_orphans(self):
        with unittest.mock.patch("player._fxroute_mpv_pids", return_value=[]):
            with unittest.mock.patch("player.os.kill") as mock_kill:
                _stop_orphan_mpv_processes(SOCKET)
        mock_kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
