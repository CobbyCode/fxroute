#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Bluetooth connections outlive source selection; A2DP recovery stays bounded."""

import asyncio
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audio.bluetooth as bluetooth_module
import audio.samplerate.bluetooth as bluez_mod
from audio.bluetooth import BluetoothInputDependencies, BluetoothInputMonitor
from audio.source_feed import SourceOverviewFeed, SourceTransitionLock
from audio.source_transitions import SourceTransitionDependencies, SourceTransitions

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
        self.assertEqual(calls, [["bluetoothctl", "info", ADDRESS],
                                 ["bluetoothctl", "connect", ADDRESS,
                                  "0000110a-0000-1000-8000-00805f9b34fb"]])

    def test_acl_connection_without_stream_still_requests_a2dp(self):
        connected, calls = self._run({ADDRESS: _info(connected=True)}, [ADDRESS])
        self.assertEqual(connected, [ADDRESS])
        self.assertEqual(calls[-1], ["bluetoothctl", "connect", ADDRESS,
                                     "0000110a-0000-1000-8000-00805f9b34fb"])

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


class ConnectionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """Source selection changes routing, not the peer connection/authorization."""

    async def asyncSetUp(self):
        self.mode = "app-playback"
        self.links = set()
        self.agents = []
        self.monitor = BluetoothInputMonitor(BluetoothInputDependencies(
            sync_peak_monitor_for_source_mode_state=AsyncMock(),
            get_persisted_source_mode=lambda: self.mode,
        ))

        class Agent:
            returncode = None

            def terminate(self):
                self.returncode = 0

            async def wait(self):
                return self.returncode

        async def spawn(*args, **kwargs):
            agent = Agent()
            self.agents.append(agent)
            return agent

        self.spawn = spawn

        async def connect(ports, sink):
            self.links.add((ports[0], sink))

        async def disconnect(ports, sink):
            self.links.discard((ports[0], sink))

        async def present(source, channels):
            return all((f"{source}:capture_{left}", f"fxroute_dsp_sink:playback_{right}")
                       in self.links for left, right in channels)

        self.kick = Mock(return_value=[])
        self.reconnect = Mock(return_value=[])
        for patcher in (
            patch.object(bluetooth_module.asyncio, "create_subprocess_exec", spawn),
            patch.object(bluetooth_module.pw_link, "connect_ports", connect),
            patch.object(bluetooth_module.pw_link, "disconnect_ports", disconnect),
            patch.object(bluetooth_module, "input_links_present", present),
            patch.object(bluez_mod, "disconnect_connected_bluetooth_audio_sources", self.kick),
            patch.object(bluetooth_module, "reconnect_bluetooth_audio_devices", self.reconnect),
            patch.object(bluetooth_module, "set_bluetooth_receiver_enabled"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def overview(self, mode, *, source=SOURCE_NAME):
        self.mode = mode
        return {"mode": mode, "bluetooth": {"selectable": True,
                "discoverable": mode == "bluetooth-input", "pairable": mode == "bluetooth-input",
                "source_name": source}}

    async def test_inactive_startup_keeps_authorization_available_without_audio_links(self):
        await self.monitor.sync(self.overview("app-playback"))
        await self.monitor.finish_bluetoothctl_actions()
        self.assertIsNotNone(self.monitor.agent_process, "inactive input still needs authorization")
        self.assertIsNone(self.monitor.agent_process.returncode)
        self.assertEqual(self.links, set())
        self.kick.assert_not_called()

    async def test_switch_away_and_back_only_unlinks_and_relinks_stereo(self):
        await self.monitor.sync(self.overview("bluetooth-input"))
        agent = self.monitor.agent_process
        expected = {(f"{SOURCE_NAME}:capture_{channel}", f"fxroute_dsp_sink:playback_{channel}")
                    for channel in ("FL", "FR")}
        self.assertEqual(self.links, expected)
        await self.monitor.sync(self.overview("app-playback"))
        await self.monitor.finish_bluetoothctl_actions()
        self.assertEqual(self.links, set())
        self.assertIs(self.monitor.agent_process, agent)
        self.assertIsNone(agent.returncode)
        self.kick.assert_not_called()
        await self.monitor.sync(self.overview("bluetooth-input"))
        await self.monitor.finish_bluetoothctl_actions()
        self.assertEqual(self.links, expected)
        self.assertIs(self.monitor.agent_process, agent)
        self.reconnect.assert_not_called()

    async def test_profile_success_without_source_keeps_spaced_bounded_recovery(self):
        await self.monitor.sync(self.overview("bluetooth-input"))
        self.reconnect.return_value = [ADDRESS]
        clock = [100.0]
        missing = self.overview("bluetooth-input", source=None)
        with patch.object(bluetooth_module, "time", Mock(monotonic=lambda: clock[0])):
            for attempt in range(bluetooth_module.BLUETOOTH_RECONNECT_ATTEMPTS):
                await self.monitor.sync(missing)
                await self.monitor.finish_bluetoothctl_actions()
                self.assertEqual(self.reconnect.call_count, attempt + 1,
                                 "profile success without a source must not stop recovery")
                self.assertIsNone(self.monitor.input_source_name)
                self.assertEqual(self.links, set())
                clock[0] += bluetooth_module.BLUETOOTH_RECONNECT_INTERVAL_SECONDS / 2
                await self.monitor.sync(missing)
                await self.monitor.finish_bluetoothctl_actions()
                self.assertEqual(self.reconnect.call_count, attempt + 1,
                                 "profile success must not bypass retry spacing")
                clock[0] += bluetooth_module.BLUETOOTH_RECONNECT_INTERVAL_SECONDS / 2
            await self.monitor.sync(missing)
            await self.monitor.finish_bluetoothctl_actions()
        self.assertEqual(self.reconnect.call_count, bluetooth_module.BLUETOOTH_RECONNECT_ATTEMPTS)
        self.assertTrue(all(call.args == ([ADDRESS],) for call in self.reconnect.call_args_list))
        self.kick.assert_not_called()

    async def test_late_source_after_recovery_budget_is_linked_on_monitor_tick(self):
        await self.monitor.sync(self.overview("bluetooth-input"))
        self.reconnect.return_value = [ADDRESS]
        with patch.object(bluetooth_module, "BLUETOOTH_RECONNECT_INTERVAL_SECONDS", 0.0):
            missing = self.overview("bluetooth-input", source=None)
            for _ in range(bluetooth_module.BLUETOOTH_RECONNECT_ATTEMPTS):
                await self.monitor.sync(missing)
                await self.monitor.finish_bluetoothctl_actions()
        self.assertEqual(self.reconnect.call_count, bluetooth_module.BLUETOOTH_RECONNECT_ATTEMPTS)
        self.assertEqual(self.links, set())
        late_source = self.overview("bluetooth-input")
        with patch.object(bluetooth_module, "get_audio_source_overview", return_value=late_source):
            await self.monitor._monitor_once()
            await self.monitor._monitor_once()
        self.assertEqual(self.monitor.input_source_name, SOURCE_NAME)
        self.assertEqual(self.links, {
            (f"{SOURCE_NAME}:capture_FL", "fxroute_dsp_sink:playback_FL"),
            (f"{SOURCE_NAME}:capture_FR", "fxroute_dsp_sink:playback_FR"),
        })
        self.assertEqual(self.reconnect.call_count, bluetooth_module.BLUETOOTH_RECONNECT_ATTEMPTS,
                         "an available source must not trigger another profile request")
        self.kick.assert_not_called()

    async def test_source_returning_during_profile_connect_remains_recoverable(self):
        await self.monitor.sync(self.overview("bluetooth-input"))
        started, release = threading.Event(), threading.Event()

        def connect(addresses):
            if not started.is_set():
                started.set()
                if not release.wait(5):
                    raise RuntimeError("profile connect test was not released")
            return [ADDRESS]

        self.reconnect.side_effect = connect
        await self.monitor.sync(self.overview("bluetooth-input", source=None))
        finish = asyncio.create_task(self.monitor.finish_bluetoothctl_actions())
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            await self.monitor.sync(self.overview("bluetooth-input"))
        finally:
            release.set()
            await asyncio.wait_for(finish, 2)
        self.assertEqual(len(self.links), 2)
        self.assertEqual(self.reconnect.call_count, 1)
        await self.monitor.sync(self.overview("bluetooth-input", source=None))
        await self.monitor.finish_bluetoothctl_actions()
        self.assertEqual(self.reconnect.call_count, 2,
                         "a returned source must allow recovery of a later loss")
        self.kick.assert_not_called()

    async def test_inactive_monitor_repairs_an_exited_agent(self):
        await self.monitor.sync(self.overview("bluetooth-input"))
        await self.monitor.sync(self.overview("app-playback"))
        await self.monitor.finish_bluetoothctl_actions()
        self.assertIsNotNone(self.monitor.agent_process)
        self.assertTrue(self.monitor._idle(), "healthy inactive input must skip expensive inventories")
        self.monitor.agent_process.returncode = 1
        self.assertFalse(self.monitor._idle(), "a dead authorization agent needs recovery")
        await self.monitor._act(self.overview("app-playback"))
        self.assertIs(self.monitor.agent_process, self.agents[-1])
        self.assertEqual(len(self.agents), 2)
        self.assertIsNone(self.monitor.agent_process.returncode)

    async def test_shutdown_stops_idle_agent_without_disconnecting_peer(self):
        await self.monitor.sync(self.overview("app-playback"))
        agent = self.monitor.agent_process
        self.assertIsNotNone(agent)
        await self.monitor.stop()
        self.assertEqual(agent.returncode, 0)
        self.assertIsNone(self.monitor.agent_process)
        self.kick.assert_not_called()

    async def test_agent_failure_does_not_roll_back_an_app_switch(self):
        await self.monitor.sync(self.overview("bluetooth-input"))
        self.monitor.agent_process.returncode = 1
        lock = SourceTransitionLock()
        transitions = SourceTransitions(SourceTransitionDependencies(
            get_lock=lambda: lock, feed=SourceOverviewFeed(AsyncMock()),
            build_overview=lambda: self.overview(self.mode),
            save_selection=lambda mode, key: self.overview(mode),
            load_selection=lambda: {"mode": self.mode, "selected_input_key": None},
            with_current_stdin_status=lambda value: value,
            sync_external_input=AsyncMock(side_effect=lambda value: value),
            sync_bluetooth_input=self.monitor.sync, get_stdin_input=lambda: None,
            note_source_selection=lambda mode: None, pause_app_playback=AsyncMock(),
            sync_peak_monitor=AsyncMock(), sync_peak_monitor_for_stdin=AsyncMock(),
            finish_bluetoothctl_actions=self.monitor.finish_bluetoothctl_actions,
        ))
        with patch.object(bluetooth_module.asyncio, "create_subprocess_exec", side_effect=OSError("agent unavailable")):
            result = await transitions.save_selection("app-playback", None)
        self.assertEqual(result["mode"], "app-playback")
        self.assertEqual(self.mode, "app-playback")
        self.assertEqual(self.links, set())
        self.assertIsNone(self.monitor.agent_process, "a failed replacement must not retain a dead process")
        self.assertTrue(self.monitor._idle(), "failed replacement must respect the inactive retry interval")

    async def test_adapter_appearing_after_inactive_startup_starts_authorization(self):
        unavailable = self.overview("app-playback", source=None)
        unavailable["bluetooth"]["selectable"] = False
        await self.monitor.sync(unavailable)
        await self.monitor.finish_bluetoothctl_actions()
        self.assertIsNone(self.monitor.agent_process)
        self.assertTrue(self.monitor._idle(), "unavailable adapter must not be polled on every tick")
        self.monitor._last_inactive_probe_at = 0.0
        self.assertFalse(self.monitor._idle(), "an inactive missing agent must eventually retry")
        with patch.object(bluetooth_module, "get_audio_source_overview",
                          return_value=self.overview("app-playback", source=None)):
            await self.monitor._monitor_once()
        self.assertIsNotNone(self.monitor.agent_process)
        self.assertIsNone(self.monitor.agent_process.returncode)
        self.assertTrue(self.monitor._idle())

    async def test_immediate_agent_exit_is_retried_while_inactive(self):
        async def exited(*args, **kwargs):
            agent = await self.spawn(*args, **kwargs)
            agent.returncode = 1
            agent.stderr = AsyncMock()
            agent.stderr.read.return_value = b"registration failed"
            return agent

        with patch.object(bluetooth_module.asyncio, "create_subprocess_exec", exited):
            await self.monitor.sync(self.overview("app-playback", source=None))
        await self.monitor.finish_bluetoothctl_actions()
        self.assertIsNone(self.monitor.agent_process)
        self.assertTrue(self.monitor._idle(), "agent retries must be bounded")
        self.monitor._last_inactive_probe_at = 0.0
        self.assertFalse(self.monitor._idle())
        await self.monitor._act(self.overview("app-playback", source=None))
        self.assertIsNotNone(self.monitor.agent_process)
        self.assertIsNone(self.monitor.agent_process.returncode)
        self.assertEqual(len(self.agents), 2)


class ReentryTests(unittest.IsolatedAsyncioTestCase):
    """Recover dropped streams, never reconnect an intact source on selection."""

    def _monitor(self, persisted="bluetooth-input"):
        return BluetoothInputMonitor(BluetoothInputDependencies(
            sync_peak_monitor_for_source_mode_state=AsyncMock(),
            get_persisted_source_mode=lambda: persisted,
        ))

    async def test_a_streamed_device_is_reconnected_after_leaving(self):
        """A peer that dropped on its own is remembered from its stream."""
        monitor = self._monitor()
        reconnects = []
        with patch.object(monitor, "_ensure_agent", AsyncMock()), patch.object(
            bluetooth_module, "input_links_present", AsyncMock(return_value=True)
        ), patch.object(bluetooth_module.pw_link, "connect_ports", AsyncMock()), patch.object(
            bluetooth_module.pw_link, "disconnect_ports", AsyncMock()
        ), patch.object(bluetooth_module, "reconnect_bluetooth_audio_devices",
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

    async def test_leaving_cancels_a_recorded_reconnect(self):
        """Deferred recovery must not connect a peer after another input wins."""
        reconnects = []
        monitor = self._monitor()
        monitor._reconnect_addresses = [ADDRESS]
        with patch.object(monitor, "_ensure_agent", AsyncMock()), patch.object(
            bluetooth_module, "reconnect_bluetooth_audio_devices",
                        side_effect=lambda addresses: reconnects.append(list(addresses)) or []), patch.object(
            bluetooth_module, "set_bluetooth_receiver_enabled") as receiver:
            await monitor.sync({"mode": "bluetooth-input",
                                "bluetooth": {"selectable": True, "discoverable": True,
                                              "pairable": True}})
            self.assertTrue(monitor._pending_device_reconnect)
            await monitor.sync({"mode": "app-playback"})
            await monitor.finish_bluetoothctl_actions()
        self.assertEqual(reconnects, [])
        self.assertEqual(receiver.call_args_list[-1].args, (False,))

    async def test_leaving_during_receiver_enable_cancels_captured_reconnect(self):
        monitor = self._monitor()
        monitor._reconnect_addresses = [ADDRESS]
        started, release = threading.Event(), threading.Event()
        reconnects, receiver_modes = [], []

        def toggle(enabled):
            receiver_modes.append(enabled)
            if enabled:
                started.set()
                if not release.wait(5):
                    raise RuntimeError("receiver test was not released")

        with patch.object(monitor, "_ensure_agent", AsyncMock()), patch.object(
            bluetooth_module, "set_bluetooth_receiver_enabled", side_effect=toggle
        ), patch.object(bluetooth_module, "reconnect_bluetooth_audio_devices",
                        side_effect=lambda addresses: reconnects.append(list(addresses)) or []):
            await monitor.sync({"mode": "bluetooth-input", "bluetooth": {
                "selectable": True, "discoverable": False, "pairable": False}})
            finish = asyncio.create_task(monitor.finish_bluetoothctl_actions())
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                await monitor.sync({"mode": "app-playback"})
            finally:
                release.set()
                await asyncio.wait_for(finish, 2)
        self.assertEqual(receiver_modes, [True, False])
        self.assertEqual(reconnects, [], "deselection must invalidate an already captured reconnect")

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
