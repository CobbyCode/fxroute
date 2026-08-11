#!/usr/bin/env python3
"""Startup probes must be hard-bounded and keep their failure contracts.

Covers:
- EasyEffects flatpak capability probe: missing binary, fast success/failure,
  timeout (no exception propagation), native fallback selection.
- MPV version probe: timeout maps to the controlled player-unavailable
  contract, fast probe keeps the start() contract (faked daemon/socket).
"""

import os
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import player
from easyeffects import EasyEffectsManager

FLATPAK_PROBE = ["flatpak", "info", "com.github.wwmm.easyeffects"]


class FlatpakProbeTests(unittest.TestCase):
    def _manager(self):
        manager = object.__new__(EasyEffectsManager)
        manager.home = pathlib.Path("/tmp")
        return manager

    def test_flatpak_missing_returns_false_without_subprocess(self):
        manager = self._manager()
        with mock.patch("easyeffects.shutil.which", return_value=None), mock.patch(
            "easyeffects.subprocess.run"
        ) as run:
            self.assertFalse(manager._has_flatpak_install())
        run.assert_not_called()

    def test_flatpak_fast_success_returns_true_with_timeout(self):
        manager = self._manager()
        result = subprocess.CompletedProcess(FLATPAK_PROBE, 0, stdout="", stderr="")
        with mock.patch("easyeffects.shutil.which", return_value="/usr/bin/flatpak"), mock.patch(
            "easyeffects.subprocess.run", return_value=result
        ) as run:
            self.assertTrue(manager._has_flatpak_install())
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs.get("timeout"), 2)

    def test_flatpak_fast_nonzero_returns_false(self):
        manager = self._manager()
        result = subprocess.CompletedProcess(FLATPAK_PROBE, 1, stdout="", stderr="")
        with mock.patch("easyeffects.shutil.which", return_value="/usr/bin/flatpak"), mock.patch(
            "easyeffects.subprocess.run", return_value=result
        ):
            self.assertFalse(manager._has_flatpak_install())

    def test_flatpak_timeout_returns_false_without_propagating(self):
        manager = self._manager()
        with mock.patch("easyeffects.shutil.which", return_value="/usr/bin/flatpak"), mock.patch(
            "easyeffects.subprocess.run",
            side_effect=subprocess.TimeoutExpired(FLATPAK_PROBE, 2),
        ):
            self.assertFalse(manager._has_flatpak_install())

    def test_runtime_detection_survives_probe_timeout(self):
        manager = self._manager()

        def fake_which(name):
            if name == "flatpak":
                return "/usr/bin/flatpak"
            if name == "easyeffects":
                return "/usr/bin/easyeffects"
            return None

        with mock.patch("easyeffects.shutil.which", side_effect=fake_which), mock.patch(
            "easyeffects.subprocess.run",
            side_effect=subprocess.TimeoutExpired(FLATPAK_PROBE, 2),
        ):
            runtime = manager._detect_runtime()
        self.assertFalse(runtime.flatpak_available)
        self.assertEqual(runtime.mode, "native")

    def test_flatpak_unavailable_selects_native_runtime(self):
        manager = self._manager()

        def fake_which(name):
            if name == "flatpak":
                return None
            if name == "easyeffects":
                return "/usr/bin/easyeffects"
            return None

        with mock.patch("easyeffects.shutil.which", side_effect=fake_which):
            runtime = manager._detect_runtime()
        self.assertEqual(runtime.mode, "native")
        self.assertFalse(runtime.flatpak_available)
        self.assertTrue(runtime.native_available)


class MPVVersionProbeTests(unittest.TestCase):
    def test_version_probe_timeout_raises_mpv_not_installed_without_daemon(self):
        wrapper = player.MPVWrapper()
        with mock.patch(
            "player.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["mpv", "--version"], 2),
        ), mock.patch("player.subprocess.Popen") as popen:
            with self.assertRaisesRegex(player.MPVNotInstalledError, "version probe timed out"):
                wrapper.start()
        popen.assert_not_called()
        self.assertFalse(wrapper._running)

    def test_fast_probe_keeps_start_contract_with_faked_daemon(self):
        wrapper = player.MPVWrapper()
        fake_process = mock.MagicMock()
        fake_process.poll.return_value = None
        socket_file = wrapper.socket_path

        def fake_popen(*args, **kwargs):
            pathlib.Path(socket_file).write_text("")
            return fake_process

        result = subprocess.CompletedProcess(["mpv", "--version"], 0, stdout="", stderr="")
        try:
            with mock.patch("player.subprocess.run", return_value=result), mock.patch(
                "player.subprocess.Popen", side_effect=fake_popen
            ):
                wrapper.start()
            self.assertTrue(wrapper._running)
            self.assertIs(wrapper.process, fake_process)
        finally:
            wrapper.stop()
            try:
                os.unlink(socket_file)
            except FileNotFoundError:
                pass
            self.assertIsNone(wrapper.process)


if __name__ == "__main__":
    unittest.main(verbosity=2)
