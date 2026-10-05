#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Multichannel STDIN ingress: runtime ownership, lifecycle and main wiring.

A STDIN layout with positions beyond the front pair links straight to the
Surround engine inputs through the mapped-ingress slot of the DSP runtime;
Stereo and Stereo + Sub keep the ``fxroute_dsp_sink`` route.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.stdin_input import StdinInputDependencies, StdinInputService
from audio.stdin_pipewire import PipeWirePcmSink, StdinIngress
from audio.stdin_protocol import parse_spec
from dsp.runtime import CommandResult, DSPRuntime, DSPRuntimeConfig, mapped_ingress_edges

NODE = "fxroute_stdin_abc"
SUFFIXES = {"FL": "AUX0", "FR": "AUX1", "FC": "AUX2", "LFE": "AUX3", "BL": "AUX4", "BR": "AUX5"}
EDGES = mapped_ingress_edges(NODE, SUFFIXES, port_kind="output")


class _Graph:
    """Fake pw-link graph for the runtime."""

    def __init__(self):
        self.links = set()
        self.nodes = {NODE}

    async def run(self, args):
        if args == ("pw-link", "-l"):
            return CommandResult(0, "\n".join(f"{a} -> {b}" for a, b in sorted(self.links)))
        if args[1] == "-d":
            self.links.discard(tuple(args[2:]))
            return CommandResult(0)
        if args[1].split(":")[0].startswith("fxroute_stdin") and args[1].split(":")[0] not in self.nodes:
            return CommandResult(1, "", "failed to link ports: No such file or directory")
        self.links.add(tuple(args[1:]))
        return CommandResult(0)

    def stdin_links(self):
        return {link for link in self.links if link[0].startswith("fxroute_stdin")}


def _native(graph, mode="surround"):
    native = DSPRuntime(SimpleNamespace(), command_runner=graph.run)
    native._process = SimpleNamespace(returncode=None)
    native._config = DSPRuntimeConfig(mode, "A", 48000, ("playback_AUX0",), ())
    return native


class EdgeTests(unittest.TestCase):
    def test_a_playback_stream_names_its_output_ports(self):
        self.assertEqual(mapped_ingress_edges("n", {"FL": "AUX0", "BR": "AUX9"}, port_kind="output"), (
            ("n:output_AUX0", "fxroute_dsp:input_1"), ("n:output_AUX9", "fxroute_dsp:input_8")))

    def test_capture_nodes_stay_the_default(self):
        self.assertEqual(mapped_ingress_edges("n", {"FR": "FR"}), (("n:capture_FR", "fxroute_dsp:input_2"),))


class RuntimeOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_surround_engine_follows_the_running_config(self):
        native = _native(_Graph())
        self.assertTrue(native.surround_engine)
        native._config = DSPRuntimeConfig("stereo", "A", 48000, ("playback_AUX0",), ())
        self.assertFalse(native.surround_engine)
        native._config = None
        self.assertFalse(native.surround_engine)
        native._config = DSPRuntimeConfig("surround", "A", 48000, ("playback_AUX0",), ())
        native._process = None
        self.assertFalse(native.surround_engine)

    async def test_release_clears_only_the_edges_it_owns(self):
        graph = _Graph()
        native = _native(graph)
        await native.set_mapped_ingress(EDGES)
        self.assertEqual(graph.stdin_links(), set(EDGES))
        await native.release_mapped_ingress(EDGES)
        self.assertEqual((graph.stdin_links(), native._mapped_ingress), (set(), ()))
        self.assertIn(("fxroute_dsp_sink:monitor_FL", "fxroute_dsp:input_1"), graph.links)

    async def test_a_newer_owner_keeps_its_ingress_when_an_old_stream_releases(self):
        graph = _Graph()
        native = _native(graph)
        newer = mapped_ingress_edges("alsa_input.card", {"FL": "AUX0", "FR": "AUX1"})
        await native.set_mapped_ingress(newer)
        await native.release_mapped_ingress(EDGES)
        self.assertEqual(native._mapped_ingress, newer)
        self.assertEqual({source for source, _ in graph.links if source.startswith("alsa_input.")},
                         {"alsa_input.card:capture_AUX0", "alsa_input.card:capture_AUX1"})

    async def test_engine_rebuild_relinks_the_stream_before_the_engine_goes_live(self):
        graph = _Graph()
        native = _native(graph)
        native._mapped_ingress = EDGES
        await native._reconcile_ingress_links()
        self.assertEqual(graph.stdin_links(), set(EDGES))
        self.assertTrue(await native.verify())

    async def test_rebuild_to_a_stereo_engine_links_only_the_front_inputs_until_released(self):
        graph = _Graph()
        native = _native(graph)
        await native.set_mapped_ingress(EDGES)
        native._config = DSPRuntimeConfig("stereo", "A", 48000, ("playback_AUX0",), ())
        await native._reconcile_ingress_links()
        self.assertEqual({target for _, target in graph.stdin_links()},
                         {"fxroute_dsp:input_1", "fxroute_dsp:input_2"})
        await native.release_mapped_ingress(EDGES)
        self.assertEqual(graph.stdin_links(), set())

    async def test_a_stream_released_during_a_measurement_is_not_resumed(self):
        graph = _Graph()
        native = _native(graph)
        await native.set_mapped_ingress(EDGES)
        previous = await native.enter_active_measurement()
        self.assertEqual(graph.stdin_links(), set())
        graph.nodes.clear()
        await native.release_mapped_ingress(EDGES)
        await native.exit_active_measurement(previous)
        self.assertEqual((native._mapped_ingress, native._measurement_mapped_ingress, graph.stdin_links()),
                         ((), (), set()))

    async def test_a_live_stream_is_suspended_and_resumed_by_a_measurement(self):
        graph = _Graph()
        native = _native(graph)
        await native.set_mapped_ingress(EDGES)
        previous = await native.enter_active_measurement()
        self.assertEqual(graph.stdin_links(), set())
        await native.exit_active_measurement(previous)
        self.assertEqual(graph.stdin_links(), set(EDGES))


class MainWiringTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import main
        self.main = main
        self.dsp = SimpleNamespace(
            set_mapped_ingress=AsyncMock(), release_mapped_ingress=AsyncMock(),
            verify=AsyncMock(return_value=True), surround_engine=True, sync_in_progress=False)
        patcher = patch.object(main.runtime, "dsp_runtime", self.dsp)
        patcher.start()
        self.addCleanup(patcher.stop)

    def sink(self, channels=6, **extra):
        spec = parse_spec(dict(version=1, format="s16le", rate=48000, channels=channels, **extra))
        return self.main._build_stdin_sink(spec, "abc")

    def test_service_builds_its_sinks_with_the_engine_hooks(self):
        sink = self.sink()
        self.assertIsInstance(sink, PipeWirePcmSink)
        self.assertIsInstance(sink._ingress, StdinIngress)
        self.assertEqual(sink.edges, EDGES)

    def test_surround_follows_the_running_engine(self):
        sink = self.sink()
        self.assertTrue(sink._ingress.surround_active())
        self.dsp.surround_engine = False
        self.assertFalse(sink._ingress.surround_active())
        with patch.object(self.main.runtime, "dsp_runtime", None):
            self.assertFalse(sink._ingress.surround_active())

    def test_surround_is_unknown_while_the_engine_is_rebuilt(self):
        hook = self.sink()._ingress.surround_active
        self.dsp.sync_in_progress = True
        self.assertIsNone(hook())
        self.dsp.surround_engine = False
        self.assertIsNone(hook())
        self.dsp.sync_in_progress = False
        self.assertIs(hook(), False)

    def test_surround_is_unknown_during_a_coordinated_transition(self):
        hook = self.sink()._ingress.surround_active
        with patch.object(self.main, "_playback_transition_is_active", return_value=True):
            self.assertIsNone(hook())
        self.assertIs(hook(), True)

    async def test_links_are_not_judged_while_the_engine_is_rebuilt(self):
        hook = self.sink()._ingress.edges_present
        with patch.object(self.main, "ingress_links_present", AsyncMock(return_value=False)) as live:
            self.dsp.sync_in_progress = True
            self.assertTrue(await hook(EDGES))
            live.assert_not_awaited()
            self.dsp.sync_in_progress = False
            self.assertFalse(await hook(EDGES))
            live.assert_awaited_once_with(EDGES)

    async def test_links_are_not_judged_during_a_coordinated_transition(self):
        hook = self.sink()._ingress.edges_present
        with patch.object(self.main, "ingress_links_present", AsyncMock(return_value=False)) as live, \
                patch.object(self.main, "_playback_transition_is_active", return_value=True):
            self.assertTrue(await hook(EDGES))
            live.assert_not_awaited()

    async def test_linking_verifies_the_graph(self):
        with patch.object(self.main, "_measurement_session_owns_live_rate", return_value=False):
            await self.main._set_stdin_ingress(EDGES)
        self.dsp.set_mapped_ingress.assert_awaited_once_with(EDGES)

    async def test_unverified_links_fail(self):
        self.dsp.verify.return_value = False
        with patch.object(self.main, "_measurement_session_owns_live_rate", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "could not be verified"):
                await self.main._set_stdin_ingress(EDGES)

    async def test_a_measurement_owning_the_graph_refuses_the_stream(self):
        with patch.object(self.main, "_measurement_session_owns_live_rate", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "measurement"):
                await self.main._set_stdin_ingress(EDGES)
        self.dsp.set_mapped_ingress.assert_not_awaited()

    async def test_missing_runtime_refuses_linking_but_release_is_a_no_op(self):
        with patch.object(self.main.runtime, "dsp_runtime", None):
            with self.assertRaisesRegex(RuntimeError, "native DSP runtime"):
                await self.main._set_stdin_ingress(EDGES)
            await self.main._release_stdin_ingress(EDGES)

    async def test_release_hands_the_edges_to_the_runtime_owner_check(self):
        await self.main._release_stdin_ingress(EDGES)
        self.dsp.release_mapped_ingress.assert_awaited_once_with(EDGES)

    async def test_resync_reaches_the_running_service_and_tolerates_none(self):
        service = SimpleNamespace(resync_routing=AsyncMock())
        with patch.object(self.main.runtime, "stdin_input", service):
            await self.main._resync_stdin_routing()
        service.resync_routing.assert_awaited_once_with()
        with patch.object(self.main.runtime, "stdin_input", None):
            await self.main._resync_stdin_routing()


class OutputModeFollowTests(unittest.IsolatedAsyncioTestCase):
    """Service, real sink and a fake engine: the route follows the output mode."""

    async def asyncSetUp(self):
        self.surround = True
        self.edges = ()
        self.sink_links = set()
        self.calls = []

        async def set_edges(edges):
            self.calls.append("set")
            self.edges = edges

        async def release_edges(edges):
            self.calls.append("release")
            if self.edges == edges:
                self.edges = ()

        async def present(edges):
            return bool(edges) and self.edges == edges

        async def connect(sources, target):
            self.calls.append("connect")
            self.sink_links.add(target)

        async def disconnect(sources, target):
            self.calls.append("disconnect")
            self.sink_links.discard(target)

        async def sink_present(_name, channels):
            return all(f"fxroute_dsp_sink:playback_{side}" in self.sink_links for _, side in channels)

        create = asyncio.create_subprocess_exec

        async def spawn(*argv, **kwargs):
            return await create(sys.executable, "-c", "import sys; sys.stdin.buffer.read()", **kwargs)

        ports = "\n".join(f"fxroute_stdin_{{id}}:output_AUX{i}" for i in range(8))
        self.ingress = StdinIngress(lambda: self.surround, set_edges, release_edges, present)
        self.sinks = []

        def factory(spec, session_id):
            sink = PipeWirePcmSink(spec, session_id, self.ingress)
            self.sinks.append(sink)
            self.ports = ports.replace("{id}", session_id)
            return sink

        async def run_pw_link(*_args):
            return self.ports

        for patcher in (
            patch("audio.stdin_pipewire.pcm_adapter_error", return_value=None),
            patch("audio.stdin_pipewire.asyncio.create_subprocess_exec", side_effect=spawn),
            patch("audio.stdin_pipewire.pw_link.run_pw_link_command", side_effect=run_pw_link),
            patch("audio.stdin_pipewire.pw_link.connect_ports", side_effect=connect),
            patch("audio.stdin_pipewire.pw_link.disconnect_ports", side_effect=disconnect),
            patch("audio.stdin_pipewire.input_links_present", side_effect=sink_present),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "stdin.sock"

        async def ignore():
            return None

        self.service = StdinInputService(StdinInputDependencies(factory, ignore))
        await self.service.start(self.path)
        await self.service.set_selected(True)

    async def asyncTearDown(self):
        await self.service.stop()

    async def writer(self, **metadata):
        import json
        reader, writer = await asyncio.open_unix_connection(str(self.path))
        self.addCleanup(writer.close)
        header = dict(version=1, format="s16le", rate=48000, channels=6) | metadata
        writer.write(json.dumps(header).encode() + b"\n")
        await writer.drain()
        for expected in ("accepted", "ready"):
            self.assertEqual(json.loads(await asyncio.wait_for(reader.readline(), 3))["event"], expected)
        return reader, writer

    async def test_a_layout_stream_starts_on_the_direct_route_in_surround(self):
        await self.writer()
        self.assertEqual((self.edges != (), self.sink_links), (True, set()))
        self.assertTrue(self.service.snapshot()["routed"])

    async def test_resync_moves_the_stream_to_the_sink_route_when_surround_ends(self):
        await self.writer()
        self.surround = False
        await self.service.resync_routing()
        self.assertEqual((self.edges, self.sink_links),
                         ((), {"fxroute_dsp_sink:playback_FL", "fxroute_dsp_sink:playback_FR"}))
        self.surround = True
        await self.service.resync_routing()
        self.assertEqual((self.edges != (), self.sink_links), (True, set()))

    async def test_the_link_watch_follows_a_mode_change_without_a_resync(self):
        await self.writer()
        self.surround = False
        async with asyncio.timeout(3):
            while not self.sink_links:
                await asyncio.sleep(0.05)
        self.assertEqual(self.edges, ())
        self.assertTrue(self.service.snapshot()["routed"])

    async def test_a_rebuild_in_flight_neither_moves_nor_kills_the_stream(self):
        await self.writer()
        self.surround = None
        await asyncio.sleep(1.3)  # more than two link-watch ticks
        self.assertEqual((self.edges != (), self.sink_links), (True, set()))
        snapshot = self.service.snapshot()
        self.assertEqual((snapshot["routed"], snapshot["error"]), (True, None))
        self.surround = False
        await self.service.resync_routing()
        self.assertEqual((self.edges, len(self.sink_links)), ((), 2))

    async def test_the_front_pair_never_reaches_the_engine_on_both_routes(self):
        await self.writer()
        for surround in (False, True, False):
            self.surround = surround
            await self.service.resync_routing()
            self.assertFalse(self.edges and self.sink_links, (surround, self.edges, self.sink_links))

    async def test_a_plain_stereo_stream_never_touches_the_engine_hooks(self):
        await self.writer(channels=2)
        self.assertEqual((self.edges, self.calls.count("set"), self.calls.count("connect")), ((), 0, 2))
        self.surround = False
        self.surround = True
        await self.service.resync_routing()
        self.assertEqual((self.calls.count("set"), self.calls.count("disconnect")), (0, 0))

    async def test_ending_the_stream_releases_the_ingress(self):
        reader, writer = await self.writer()
        writer.write_eof()
        await asyncio.wait_for(reader.read(), 3)
        async with asyncio.timeout(3):
            while self.edges:
                await asyncio.sleep(0.02)
        self.assertEqual(self.edges, ())

    async def test_deselecting_the_source_releases_the_ingress(self):
        await self.writer()
        await self.service.set_selected(False)
        self.assertEqual(self.edges, ())


if __name__ == "__main__":
    unittest.main()
