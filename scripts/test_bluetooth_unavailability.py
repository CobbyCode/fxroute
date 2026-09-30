#!/usr/bin/env python3
"""Bluetooth input: act on a confirmed loss only, recover when it returns.

A single failed availability probe (bluetoothctl timeout, WirePlumber
restart) looks like a removed adapter. The selected Bluetooth input must
not fall back to App playback, lose its agent or disconnect an active
stream on one such probe; only a loss that persists for
SOURCE_UNAVAILABLE_CONFIRM_SECONDS does all three. The monitor observes each
tick's probe and owns the confirmation; the source overview only reads it.
The scenarios run the real get_audio_source_overview over a scripted raw
probe and the real monitor wired through main (shared publish path).
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audio.bluetooth as bluetooth_module
import audio.samplerate.overview as overview_mod
from audio.source_monitor import UnavailabilityConfirmation
import main
from scripts.test_peak_monitor_line_source_arming import make_coordinator

TICK = 3.0  # BLUETOOTH_INPUT_MONITOR_INTERVAL_SECONDS


def _pactl(args):
    if args[:2] == ["pactl", "info"]:
        return "Server Name: PulseAudio\n"
    if args in (["pactl", "list", "sources", "short"], ["pactl", "list", "sources"]):
        return ""
    raise AssertionError(f"unexpected command: {args}")


class _FakeAgent:
    def __init__(self):
        self.returncode = None
        self.stderr = None

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


class _Host:
    """Scripted raw Bluetooth probe plus a manual clock."""

    def __init__(self):
        self.available = True
        self.streaming = False
        self.now = 1000.0

    def probe(self):
        session = ({"source_name": "bluez_input.phone", "device_name": "Phone",
                    "active_codec": "aac", "streaming": True}
                   if self.available and self.streaming else {})
        state = "unavailable"
        if self.available:
            state = "streaming" if self.streaming else "idle"
        return {
            "available": self.available,
            "roles": {"bluetooth_input": {
                "selectable": self.available, "state": state, "enabled": True,
                "discoverable": True, "pairable": True, "notes": [],
            }},
            "receiver_session": session,
        }


class ConfirmationTests(unittest.TestCase):
    """The monitor observes each probe; the overview only reads the decision."""

    def setUp(self):
        self.host = _Host()
        self.monitor = main.bluetooth_input
        persisted = {"mode": "bluetooth-input", "selected_input_key": None}
        for patcher in (
            patch.object(main.samplerate, "_load_audio_source_selection", return_value=persisted),
            patch.object(overview_mod, "_load_audio_source_selection", return_value=persisted),
            patch.object(overview_mod, "_run_command", side_effect=_pactl),
            patch.object(overview_mod, "get_bluetooth_audio_overview", side_effect=self.host.probe),
            patch.object(self.monitor, "availability",
                         UnavailabilityConfirmation(monotonic=lambda: self.host.now)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _build(self, *, available, advance=0.0):
        self.host.now += advance
        self.host.available = available
        return overview_mod.get_audio_source_overview()

    def _tick(self, *, available, advance=0.0):
        """Build then observe, like the monitor tick; return the built mode."""
        overview = self._build(available=available, advance=advance)
        self.monitor._observe_availability(overview)
        return overview["mode"]

    def test_single_failed_probe_keeps_bluetooth_selected(self):
        self.assertEqual(self._tick(available=False), "bluetooth-input")
        self.assertEqual(self._tick(available=True, advance=TICK), "bluetooth-input")

    def test_loss_is_confirmed_once_it_persists(self):
        self.assertEqual(self._tick(available=False), "bluetooth-input")
        self.assertEqual(self._tick(available=False, advance=TICK), "bluetooth-input")
        self.assertEqual(self._tick(available=False, advance=TICK), "app-playback")

    def test_an_available_probe_in_between_starts_over(self):
        self._tick(available=False)
        self._tick(available=True, advance=TICK)
        self.assertEqual(self._tick(available=False, advance=TICK), "bluetooth-input")
        self.assertEqual(self._tick(available=False, advance=TICK), "bluetooth-input")

    def test_one_observed_failure_never_confirms_alone(self):
        self._tick(available=False)
        self.assertEqual(self._build(available=False, advance=600.0)["mode"], "bluetooth-input")

    def test_building_the_overview_never_records_a_probe(self):
        """Polls, switches and STDIN rebuilds read the decision, never feed it."""
        for _ in range(5):
            self.assertEqual(self._build(available=False, advance=TICK)["mode"], "bluetooth-input")
        self.assertFalse(self.monitor.availability.confirmed())

    def test_overview_carries_the_probed_receiver_source_for_routing(self):
        self.host.streaming = True
        overview = self._build(available=True)
        self.assertEqual(overview["bluetooth"].get("source_name"), "bluez_input.phone")
        self.host.streaming = False
        self.assertIsNone(self._build(available=True)["bluetooth"].get("source_name"))

    def test_leaving_bluetooth_input_forgets_an_outage(self):
        self._tick(available=False)
        self._tick(available=False, advance=TICK)
        with patch.object(main.samplerate, "_load_audio_source_selection",
                          return_value={"mode": "app-playback", "selected_input_key": None}):
            self.monitor._observe_availability(self._build(available=False))
        self.assertFalse(self.monitor.availability.confirmed())
        self.assertEqual(self._build(available=False, advance=TICK)["mode"], "bluetooth-input")


class MonitorScenarioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.host = _Host()
        self._lock = main.runtime.source_transition_lock
        main.runtime.source_transition_lock = None
        main.source_overview_feed.reset()
        self.broadcast = AsyncMock()
        self.coordinator, _monitor = make_coordinator()
        self.agents = []
        self.receiver = Mock()
        self.disconnect_devices = Mock(return_value=[])
        self.save_selection = Mock()
        self.monitor = main.bluetooth_input

        async def spawn(*_args, **_kwargs):
            self.agents.append(_FakeAgent())
            return self.agents[-1]

        async def link(source_name):
            self.monitor.input_source_name = source_name

        persisted = {"mode": "bluetooth-input", "selected_input_key": None}
        for patcher in (
            patch.object(main.manager, "broadcast", self.broadcast),
            patch.object(main, "peak_monitor_coordinator", self.coordinator),
            patch.object(main.samplerate, "_load_audio_source_selection", return_value=persisted),
            patch.object(main.samplerate, "_save_audio_source_selection", self.save_selection),
            patch.object(overview_mod, "_load_audio_source_selection", return_value=persisted),
            patch.object(overview_mod, "_run_command", side_effect=_pactl),
            patch.object(overview_mod, "get_bluetooth_audio_overview", side_effect=self.host.probe),
            patch.object(self.monitor, "availability",
                         UnavailabilityConfirmation(monotonic=lambda: self.host.now)),
            patch.object(bluetooth_module, "set_bluetooth_receiver_enabled", self.receiver),
            patch.object(bluetooth_module, "disconnect_connected_bluetooth_audio_sources",
                         self.disconnect_devices),
            patch.object(bluetooth_module.pw_link, "disconnect_ports", AsyncMock()),
            patch.object(bluetooth_module.asyncio, "create_subprocess_exec", spawn),
            patch.object(self.monitor, "_ensure_loopback", AsyncMock(side_effect=link)),
            patch.object(self.monitor, "agent_process", None),
            patch.object(self.monitor, "input_source_name", None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        main.runtime.source_transition_lock = self._lock
        main.source_overview_feed.reset()

    async def _tick(self, *, available, streaming=None):
        self.host.now += TICK
        self.host.available = available
        if streaming is not None:
            self.host.streaming = streaming
        await self.monitor._monitor_once()

    def _pushed_modes(self):
        return [call.args[0]["data"]["mode"] for call in self.broadcast.await_args_list
                if call.args[0]["type"] == "source"]

    async def test_sync_keeps_everything_while_the_loss_is_unconfirmed(self):
        """Startup/switch syncs during a blip neither fail nor tear down."""
        await self._tick(available=True)
        agent = self.monitor.agent_process
        self.host.available = False
        overview = await self.monitor.sync(overview_mod.get_audio_source_overview())
        self.assertEqual(overview["mode"], "bluetooth-input")
        self.assertIs(self.monitor.agent_process, agent)
        self.assertIsNone(agent.returncode)
        self.receiver.assert_not_called()

    async def test_transient_probe_failure_changes_nothing(self):
        await self._tick(available=True)
        agent = self.monitor.agent_process
        await self._tick(available=False)
        self.assertIs(self.monitor.agent_process, agent)
        self.assertIsNone(agent.returncode, "one failed probe must not stop the agent")
        await self._tick(available=True)
        self.assertIs(self.monitor.agent_process, agent, "no agent restart after a blip")
        self.assertEqual(self._pushed_modes(), ["bluetooth-input"], "no App-playback fallback pushed")
        self.disconnect_devices.assert_not_called()

    async def test_sustained_adapter_loss_stops_the_agent_and_recovers(self):
        await self._tick(available=True)
        first_agent = self.monitor.agent_process
        await self._tick(available=False)
        await self._tick(available=False)
        self.assertEqual(self._pushed_modes(), ["bluetooth-input"], "3 s of loss is not confirmed yet")
        self.assertIsNone(first_agent.returncode)
        await self._tick(available=False)
        self.assertEqual(self._pushed_modes(), ["bluetooth-input", "app-playback"])
        self.assertEqual(first_agent.returncode, 0, "a confirmed loss stops the leftover agent")
        self.assertIsNone(self.monitor.agent_process)
        await self._tick(available=False)
        self.assertEqual(len(self._pushed_modes()), 2, "an unchanged fallback is pushed once")

        await self._tick(available=True)
        self.assertEqual(self._pushed_modes(), ["bluetooth-input", "app-playback", "bluetooth-input"])
        self.assertEqual(len(self.agents), 2)
        self.assertIs(self.monitor.agent_process, self.agents[1], "a fresh agent registers on return")
        self.disconnect_devices.assert_not_called()
        self.save_selection.assert_not_called()

    async def test_single_failed_probe_keeps_an_active_stream(self):
        await self._tick(available=True, streaming=True)
        self.assertEqual(self.monitor.input_source_name, "bluez_input.phone")
        await self._tick(available=False)
        self.assertEqual(self.monitor.input_source_name, "bluez_input.phone",
                         "one failed probe must not unlink the stream")
        self.assertIsNone(self.monitor.agent_process.returncode)
        await self._tick(available=True, streaming=True)
        self.assertEqual(self.monitor.input_source_name, "bluez_input.phone")
        self.disconnect_devices.assert_not_called()
        self.assertNotIn("app-playback", self._pushed_modes())

    async def test_sustained_loss_while_streaming_disconnects_and_recovers(self):
        await self._tick(available=True, streaming=True)
        await self._tick(available=False)
        await self._tick(available=False)
        self.disconnect_devices.assert_not_called()
        self.assertEqual(self.monitor.input_source_name, "bluez_input.phone")
        await self._tick(available=False)
        self.disconnect_devices.assert_called_once()
        self.assertIsNone(self.monitor.input_source_name)
        self.assertIsNone(self.monitor.agent_process)
        self.assertEqual(self._pushed_modes()[-1], "app-playback")

        await self._tick(available=True, streaming=True)
        self.assertEqual(self.monitor.input_source_name, "bluez_input.phone")
        self.assertIs(self.monitor.agent_process, self.agents[-1])
        self.assertEqual(self._pushed_modes()[-1], "bluetooth-input")
        self.save_selection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
