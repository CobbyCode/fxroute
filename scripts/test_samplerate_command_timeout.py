#!/usr/bin/env python3
"""audio/samplerate._run_command must be bounded against hung external commands."""

import pathlib
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import audio.samplerate as samplerate


class SamplerateCommandTimeoutTests(unittest.TestCase):
    def test_successful_command_passes_timeout(self):
        result = subprocess.CompletedProcess([], 0, stdout="42\n", stderr="")
        with mock.patch("audio.samplerate.parsing.subprocess.run", return_value=result) as run:
            self.assertEqual(samplerate._run_command(["wpctl", "status"]), "42\n")
        run.assert_called_once()
        self.assertEqual(
            run.call_args.kwargs.get("timeout"), samplerate.COMMAND_TIMEOUT_SECONDS
        )

    def test_nonzero_exit_raises_runtime_error(self):
        result = subprocess.CompletedProcess([], 1, stdout="", stderr="boom")
        with mock.patch("audio.samplerate.parsing.subprocess.run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                samplerate._run_command(["pactl", "info"])

    def test_hung_command_raises_timeout_error(self):
        with mock.patch(
            "audio.samplerate.parsing.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["bluetoothctl", "info"], 5.0),
        ):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                samplerate._run_command(["bluetoothctl", "info", "AA:BB:CC"])

    def test_bluetooth_overview_skips_device_queries_when_daemon_unreachable(self):
        # On hosts with bluetoothctl installed but no reachable BlueZ daemon,
        # bluetoothctl calls block until the command timeout. The overview
        # must probe the D-Bus service first and skip all bluetoothctl
        # queries when it is not there, keeping the audio overviews fast.
        import audio.samplerate.bluetooth as bt

        def fake_run(cmd):
            if cmd[0] == "dbus-send":
                raise RuntimeError("NameHasNoOwner")
            raise AssertionError(f"unexpected command: {cmd}")

        with mock.patch.object(bt, "_command_available", return_value=True), \
            mock.patch.object(bt, "_run_command", side_effect=fake_run), \
            mock.patch.object(bt, "_load_audio_source_selection", return_value={"mode": "app_playback"}), \
            mock.patch.object(bt, "_pipewire_bluez_plugin_available", return_value=False):
            overview = bt.get_bluetooth_audio_overview()
        self.assertFalse(overview.get("devices"))
        self.assertTrue(
            any("not reachable" in n for n in overview.get("notes") or [])
        )

    def test_bluetooth_overview_queries_devices_when_daemon_reachable(self):
        import audio.samplerate.bluetooth as bt

        calls = []

        def fake_run(cmd):
            calls.append(cmd)
            if cmd[0] == "dbus-send":
                return "method return sender=org.freedesktop.DBus -> dest=:1.2\n"
            if cmd[:2] == ["bluetoothctl", "show"]:
                return "Controller AA:BB:CC:DD:EE:FF hostname alias\n"
            if cmd == ["bluetoothctl", "devices", "Paired"]:
                return "Device AA:BB:CC:DD:EE:FF Some Device\n"
            if cmd[:2] == ["bluetoothctl", "devices"]:
                return ""
            raise AssertionError(f"unexpected command: {cmd}")

        with mock.patch.object(bt, "_command_available", return_value=True), \
            mock.patch.object(bt, "_run_command", side_effect=fake_run), \
            mock.patch.object(bt, "_load_audio_source_selection", return_value={"mode": "app_playback"}), \
            mock.patch.object(bt, "_pipewire_bluez_plugin_available", return_value=False):
            overview = bt.get_bluetooth_audio_overview()
        self.assertTrue(any("AA:BB:CC:DD:EE:FF" in str(d.get("address")) for d in overview.get("devices") or []))
        self.assertTrue(any(c[:2] == ["bluetoothctl", "devices"] for c in calls))

    def test_bluetooth_receiver_and_disconnect_skip_when_daemon_unreachable(self):
        # The shutdown cleanup disables the Bluetooth receiver and disconnects
        # audio sources; with no reachable BlueZ daemon those calls used to
        # block until the command timeout, emitting errors and stalling the
        # service stop. Both must become no-ops when the daemon is down.
        import audio.samplerate.bluetooth as bt

        def fake_run(cmd):
            if cmd[0] == "dbus-send":
                raise RuntimeError("NameHasNoOwner")
            raise AssertionError(f"unexpected command: {cmd}")

        with mock.patch.object(bt, "_command_available", return_value=True), \
            mock.patch.object(bt, "_run_command", side_effect=fake_run), \
            mock.patch.object(bt, "_load_audio_source_selection", return_value={"mode": "app_playback"}), \
            mock.patch.object(bt, "_pipewire_bluez_plugin_available", return_value=False):
            self.assertEqual(bt.disconnect_connected_bluetooth_audio_sources(), [])
            overview = bt.set_bluetooth_receiver_enabled(False)
        self.assertEqual(overview.get("devices"), [])

    def test_error_fallback_paths_still_work(self):
        # The fallback helpers parse failures into notes instead of raising.
        self.assertEqual(samplerate._parse_active_rate(""), None)
        self.assertEqual(
            samplerate._parse_default_sink(""),
            {"id": None, "name": None, "description": None},
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
