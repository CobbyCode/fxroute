#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Hardware playback-port resolution for DSP, diagnosis and readback.

A sink may name its playback ports ``playback_FL/FR/RL/RR`` (UMC204HD) or
``playback_AUX0…`` (Focusrite Scarlett 16i16 4th Gen).  The plan links against
the ports the device actually exposes, in its own channel order — and every
consumer (DSP link build, graph diagnosis, readback) must describe the same
resolved topology.  These tests pin both
port models, including the natural channel order that keeps ``AUX10`` behind
``AUX2``.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import playback.orchestration as playback_orchestration
from audio.output_ports import (
    hardware_playback_port_fallback_from_mode,
    hardware_playback_ports_from_mode,
    order_hardware_playback_ports,
    playback_port_names,
    playback_ports_from_channel_map,
    resolve_hardware_playback_ports,
    warn_semantic_playback_fallback,
)
from audio.samplerate import overview as overview_module

SCARLETT = "alsa_output.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-output"
UMC = "alsa_output.usb-BEHRINGER_UMC204HD_192k-00.analog-surround-40"
DSP_SINK = "fxroute_dsp_sink"

AUX_PORTS = tuple(f"playback_AUX{index}" for index in range(18))
SEMANTIC_PORTS = ("playback_FL", "playback_FR", "playback_RL", "playback_RR")
# The Channel Map a raw-channel USB interface publishes (Scarlett 16i16).
AUX_CHANNEL_MAP = ",".join(f"aux{index}" for index in range(18))
# The Channel Map a device with a real channel layout publishes (UMC204HD).
SEMANTIC_CHANNEL_MAP = "front-left,front-right,rear-left,rear-right"


def _io_listing(name: str, ports) -> str:
    return "\n".join(f"{name}:{port}" for port in ports)


def _short_line(sink_id: int, name: str, spec: str, state: str) -> str:
    return f"{sink_id}\t{name}\tPipeWire\t{spec}\t{state}"


def _detailed_block(sink_id: int, name: str, description: str, spec: str, state: str,
                    channel_map: str | None = None) -> str:
    block = (
        f"Sink #{sink_id}\n"
        f"\tState: {state}\n"
        f"\tName: {name}\n"
        f"\tDescription: {description}\n"
        f"\tSample Specification: {spec}\n"
    )
    if channel_map is not None:
        block += f"\tChannel Map: {channel_map}\n"
    return block


def _status(name: str) -> dict:
    return {
        "available": True,
        "sink": {"id": 1725, "name": name, "description": "Scarlett 16i16 4th Gen"},
        "relevant_sink": {"name": name},
        "notes": [],
    }


class PortResolutionTests(unittest.TestCase):
    """The pure resolver orders both port models by physical channel."""

    def test_semantic_ports_keep_their_conventional_order(self):
        self.assertEqual(order_hardware_playback_ports(SEMANTIC_PORTS), SEMANTIC_PORTS)

    def test_aux_ports_are_ordered_numerically_not_lexicographically(self):
        listing = _io_listing(SCARLETT, AUX_PORTS)
        self.assertEqual(
            resolve_hardware_playback_ports(listing, SCARLETT), AUX_PORTS
        )
        # Lexicographic ordering would put AUX10…AUX17 before AUX2.
        self.assertLess(
            resolve_hardware_playback_ports(listing, SCARLETT).index("playback_AUX2"),
            resolve_hardware_playback_ports(listing, SCARLETT).index("playback_AUX10"),
        )

    def test_other_nodes_and_input_ports_are_ignored(self):
        listing = "\n".join([
            f"{SCARLETT}:capture_AUX0",
            f"{UMC}:playback_FL",
            f"{SCARLETT}:playback_AUX0",
            f"{SCARLETT}:monitor_AUX1",
            f"{SCARLETT}:playback_AUX1",
        ])
        self.assertEqual(
            playback_port_names(listing, SCARLETT), ("playback_AUX0", "playback_AUX1")
        )

    def test_semantic_ports_win_over_extra_aux_ports(self):
        self.assertEqual(
            order_hardware_playback_ports(("playback_AUX0", "playback_FL", "playback_FR")),
            ("playback_FL", "playback_FR", "playback_AUX0"),
        )


class ChannelMapPlaybackPortTests(unittest.TestCase):
    """A sink's channel map describes its real playback ports.

    ``pactl list sinks`` publishes ``Channel Map:`` even while a suspended
    device has not published any port yet, so this derivation is the fallback
    that keeps the topology the device's own instead of the semantic
    ``playback_FL/FR`` guess that can never link on a raw-channel interface.
    """

    def test_aux_channel_map_yields_aux_ports(self):
        self.assertEqual(playback_ports_from_channel_map(AUX_CHANNEL_MAP, 18), AUX_PORTS)

    def test_positional_channel_map_yields_semantic_ports(self):
        self.assertEqual(
            playback_ports_from_channel_map(SEMANTIC_CHANNEL_MAP, 4), SEMANTIC_PORTS
        )

    def test_channel_map_is_bounded_by_the_reported_channel_count(self):
        self.assertEqual(
            playback_ports_from_channel_map(AUX_CHANNEL_MAP, 2),
            ("playback_AUX0", "playback_AUX1"),
        )

    def test_missing_positions_are_padded_in_channel_order(self):
        self.assertEqual(
            playback_ports_from_channel_map("front-left,front-right", 4),
            ("playback_FL", "playback_FR", "playback_RL", "playback_RR"),
        )

    def test_map_accepts_an_already_split_designation_list(self):
        self.assertEqual(
            playback_ports_from_channel_map(["AUX0", " AUX1 "], 2),
            ("playback_AUX0", "playback_AUX1"),
        )

    def test_without_a_channel_map_nothing_is_invented(self):
        self.assertEqual(playback_ports_from_channel_map(None, None), ())
        self.assertEqual(playback_ports_from_channel_map("", 18), ())
        self.assertEqual(playback_ports_from_channel_map(AUX_CHANNEL_MAP, 0), ())

    def test_mode_accessor_reads_the_payload_field(self):
        mode = {"hardware_playback_ports_from_channel_map": list(AUX_PORTS)}
        self.assertEqual(hardware_playback_port_fallback_from_mode(mode), AUX_PORTS)
        self.assertEqual(
            hardware_playback_port_fallback_from_mode(mode, count=2),
            ("playback_AUX0", "playback_AUX1"),
        )
        # A payload without the derived list yields nothing, never a guess.
        self.assertEqual(hardware_playback_port_fallback_from_mode({}), ())
        self.assertEqual(hardware_playback_port_fallback_from_mode(None), ())


class OutputDiscoveryPortTests(unittest.TestCase):
    """Output discovery carries the resolved list every consumer reuses."""

    def _overview(self, name: str, io_listing: str, *, channel_map: str | None = None,
                  spec: str = "s32le 18ch 44100Hz") -> dict:
        short = _short_line(1725, name, spec, "IDLE")
        detailed = _detailed_block(1725, name, "Scarlett 16i16 4th Gen", spec, "IDLE",
                                   channel_map=channel_map)

        def run(args: list[str]) -> str:
            if args[:1] == ["pactl"] and "short" in args:
                return short
            if args[:1] == ["pactl"]:
                return detailed
            if args[:2] == ["pw-cli", "ls"]:
                return f"id 9,\n    node.name = \"{name}\"\n"
            if args[:2] == ["pw-cli", "enum-params"]:
                return ""
            if args[:1] == ["pw-link"]:
                return io_listing
            raise AssertionError(f"unexpected command: {args}")

        with mock.patch.object(overview_module, "get_samplerate_status", return_value=_status(name)), \
             mock.patch.object(overview_module, "get_bluetooth_audio_overview", return_value={"available": False}), \
             mock.patch.object(overview_module, "_load_audio_output_selection", return_value={"selected_key": name}), \
             mock.patch.object(overview_module, "_run_command", side_effect=run):
            return overview_module.get_audio_output_overview()

    def test_discovery_reports_resolved_aux_ports(self):
        payload = self._overview(SCARLETT, _io_listing(SCARLETT, AUX_PORTS))
        self.assertEqual(payload["output_mode"]["hardware_playback_ports"], list(AUX_PORTS))
        self.assertEqual(payload["output_mode"]["effective_output_key"], SCARLETT)

    def test_discovery_reports_semantic_ports(self):
        payload = self._overview(UMC, _io_listing(UMC, SEMANTIC_PORTS))
        self.assertEqual(payload["output_mode"]["hardware_playback_ports"], list(SEMANTIC_PORTS))

    def test_discovery_carries_the_channel_map_fallback(self):
        payload = self._overview(SCARLETT, "", channel_map=AUX_CHANNEL_MAP)
        mode = payload["output_mode"]
        # The live listing had nothing, so the resolved list stays empty and
        # the sink's own channel map describes the fallback topology.
        self.assertEqual(mode["hardware_playback_ports"], [])
        self.assertEqual(mode["hardware_playback_ports_from_channel_map"], list(AUX_PORTS))

    def test_channel_map_fallback_describes_a_semantic_device_too(self):
        payload = self._overview(UMC, "", channel_map=SEMANTIC_CHANNEL_MAP,
                                 spec="s32le 4ch 44100Hz")
        self.assertEqual(
            payload["output_mode"]["hardware_playback_ports_from_channel_map"],
            list(SEMANTIC_PORTS),
        )

    def test_discovery_without_a_channel_map_invents_nothing(self):
        payload = self._overview(SCARLETT, "")
        self.assertEqual(payload["output_mode"]["hardware_playback_ports"], [])
        self.assertEqual(
            payload["output_mode"]["hardware_playback_ports_from_channel_map"], []
        )

    def test_resolved_list_and_channel_map_fallback_coexist(self):
        payload = self._overview(SCARLETT, _io_listing(SCARLETT, AUX_PORTS),
                                 channel_map=AUX_CHANNEL_MAP)
        self.assertEqual(payload["output_mode"]["hardware_playback_ports"], list(AUX_PORTS))
        self.assertEqual(
            payload["output_mode"]["hardware_playback_ports_from_channel_map"],
            list(AUX_PORTS),
        )

    def test_discovery_failure_degrades_to_empty_list(self):
        class Boom(RuntimeError):
            pass

        def run(args: list[str]) -> str:
            if args[:1] == ["pw-link"]:
                raise Boom("pw-link unavailable")
            if args[:1] == ["pactl"] and "short" in args:
                return _short_line(1725, SCARLETT, "s32le 18ch 44100Hz", "IDLE")
            if args[:1] == ["pactl"]:
                return _detailed_block(1725, SCARLETT, "Scarlett", "s32le 18ch 44100Hz", "IDLE")
            if args[:2] == ["pw-cli", "ls"]:
                return ""
            return ""

        # No channel map in that fixture either: an output whose topology is
        # completely unknown must stay empty instead of being guessed.

        with mock.patch.object(overview_module, "get_samplerate_status", return_value=_status(SCARLETT)), \
             mock.patch.object(overview_module, "get_bluetooth_audio_overview", return_value={"available": False}), \
             mock.patch.object(overview_module, "_load_audio_output_selection", return_value={"selected_key": SCARLETT}), \
             mock.patch.object(overview_module, "_run_command", side_effect=run):
            payload = overview_module.get_audio_output_overview()
        self.assertEqual(payload["output_mode"]["hardware_playback_ports"], [])


class GraphDiagnosisPortTests(unittest.IsolatedAsyncioTestCase):
    """The graph diagnosis verifies the same resolved hardware links."""

    PLAN_SNAPSHOT = {"active": True, "config": {
        "output_mode": "stereo", "output_key": SCARLETT, "hardware_ports": list(AUX_PORTS),
        "output_routes": [[1, "playback_AUX0"], [2, "playback_AUX1"]]}}

    def _orchestrator(self, io_text: str, links_text: str, snapshot=None):
        from types import SimpleNamespace

        from dsp.runtime import _contains_link as real_contains_link

        async def fake_pw_link(*args: str) -> str:
            return io_text if args == ("-io",) else links_text

        deps = SimpleNamespace(
            run_pw_link_command=fake_pw_link,
            output_mode_subwoofer_modes=frozenset({"subwoofer-2.1", "subwoofer-2.2"}),
            output_mode_stereo="stereo",
            get_dsp_snapshot=lambda: self.PLAN_SNAPSHOT if snapshot is None else snapshot,
            helper_argument_sample_rate=lambda snapshot: 44100,
            resolve_source_producer_ports=None,
            contains_link=real_contains_link,
        )
        return type("_Orchestrator", (), {
            "_deps": deps,
            "playback_graph_diagnosis": playback_orchestration.PlaybackOrchestrator.playback_graph_diagnosis,
            "missing_playback_graph_links": playback_orchestration.PlaybackOrchestrator.missing_playback_graph_links,
        })()

    def _overview(self, mode: str, ports=None, derived=None) -> dict:
        block = {"mode": mode, "effective_output_key": SCARLETT}
        if ports is not None:
            block["hardware_playback_ports"] = list(ports)
        if derived is not None:
            block["hardware_playback_ports_from_channel_map"] = list(derived)
        return {"output_mode": block}

    async def test_stereo_diagnosis_tracks_aux_links(self):
        io_text = _io_listing(SCARLETT, AUX_PORTS[:4]) + "\n" + "\n".join([
            f"{DSP_SINK}:monitor_FL", f"{DSP_SINK}:monitor_FR",
            "fxroute_dsp:input_1", "fxroute_dsp:input_2",
            "fxroute_dsp:output_1", "fxroute_dsp:output_2",
        ])
        links_text = "\n".join([
            f"{DSP_SINK}:monitor_FL -> fxroute_dsp:input_1",
            f"{DSP_SINK}:monitor_FR -> fxroute_dsp:input_2",
            f"fxroute_dsp:output_1 -> {SCARLETT}:playback_AUX0",
            f"fxroute_dsp:output_2 -> {SCARLETT}:playback_AUX1",
        ])
        orchestrator = self._orchestrator(io_text, links_text)
        diagnosis = await orchestrator.playback_graph_diagnosis(
            self._overview("stereo"), target_rate=44100
        )
        self.assertEqual(
            diagnosis["output_targets"],
            (f"{SCARLETT}:playback_AUX0", f"{SCARLETT}:playback_AUX1"),
        )
        self.assertTrue(diagnosis["links_complete"])
        self.assertTrue(all(diagnosis["links"].values()))

    async def test_diagnosis_rejects_additional_wrong_device(self):
        io_text = _io_listing(SCARLETT, AUX_PORTS[:4]) + "\n" + "\n".join([
            "fxroute_dsp:input_1", "fxroute_dsp:input_2",
            "fxroute_dsp:output_1", "fxroute_dsp:output_2",
        ])
        links = "\n".join([
            f"{DSP_SINK}:monitor_FL -> fxroute_dsp:input_1",
            f"{DSP_SINK}:monitor_FR -> fxroute_dsp:input_2",
            f"fxroute_dsp:output_1 -> {SCARLETT}:playback_AUX0",
            f"fxroute_dsp:output_2 -> {SCARLETT}:playback_AUX1",
            "fxroute_dsp:output_1 -> alsa_output.other:playback_FL",
        ])
        diagnosis = await self._orchestrator(io_text, links).playback_graph_diagnosis(
            self._overview("stereo"), target_rate=44100)
        self.assertFalse(diagnosis["links_complete"])
        self.assertTrue(diagnosis["bypass_only"])

    async def test_diagnosis_rejects_wrong_channel_on_selected_sink(self):
        io_text = _io_listing(SCARLETT, AUX_PORTS[:4]) + "\n" + "\n".join([
            "fxroute_dsp:input_1", "fxroute_dsp:input_2",
            "fxroute_dsp:output_1", "fxroute_dsp:output_2",
        ])
        links = "\n".join([
            f"{DSP_SINK}:monitor_FL -> fxroute_dsp:input_1",
            f"{DSP_SINK}:monitor_FR -> fxroute_dsp:input_2",
            f"fxroute_dsp:output_1 -> {SCARLETT}:playback_AUX0",
            f"fxroute_dsp:output_2 -> {SCARLETT}:playback_AUX1",
            f"fxroute_dsp:output_1 -> {SCARLETT}:playback_AUX1",
        ])
        diagnosis = await self._orchestrator(io_text, links).playback_graph_diagnosis(
            self._overview("stereo"), target_rate=44100)
        self.assertFalse(diagnosis["links_complete"])
        self.assertIn(f"fxroute_dsp:output_1 -> {SCARLETT}:playback_AUX1",
                      diagnosis["unexpected_output_links"])

    async def test_diagnosis_flags_a_missing_aux_link(self):
        io_text = _io_listing(SCARLETT, AUX_PORTS[:4]) + "\n" + "\n".join([
            f"{DSP_SINK}:monitor_FL", f"{DSP_SINK}:monitor_FR",
            "fxroute_dsp:input_1", "fxroute_dsp:input_2",
            "fxroute_dsp:output_1", "fxroute_dsp:output_2",
        ])
        links_text = "\n".join([
            f"{DSP_SINK}:monitor_FL -> fxroute_dsp:input_1",
            f"{DSP_SINK}:monitor_FR -> fxroute_dsp:input_2",
            f"fxroute_dsp:output_1 -> {SCARLETT}:playback_AUX0",
        ])
        orchestrator = self._orchestrator(io_text, links_text)
        diagnosis = await orchestrator.playback_graph_diagnosis(
            self._overview("stereo"), target_rate=44100
        )
        self.assertFalse(diagnosis["links_complete"])
        self.assertEqual(
            orchestrator.missing_playback_graph_links(diagnosis),
            [f"fxroute_dsp:output_2 -> {SCARLETT}:playback_AUX1"],
        )

    async def test_diagnosis_uses_the_channel_map_when_nothing_is_published(self):
        """Neither the payload nor the live read sees ports; the map decides."""
        io_text = "\n".join([
            f"{DSP_SINK}:monitor_FL", f"{DSP_SINK}:monitor_FR",
            "fxroute_dsp:input_1", "fxroute_dsp:input_2",
            "fxroute_dsp:output_1", "fxroute_dsp:output_2",
        ])
        links_text = "\n".join([
            f"{DSP_SINK}:monitor_FL -> fxroute_dsp:input_1",
            f"{DSP_SINK}:monitor_FR -> fxroute_dsp:input_2",
            f"fxroute_dsp:output_1 -> {SCARLETT}:playback_AUX0",
            f"fxroute_dsp:output_2 -> {SCARLETT}:playback_AUX1",
        ])
        orchestrator = self._orchestrator(io_text, links_text)
        with self.assertNoLogs("audio.output_ports", level="WARNING"):
            diagnosis = await orchestrator.playback_graph_diagnosis(
                self._overview("stereo", derived=AUX_PORTS), target_rate=44100
            )
        self.assertEqual(
            diagnosis["output_targets"],
            (f"{SCARLETT}:playback_AUX0", f"{SCARLETT}:playback_AUX1"),
        )
        self.assertTrue(diagnosis["links_complete"])

    async def test_bypass_detection_falls_back_to_live_port_resolution(self):
        """Without a discovery payload the live read names the ports to guard."""
        io_text = _io_listing(SCARLETT, AUX_PORTS[:4]) + "\n" + "\n".join([
            f"{DSP_SINK}:monitor_FL", f"{DSP_SINK}:monitor_FR",
            "fxroute_dsp:input_1", "fxroute_dsp:input_2",
            "fxroute_dsp:output_1", "fxroute_dsp:output_2",
        ])
        links_text = "\n".join([
            f"{DSP_SINK}:monitor_FL -> fxroute_dsp:input_1",
            f"{DSP_SINK}:monitor_FR -> fxroute_dsp:input_2",
            f"fxroute_dsp:output_1 -> {SCARLETT}:playback_AUX0",
            f"fxroute_dsp:output_2 -> {SCARLETT}:playback_AUX1",
            f"mpv:output_FL -> {SCARLETT}:playback_AUX2",
        ])
        orchestrator = self._orchestrator(io_text, links_text)
        diagnosis = await orchestrator.playback_graph_diagnosis(
            self._overview("stereo"), target_rate=44100
        )
        self.assertTrue(diagnosis["direct_source_to_hw_present"])
        self.assertFalse(diagnosis["links_complete"])

    async def test_expected_edges_follow_the_running_plan(self):
        """A plan that routes four outputs expects all four hardware links."""
        io_text = _io_listing(SCARLETT, AUX_PORTS[:4]) + "\n" + "\n".join([
            f"{DSP_SINK}:monitor_FL", f"{DSP_SINK}:monitor_FR",
            "fxroute_dsp:input_1", "fxroute_dsp:input_2",
            "fxroute_dsp:output_1", "fxroute_dsp:output_2",
            "fxroute_dsp:output_3", "fxroute_dsp:output_4",
        ])
        links_text = "\n".join([
            f"{DSP_SINK}:monitor_FL -> fxroute_dsp:input_1",
            f"{DSP_SINK}:monitor_FR -> fxroute_dsp:input_2",
            f"fxroute_dsp:output_1 -> {SCARLETT}:playback_AUX0",
            f"fxroute_dsp:output_2 -> {SCARLETT}:playback_AUX1",
            f"fxroute_dsp:output_3 -> {SCARLETT}:playback_AUX2",
            f"fxroute_dsp:output_4 -> {SCARLETT}:playback_AUX3",
        ])
        snapshot = {
            "active": True,
            "config": {
                "output_mode": "stereo-sub",
                "output_key": SCARLETT,
                "hardware_ports": list(AUX_PORTS),
                "output_routes": [[index + 1, port] for index, port in enumerate(AUX_PORTS[:4])],
            },
        }
        orchestrator = self._orchestrator(io_text, links_text, snapshot=snapshot)
        diagnosis = await orchestrator.playback_graph_diagnosis(
            self._overview("stereo", ports=AUX_PORTS), target_rate=44100
        )
        self.assertEqual(
            diagnosis["output_targets"],
            tuple(f"{SCARLETT}:{port}" for port in AUX_PORTS[:4]),
        )
        self.assertTrue(diagnosis["links_complete"])
        self.assertFalse(diagnosis["bypass_only"])

    async def test_without_a_plan_every_dsp_output_link_is_unexpected(self):
        """An engine without a plan config expects no hardware edge at all."""
        io_text = _io_listing(SCARLETT, AUX_PORTS[:4]) + "\n" + "\n".join([
            f"{DSP_SINK}:monitor_FL", f"{DSP_SINK}:monitor_FR",
            "fxroute_dsp:input_1", "fxroute_dsp:input_2",
            "fxroute_dsp:output_1", "fxroute_dsp:output_2",
        ])
        links_text = "\n".join([
            f"{DSP_SINK}:monitor_FL -> fxroute_dsp:input_1",
            f"{DSP_SINK}:monitor_FR -> fxroute_dsp:input_2",
            f"fxroute_dsp:output_1 -> {SCARLETT}:playback_AUX0",
            f"fxroute_dsp:output_2 -> {SCARLETT}:playback_AUX1",
        ])
        orchestrator = self._orchestrator(io_text, links_text, snapshot={"active": True})
        diagnosis = await orchestrator.playback_graph_diagnosis(
            self._overview("stereo", ports=AUX_PORTS), target_rate=44100
        )
        self.assertEqual(diagnosis["output_targets"], ())
        self.assertFalse(diagnosis["links_complete"])
        self.assertTrue(diagnosis["bypass_only"])


class RuntimeStateDumpPortTests(unittest.IsolatedAsyncioTestCase):
    """The debug readback reports the resolved hardware ports."""

    async def test_dump_reports_aux_targets(self):
        overview = {
            "output_mode": {
                "mode": "subwoofer-2.1",
                "effective_output_key": SCARLETT,
                "effective_output_rate": 48000,
                "hardware_playback_ports": list(AUX_PORTS),
            }
        }
        link_text = "\n".join([
            f"{DSP_SINK}:monitor_FL -> fxroute_dsp:input_1",
            f"{DSP_SINK}:monitor_FR -> fxroute_dsp:input_2",
            f"fxroute_dsp:output_1 -> {SCARLETT}:playback_AUX0",
            f"fxroute_dsp:output_2 -> {SCARLETT}:playback_AUX1",
            f"fxroute_dsp:output_3 -> {SCARLETT}:playback_AUX2",
            f"fxroute_dsp:output_4 -> {SCARLETT}:playback_AUX3",
        ])

        def debug_command(args, _timeout):
            if args[0] == "ps":
                return {"returncode": 0, "stdout": "42 /usr/bin/fxroute-dsp conf sock"}
            return {"returncode": 0, "stdout": link_text}

        class FakeRuntime:
            def snapshot(self):
                return {"helper_pid": 42, "config": {"sample_rate": 48000}}

        with mock.patch.object(main, "get_audio_output_overview", return_value=overview), \
             mock.patch.object(main, "get_samplerate_status", return_value={"active_rate": 48000, "force_rate": 48000}), \
             mock.patch.object(main.runtime, "dsp_runtime", FakeRuntime()), \
             mock.patch.object(main, "_run_debug_command", side_effect=debug_command), \
             mock.patch.object(main, "_read_build_id", return_value="test"), \
             mock.patch.object(main, "_measurement_helper_snapshot_summary", return_value={}):
            state = await main._dump_21_runtime_state("test")
        self.assertTrue(state["links"]["dsp_main_to_hw_present"])
        self.assertTrue(state["links"]["dsp_sub_to_hw_present"])

    async def test_dump_flags_semantic_topology_when_links_missing(self):
        overview = {
            "output_mode": {
                "mode": "subwoofer-2.1",
                "effective_output_key": SCARLETT,
                "effective_output_rate": 48000,
                "hardware_playback_ports": list(AUX_PORTS),
            }
        }

        def debug_command(_args, _timeout):
            return {"returncode": 0, "stdout": ""}

        class FakeRuntime:
            def snapshot(self):
                return {"helper_pid": None, "config": {"sample_rate": 48000}}

        with mock.patch.object(main, "get_audio_output_overview", return_value=overview), \
             mock.patch.object(main, "get_samplerate_status", return_value={"active_rate": 48000, "force_rate": 48000}), \
             mock.patch.object(main.runtime, "dsp_runtime", FakeRuntime()), \
             mock.patch.object(main, "_run_debug_command", side_effect=debug_command), \
             mock.patch.object(main, "_read_build_id", return_value="test"), \
             mock.patch.object(main, "_measurement_helper_snapshot_summary", return_value={}):
            state = await main._dump_21_runtime_state("test")
        self.assertFalse(state["links"]["dsp_main_to_hw_present"])
        self.assertFalse(state["links"]["dsp_sub_to_hw_present"])


class HardwarePlaybackPortsFromModeTests(unittest.TestCase):
    def test_discovered_ports_take_precedence_over_fallback(self):
        self.assertEqual(
            hardware_playback_ports_from_mode(
                {"hardware_playback_ports": list(AUX_PORTS)},
                SEMANTIC_PORTS,
                count=2,
            ),
            AUX_PORTS[:2],
        )

    def test_fallback_used_when_discovery_is_empty(self):
        self.assertEqual(
            hardware_playback_ports_from_mode({}, SEMANTIC_PORTS, count=4),
            SEMANTIC_PORTS,
        )


class SemanticFallbackWarningTests(unittest.TestCase):
    """A lost port discovery is announced, not silently substituted.

    When neither the discovery payload nor the direct ``pw-link -io`` fallback
    yields ports, consumers substitute the historic semantic names. On a
    device that really exposes only raw channels (playback_AUX0…) that
    topology can never link; the rate-limited warning makes that rare path
    visible for field diagnosis without changing fallback behavior.
    """

    def setUp(self):
        from audio import output_ports

        output_ports._semantic_fallback_warned_at.clear()

    def test_warning_fires_and_is_rate_limited_per_output_key(self):
        from audio import output_ports

        with self.assertLogs(output_ports.logger, level="WARNING") as captured:
            warn_semantic_playback_fallback(SCARLETT)
            warn_semantic_playback_fallback(SCARLETT)
        self.assertEqual(len(captured.output), 1)
        self.assertIn(SCARLETT, captured.output[0])
        self.assertIn("playback_AUX0", captured.output[0])
        # A different output key gets its own announcement.
        with self.assertLogs(output_ports.logger, level="WARNING"):
            warn_semantic_playback_fallback(UMC)

    def test_rate_limit_window_and_reset(self):
        from audio import output_ports

        warn_semantic_playback_fallback(SCARLETT)
        real_monotonic = output_ports.time.monotonic

        class _FakeTime:
            offset = 0.0

            @classmethod
            def monotonic(cls):
                return real_monotonic() + cls.offset

        with mock.patch.object(output_ports.time, "monotonic", _FakeTime.monotonic):
            with self.assertNoLogs(output_ports.logger, level="WARNING"):
                warn_semantic_playback_fallback(SCARLETT)  # inside window
            _FakeTime.offset = output_ports.SEMANTIC_FALLBACK_WARN_INTERVAL_S + 1
            with self.assertLogs(output_ports.logger, level="WARNING"):
                warn_semantic_playback_fallback(SCARLETT)  # window elapsed

    def test_diagnosis_warns_only_when_both_sources_failed(self):
        async def _diagnose(overview: dict, io_text: str) -> dict:
            orchestrator = self._diagnosis_orchestrator(io_text, "")
            return await orchestrator.playback_graph_diagnosis(overview, target_rate=44100)

        # Discovery failed AND the live read found nothing → warn.
        with self.assertLogs("audio.output_ports", level="WARNING"):
            asyncio.run(_diagnose(self._diagnosis_overview(), ""))
        # The live pw-link read resolves AUX ports → no warning.
        with self.assertNoLogs("audio.output_ports", level="WARNING"):
            asyncio.run(_diagnose(self._diagnosis_overview(), _io_listing(SCARLETT, AUX_PORTS)))
        # The discovery payload carries ports → no warning even with empty io.
        with self.assertNoLogs("audio.output_ports", level="WARNING"):
            asyncio.run(_diagnose(self._diagnosis_overview(ports=AUX_PORTS), ""))

    def test_no_warning_when_only_the_channel_map_describes_the_sink(self):
        async def _diagnose(overview: dict, io_text: str) -> dict:
            orchestrator = self._diagnosis_orchestrator(io_text, "")
            return await orchestrator.playback_graph_diagnosis(overview, target_rate=44100)

        # The payload carries no resolved list but does carry the sink's own
        # channel map, which is a real topology — not a semantic guess.
        with self.assertNoLogs("audio.output_ports", level="WARNING"):
            asyncio.run(_diagnose(self._diagnosis_overview(derived=AUX_PORTS), ""))

    def _diagnosis_overview(self, ports=None, derived=None) -> dict:
        block = {"mode": "stereo", "effective_output_key": SCARLETT}
        if ports is not None:
            block["hardware_playback_ports"] = list(ports)
        if derived is not None:
            block["hardware_playback_ports_from_channel_map"] = list(derived)
        return {"output_mode": block}

    def _diagnosis_orchestrator(self, io_text: str, links_text: str):
        from types import SimpleNamespace

        from dsp.runtime import _contains_link as real_contains_link

        async def fake_pw_link(*args: str) -> str:
            return io_text if args == ("-io",) else links_text

        deps = SimpleNamespace(
            run_pw_link_command=fake_pw_link,
            output_mode_subwoofer_modes=frozenset({"subwoofer-2.1", "subwoofer-2.2"}),
            output_mode_stereo="stereo",
            get_dsp_snapshot=lambda: {"active": True},
            helper_argument_sample_rate=lambda snapshot: 44100,
            resolve_source_producer_ports=None,
            contains_link=real_contains_link,
        )
        return type("_Orchestrator", (), {
            "_deps": deps,
            "playback_graph_diagnosis": playback_orchestration.PlaybackOrchestrator.playback_graph_diagnosis,
            "missing_playback_graph_links": playback_orchestration.PlaybackOrchestrator.missing_playback_graph_links,
        })()



if __name__ == "__main__":
    unittest.main(verbosity=2)
