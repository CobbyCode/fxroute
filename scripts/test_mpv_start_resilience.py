#!/usr/bin/env python3
"""Regression tests: transient MPV start failures must not disable playback forever.

Live-session evidence (2026-09-18): `mpv --version` timed out once at backend
startup (first-run cache generation on a slow live medium while the DSP engine
was compiling; the same probe took 0.16s minutes later). The backend logged
"Failed to start MPV" and every /api/play returned 503 until a manual restart.
"""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from playback import player
from playback.player import MPVNotInstalledError, MPVWrapper


def _start_with_mocks(run_side_effect):
    """Run MPVWrapper.start with process/socket/threading faked. Returns (wrapper, runs)."""
    effects = run_side_effect if isinstance(run_side_effect, list) else [run_side_effect]
    runs = []

    def fake_run(*args, **kwargs):
        runs.append(kwargs.get("timeout"))
        effect = effects[len(runs) - 1]
        if isinstance(effect, BaseException):
            raise effect
        return mock.Mock(returncode=0)

    wrapper = MPVWrapper()
    with mock.patch.object(player.subprocess, "run", side_effect=fake_run), \
            mock.patch.object(player.subprocess, "Popen", return_value=mock.Mock()), \
            mock.patch("os.path.exists", return_value=True), \
            mock.patch("os.unlink", side_effect=FileNotFoundError), \
            mock.patch.object(player.threading, "Thread", return_value=mock.Mock()), \
            mock.patch.object(player, "_stop_orphan_mpv_processes", return_value=None):
        wrapper.start()
    return wrapper, runs


class MPVStartResilienceTests(unittest.TestCase):
    def test_version_probe_timeout_is_generous(self):
        _, runs = _start_with_mocks([None])
        self.assertEqual(len(runs), 1)
        self.assertGreaterEqual(runs[0], 60)

    def test_version_probe_retries_then_starts(self):
        err = subprocess.TimeoutExpired(cmd=["mpv", "--version"], timeout=1)
        wrapper, runs = _start_with_mocks([err, err, None])
        self.assertEqual(len(runs), 3)
        self.assertTrue(wrapper._running)

    def test_version_probe_fails_closed_after_retries(self):
        err = subprocess.TimeoutExpired(cmd=["mpv", "--version"], timeout=1)
        with self.assertRaises(MPVNotInstalledError):
            _start_with_mocks([err, err, err, err])
        with self.assertRaises(MPVNotInstalledError):
            _start_with_mocks(FileNotFoundError("no mpv"))

    def test_missing_binary_still_fails_fast_without_retries(self):
        with self.assertRaises(MPVNotInstalledError):
            _start_with_mocks([FileNotFoundError("no mpv")])


class EnsurePlayerRunningTests(unittest.TestCase):
    def setUp(self):
        self._instance = main.runtime.player_instance
        self._cooldown = main._player_restart_cooldown_until
        main._player_restart_cooldown_until = 0.0
        self.addCleanup(setattr, main.runtime, "player_instance", self._instance)
        self.addCleanup(setattr, main, "_player_restart_cooldown_until", self._cooldown)

    def test_running_player_needs_no_restart(self):
        running = mock.Mock(_running=True)
        main.runtime.player_instance = running
        with mock.patch.object(main, "get_player") as get_player:
            self.assertTrue(main._ensure_player_running())
            get_player.assert_not_called()

    def test_missing_player_is_started(self):
        main.runtime.player_instance = None
        fresh = mock.Mock(_running=True)
        with mock.patch.object(main, "get_player", return_value=fresh):
            self.assertTrue(main._ensure_player_running())
            fresh.start.assert_called_once()
        self.assertIs(main.runtime.player_instance, fresh)

    def test_failed_restart_returns_false_and_cools_down(self):
        main.runtime.player_instance = None
        fresh = mock.Mock(_running=False)
        fresh.start.side_effect = MPVNotInstalledError("no mpv")
        with mock.patch.object(main, "get_player", return_value=fresh):
            self.assertFalse(main._ensure_player_running())
            fresh.start.reset_mock()
            self.assertFalse(main._ensure_player_running())
            fresh.start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
