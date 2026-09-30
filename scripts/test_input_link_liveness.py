#!/usr/bin/env python3
"""Live input-link recovery with unchanged source and node names."""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio import pw_link
import audio.bluetooth as bluetooth_module
import audio.external_input as external_module
from audio.bluetooth import BluetoothInputDependencies, BluetoothInputMonitor
from audio.external_input import ExternalInputRouting, ExternalInputRoutingDependencies


class LinkGraph:
    def __init__(self):
        self.links = set()
        self.connects = []

    async def read(self, *args):
        assert args == ("-l",)
        return "\n".join(f"{source} -> {sink}" for source, sink in sorted(self.links))

    async def connect(self, ports, sink):
        self.connects.append((ports, sink))
        self.links.add((ports[0], sink))

    async def disconnect(self, ports, sink):
        for source in ports:
            self.links.discard((source, sink))


class InputLinkLivenessTests(unittest.IsolatedAsyncioTestCase):
    async def _wait_for_links(self, graph, expected_connects):
        async def check():
            while len(graph.connects) < expected_connects:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(check(), 2)
        self.assertEqual(len(graph.links), 2)

    async def test_bluetooth_monitor_repairs_link_loss_without_source_change(self):
        graph = LinkGraph()
        overview = {"mode": "bluetooth-input", "bluetooth": {
            "selectable": True, "discoverable": True, "pairable": True, "source_name": "bluez.source",
        }}
        monitor = BluetoothInputMonitor(BluetoothInputDependencies(
            sync_peak_monitor_for_source_mode_state=AsyncMock(),
            get_persisted_source_mode=lambda: "bluetooth-input",
        ))
        with patch.object(pw_link, "run_pw_link_command", graph.read), patch.object(
            pw_link, "connect_ports", graph.connect
        ), patch.object(pw_link, "disconnect_ports", graph.disconnect), patch.object(
            bluetooth_module, "get_audio_source_overview", return_value=overview
        ), patch.object(monitor, "_ensure_agent", AsyncMock()), patch.object(
            bluetooth_module, "disconnect_connected_bluetooth_audio_sources", return_value=[]
        ), patch.object(bluetooth_module, "BLUETOOTH_INPUT_MONITOR_INTERVAL_SECONDS", 0.01, create=True):
            monitor.monitor_task = asyncio.create_task(monitor.run_monitor_loop())
            try:
                await self._wait_for_links(graph, 2)
                graph.links.clear()
                await self._wait_for_links(graph, 4)
                self.assertEqual(monitor.input_source_name, "bluez.source")
            finally:
                await monitor.stop()

    async def test_external_monitor_repairs_link_loss_without_source_change(self):
        graph = LinkGraph()
        overview = {"mode": "external-input", "selected_input": {
            "source_key": "line.source", "key": "line.source::pair:1-2",
        }}
        routing = ExternalInputRouting(ExternalInputRoutingDependencies(
            get_audio_source_overview=lambda: overview,
            get_persisted_source_mode=lambda: "external-input",
        ))
        with patch.object(pw_link, "run_pw_link_command", graph.read), patch.object(
            pw_link, "connect_ports", graph.connect
        ), patch.object(pw_link, "disconnect_ports", graph.disconnect), patch.object(
            external_module, "EXTERNAL_INPUT_MONITOR_INTERVAL_SECONDS", 0.01, create=True
        ):
            routing.monitor_task = asyncio.create_task(routing.run_monitor_loop())
            try:
                await self._wait_for_links(graph, 2)
                graph.links.clear()
                await self._wait_for_links(graph, 4)
                self.assertEqual(routing.loopback_source_name, "line.source")
            finally:
                await routing.stop()

    async def test_bluetooth_same_named_node_recovers_lost_links_without_reconnecting_healthy_graph(self):
        graph = LinkGraph()
        monitor = BluetoothInputMonitor(BluetoothInputDependencies(
            sync_peak_monitor_for_source_mode_state=AsyncMock(),
        ))
        with patch.object(pw_link, "run_pw_link_command", graph.read), patch.object(
            pw_link, "connect_ports", graph.connect
        ), patch.object(pw_link, "disconnect_ports", graph.disconnect):
            await monitor._ensure_loopback("bluez.source")
            self.assertEqual(len(graph.links), 2)
            await monitor._ensure_loopback("bluez.source")
            self.assertEqual(len(graph.connects), 2)
            graph.links.clear()  # The PipeWire node was recreated with the same name.
            await monitor._ensure_loopback("bluez.source")
            self.assertEqual(len(graph.links), 2)
            self.assertEqual(len(graph.connects), 4)
            self.assertEqual(monitor.input_source_name, "bluez.source")

    async def test_external_same_selection_recovers_lost_links_without_reconnecting_healthy_graph(self):
        graph = LinkGraph()
        overview = {"mode": "external-input", "selected_input": {
            "source_key": "line.source", "key": "line.source::pair:3-4",
            "left_channel": "RL", "right_channel": "RR",
        }}
        routing = ExternalInputRouting(ExternalInputRoutingDependencies(
            get_audio_source_overview=lambda: overview,
        ))
        with patch.object(pw_link, "run_pw_link_command", graph.read), patch.object(
            pw_link, "connect_ports", graph.connect
        ), patch.object(pw_link, "disconnect_ports", graph.disconnect):
            await routing.sync(overview)
            self.assertEqual(len(graph.links), 2)
            await routing.sync(overview)
            self.assertEqual(len(graph.connects), 2)
            graph.links.discard(("line.source:capture_RL", "fxroute_dsp_sink:playback_FL"))
            await routing.sync(overview)
            self.assertEqual(len(graph.links), 2)
            self.assertEqual(len(graph.connects), 4)
            self.assertEqual(routing.loopback_selection_key, "line.source::pair:3-4")

    async def test_external_source_switch_during_recovery_cannot_restore_old_input(self):
        graph = LinkGraph()
        overview = {"mode": "external-input", "selected_input": {
            "source_key": "line.source", "key": "line.source::pair:1-2",
        }}
        routing = ExternalInputRouting(ExternalInputRoutingDependencies(
            get_audio_source_overview=lambda: overview,
        ))
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked_connect(ports, sink):
            if len(graph.connects) == 2 and not entered.is_set():
                entered.set()
                await release.wait()
            await graph.connect(ports, sink)

        with patch.object(pw_link, "run_pw_link_command", graph.read), patch.object(
            pw_link, "connect_ports", blocked_connect
        ), patch.object(pw_link, "disconnect_ports", graph.disconnect):
            await routing.sync(overview)
            graph.links.clear()
            recovery = asyncio.create_task(routing.sync(overview))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                switch = asyncio.create_task(routing.sync({"mode": "app-playback"}))
                await asyncio.sleep(0)
                self.assertFalse(switch.done())
                release.set()
                await asyncio.wait_for(asyncio.gather(recovery, switch), 2)
                self.assertIsNone(routing.loopback_source_name)
                self.assertFalse(graph.links)
            finally:
                release.set()
                if not recovery.done():
                    recovery.cancel()
                await asyncio.gather(recovery, return_exceptions=True)

    async def test_bluetooth_never_publishes_source_without_verified_links(self):
        graph = LinkGraph()
        monitor = BluetoothInputMonitor(BluetoothInputDependencies(
            sync_peak_monitor_for_source_mode_state=AsyncMock(),
        ))
        with patch.object(pw_link, "run_pw_link_command", graph.read), patch.object(
            pw_link, "connect_ports", AsyncMock()
        ), patch.object(pw_link, "disconnect_ports", graph.disconnect):
            with self.assertRaises(RuntimeError):
                await monitor._ensure_loopback("bluez.source")
            self.assertIsNone(monitor.input_source_name)

    async def test_external_never_publishes_selection_without_verified_links(self):
        graph = LinkGraph()
        overview = {"mode": "external-input", "selected_input": {
            "source_key": "line.source", "key": "line.source::pair:1-2",
        }}
        routing = ExternalInputRouting(ExternalInputRoutingDependencies(
            get_audio_source_overview=lambda: overview,
        ))
        with patch.object(pw_link, "run_pw_link_command", graph.read), patch.object(
            pw_link, "connect_ports", AsyncMock()
        ), patch.object(pw_link, "disconnect_ports", graph.disconnect):
            with self.assertRaises(RuntimeError):
                await routing.sync(overview)
            self.assertIsNone(routing.loopback_source_name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
