#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Bluetooth input comes back by itself after leaving the mode.

Selecting Bluetooth input kicks the connected A2DP source (the peer keeps
playing to its own speakers). Selecting the mode again therefore has to
reconnect that device without a manual ``bluetoothctl connect``:

- Leaving the mode remembers the devices it disconnected (and the device a
  stream was linked for), and entering it again records a reconnect that runs
  off the source-transition lock, after the receiver toggle.
- The reconnect is bounded and spaced out, so a peer that is switched off is
  not probed by bluetoothctl on every 3 s monitor tick; a peer that streams
  again retires the retry budget and drops a recorded reconnect.
- A quick toggle (leaving and entering before the recorded kick ran) drops the
  kick instead of disconnecting a device the caller just re-selected.
- Only paired/trusted devices are connected; an already-connected one counts
  as reconnected instead of being connected again.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audio.bluetooth as bluetooth_module
import audio.samplerate.bluetooth as bluez_mod
from audio.bluetooth import BluetoothInputDependencies, BluetoothInputMonitor

ADDRESS = "84:9E:56:57:19:94"
SOURCE_NAME = "bluez_input.84_9E_56_57_19_94.2"


def _info(connected=False, paired=True, trusted=True) -> str:
    return "\n".join([
        f"Device {ADDRESS} (public)",
        "	Name: ZENBOOK",
        f"	Paired: {'yes' if paired else 'no'}",
        f"	Trusted: {'yes' if trusted else 'no'}",
        f"	Connected: {'yes' if connected else 'no'}",
    ])


class ReconnectHelperTests(unittest.TestCase):
    """reconnect_bluetooth_audio_devices connects exactly what it should."""

    def _run(self, results, addresses):
        calls = []

        def run(args, timeout=None):
            calls.append(list(args))
            command = args[1] if len(args) > 1 else ""
            if command == "info":
                return results.get(args[2], _info())
            return ""

        with patch.object(bluez_mod, "_command_available", return_value=True), patch.object(
            bluez_mod, "_bluetooth_daemon_reachable", return_value=True
        ), patch.object(bluez_mod, "_run_command", side_effect=run):
            return bluez_mod.reconnect_bluetooth_audio_devices(addresses), calls

    def test_connects_a_paired_disconnected_device(self):
        connected, calls = self._run({ADDRESS: _info()}, [ADDRESS])
        self.assertEqual(connected, [ADDRESS])
        self.assertEqual([call[1] for call in calls], ["info", "connect"])

    def test_an_already_connected_device_is_not_connected_again(self):
        connected, calls = self._run({ADDRESS: _info(connected=True)}, [ADDRESS])
        self.assertEqual(connected, [ADDRESS])
        self.assertEqual([call[1] for call in calls], ["info"])

    def test_an_unpaired_device_is_never_paired_implicitly(self):
        connected, calls = self._run(
            {ADDRESS: _info(paired=False, trusted=False)}, [ADDRESS])
        self.assertEqual((connected, [call[1] for call in calls]), ([], ["info"]))

    def test_addresses_are_normalized_and_deduplicated(self):
        connected, calls = self._run({ADDRESS: _info()},
                                     [ADDRESS.lower().replace(":", "_"), ADDRESS])
        self.assertEqual(connected, [ADDRESS])
        self.assertEqual(len([call for call in calls if call[1] == "connect"]), 1)

    def test_a_missing_bluez_daemon_reconnects_nothing(self):
        with patch.object(bluez_mod, "_command_available", return_value=True), patch.object(
            bluez_mod, "_bluetooth_daemon_reachable", return_value=False
        ), patch.object(bluez_mod, "_run_command") as run:
            self.assertEqual(bluez_mod.reconnect_bluetooth_audio_devices([ADDRESS]), [])
        run.assert_not_called()

    def test_all_failed_attempts_raise(self):
        def run(args, timeout=None):
            raise RuntimeError("connect failed")

        with patch.object(bluez_mod, "_command_available", return_value=True), patch.object(
            bluez_mod, "_bluetooth_daemon_reachable", return_value=True
        ), patch.object(bluez_mod, "_run_command", side_effect=run):
            with self.assertRaises(RuntimeError):
                bluez_mod.reconnect_bluetooth_audio_devices([ADDRESS])


class ReentryTests(unittest.IsolatedAsyncioTestCase):
    """Leaving and re-selecting Bluetooth input brings the device back."""

    def _monitor(self, persisted="bluetooth-input"):
        return BluetoothInputMonitor(BluetoothInputDependencies(
            sync_peak_monitor_for_source_mode_state=AsyncMock(),
            get_persisted_source_mode=lambda: persisted,
        ))

    async def test_reentering_reconnects_the_kicked_device(self):
        """The regression: leaving and selecting again must not need a host connect."""
        monitor = self._monitor()
        reconnects = []
        with patch.object(monitor, "_ensure_agent", AsyncMock()), patch.object(
            bluetooth_module, "input_links_present", AsyncMock(return_value=True)
        ), patch.object(bluetooth_module.pw_link, "connect_ports", AsyncMock()), patch.object(
            bluetooth_module.pw_link, "disconnect_ports", AsyncMock()
        ), patch.object(bluetooth_module, "disconnect_connected_bluetooth_audio_sources",
                        return_value=[ADDRESS.lower().replace(":", "_")]), patch.object(
            bluetooth_module, "reconnect_bluetooth_audio_devices",
            side_effect=lambda addresses: reconnects.append(list(addresses)) or list(addresses)
        ), patch.object(bluetooth_module, "set_bluetooth_receiver_enabled") as receiver:
            await monitor.sync({"mode": "bluetooth-input",
                                "bluetooth": {"selectable": True, "discoverable": True,
                                              "pairable": True, "source_name": SOURCE_NAME}})
            await monitor.sync({"mode": "app-playback"})
            await monitor.finish_bluetoothctl_actions()
            self.assertEqual(reconnects, [], "leaving Bluetooth input must not reconnect")
            self.assertEqual(monitor._reconnect_addresses, [ADDRESS])

            await monitor.sync({"mode": "bluetooth-input",
                                "bluetooth": {"selectable": True, "discoverable": True,
                                              "pairable": True}})
            await monitor.finish_bluetoothctl_actions()
        self.assertEqual(reconnects, [[ADDRESS]], "re-entering must reconnect the kicked device")
        self.assertEqual(monitor._reconnect_addresses, [], "a connected device retires its retry")
        self.assertEqual(receiver.call_args_list[-1].args, (False,),
                         "an already advertising adapter is not toggled again")

    async def test_a_streamed_device_is_reconnected_after_leaving(self):
        """A peer that dropped on its own is remembered from its stream."""
        monitor = self._monitor()
        reconnects = []
        with patch.object(monitor, "_ensure_agent", AsyncMock()), patch.object(
            bluetooth_module, "input_links_present", AsyncMock(return_value=True)
        ), patch.object(bluetooth_module.pw_link, "connect_ports", AsyncMock()), patch.object(
            bluetooth_module.pw_link, "disconnect_ports", AsyncMock()
        ), patch.object(bluetooth_module, "disconnect_connected_bluetooth_audio_sources",
                        return_value=[]), patch.object(
            bluetooth_module, "reconnect_bluetooth_audio_devices",
            side_effect=lambda addresses: reconnects.append(list(addresses)) or []):
            await monitor.sync({"mode": "bluetooth-input",
                                "bluetooth": {"selectable": True, "discoverable": True,
                                              "pairable": True, "source_name": SOURCE_NAME}})
            self.assertEqual(monitor._reconnect_addresses, [ADDRESS])
            await monitor.sync({"mode": "app-playback"})
            await monitor.finish_bluetoothctl_actions()
            await monitor.sync({"mode": "bluetooth-input",
                                "bluetooth": {"selectable": True, "discoverable": True,
                                              "pairable": True}})
            await monitor.finish_bluetoothctl_actions()
        self.assertEqual(reconnects, [[ADDRESS]])

    async def test_a_quick_toggle_drops_the_kick_and_reconnects(self):
        """Entering again before the recorded kick ran must not disconnect."""
        started, reconnects = [], []
        monitor = self._monitor()

        def disconnected_once():
            started.append(1)
            return [ADDRESS]

        with patch.object(monitor, "_ensure_agent", AsyncMock()), patch.object(
            bluetooth_module, "input_links_present", AsyncMock(return_value=True)
        ), patch.object(bluetooth_module.pw_link, "disconnect_ports", AsyncMock()), patch.object(
            bluetooth_module.pw_link, "connect_ports", AsyncMock()
        ), patch.object(
            bluetooth_module, "disconnect_connected_bluetooth_audio_sources",
            side_effect=disconnected_once
        ), patch.object(bluetooth_module, "reconnect_bluetooth_audio_devices",
                        side_effect=lambda addresses: reconnects.append(list(addresses)) or []), patch.object(
            bluetooth_module, "set_bluetooth_receiver_enabled") as receiver:
            # The device streamed before, so it is a reconnect target.
            await monitor.sync({"mode": "bluetooth-input",
                                "bluetooth": {"selectable": True, "discoverable": True,
                                              "pairable": True, "source_name": SOURCE_NAME}})
            await monitor.sync({"mode": "app-playback"})
            self.assertTrue(monitor._pending_device_kick)
            # Re-selecting drops the recorded kick and asks for a reconnect;
            # the adapter is off again, so the receiver enable is recorded too.
            await monitor.sync({"mode": "bluetooth-input",
                                "bluetooth": {"selectable": True, "discoverable": False,
                                              "pairable": False}})
            await monitor.finish_bluetoothctl_actions()
        self.assertEqual(started, [], "a re-selected device must not be disconnected")
        self.assertEqual(reconnects, [[ADDRESS]])
        self.assertEqual(receiver.call_args_list[-1].args, (True,))

    async def test_reconnect_attempts_are_bounded_and_spaced(self):
        """A peer that stays away is not probed on every monitor tick."""
        attempts = []
        monitor = self._monitor()
        monitor._reconnect_addresses = [ADDRESS]
        with patch.object(bluetooth_module, "BLUETOOTH_RECONNECT_INTERVAL_SECONDS", 0.0), patch.object(
            bluetooth_module, "reconnect_bluetooth_audio_devices",
            side_effect=lambda addresses: attempts.append(list(addresses)) or []
        ), patch.object(bluetooth_module.time, "monotonic", return_value=1000.0):
            for _ in range(bluetooth_module.BLUETOOTH_RECONNECT_ATTEMPTS + 3):
                monitor._record_reconnect_attempt()
                await monitor.finish_bluetoothctl_actions()
        self.assertEqual(len(attempts), bluetooth_module.BLUETOOTH_RECONNECT_ATTEMPTS)
        self.assertTrue(all(addresses == [ADDRESS] for addresses in attempts))

    async def test_the_interval_throttles_tries_until_the_budget_is_spent(self):
        attempts = []
        monitor = self._monitor()
        monitor._reconnect_addresses = [ADDRESS]
        clock = [100.0]
        with patch.object(bluetooth_module, "reconnect_bluetooth_audio_devices",
                          side_effect=lambda addresses: attempts.append(list(addresses)) or []), patch.object(
            bluetooth_module.time, "monotonic", side_effect=lambda: clock[0]
        ):
            monitor._record_reconnect_attempt()
            await monitor.finish_bluetoothctl_actions()
            clock[0] += bluetooth_module.BLUETOOTH_RECONNECT_INTERVAL_SECONDS / 2
            monitor._record_reconnect_attempt()
            self.assertEqual(len(attempts), 1, "a tick inside the interval must not probe")
            clock[0] += bluetooth_module.BLUETOOTH_RECONNECT_INTERVAL_SECONDS / 2
            monitor._record_reconnect_attempt()
            await monitor.finish_bluetoothctl_actions()
        self.assertEqual(len(attempts), 2)

    async def test_a_returning_stream_retires_the_retry_budget(self):
        monitor = self._monitor()
        monitor._reconnect_addresses = [ADDRESS]
        monitor._reconnect_attempts = bluetooth_module.BLUETOOTH_RECONNECT_ATTEMPTS
        monitor._record_reconnect_attempt()
        self.assertFalse(monitor._pending_device_reconnect, "spent budget must not probe")
        with patch.object(monitor, "_ensure_agent", AsyncMock()), patch.object(
            bluetooth_module, "input_links_present", AsyncMock(return_value=True)
        ), patch.object(bluetooth_module.pw_link, "connect_ports", AsyncMock()):
            await monitor.sync({"mode": "bluetooth-input",
                                "bluetooth": {"selectable": True, "discoverable": True,
                                              "pairable": True, "source_name": SOURCE_NAME}})
        self.assertEqual(monitor._reconnect_attempts, 0)
        self.assertIsNone(monitor._last_reconnect_at)
        self.assertFalse(monitor._pending_device_reconnect)
        self.assertTrue(monitor._idle() or monitor._bluetooth_persisted())


if __name__ == "__main__":
    unittest.main()
