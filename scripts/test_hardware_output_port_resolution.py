#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Hardware playback-port resolution for DSP, diagnosis and readback.

FXRoute's DSP outputs are fixed (Out 1/2 Main, Out 3/4 Sub), but a sink may
name its playback ports ``playback_FL/FR/RL/RR`` (UMC204HD) or
``playback_AUX0…`` (Focusrite Scarlett 16i16 4th Gen).  The runtime must link
against the ports the device actually exposes, in its own channel order — and
every consumer (DSP link build, graph diagnosis, silent-active watcher,
readback) must describe the same resolved topology.  These tests pin both
port models, including the natural channel order that keeps ``AUX10`` behind
``AUX2``.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import playback.orchestration as playback_orchestration
from audio.output_ports import (
    hardware_playback_ports_from_mode,
    order_hardware_playback_ports,
    playback_port_names,
    resolve_hardware_playback_ports,
)
from audio.samplerate import overview as overview_module
from dsp.manager import DSPManager
from dsp.runtime import CommandResult, DSPRuntime, DSPRuntimeConfig
from playback.silent_active import SilentActiveRecovery

SCARLETT = "alsa_output.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-output"
UMC = "alsa_output.usb-BEHRINGER_UMC204HD_192k-00.analog-surround-40"
DSP_SINK = "fxroute_dsp_sink"

AUX_PORTS = tuple(f"playback_AUX{index}" for index in range(18))
SEMANTIC_PORTS = ("playback_FL", "playback_FR", "playback_RL", "playback_RR")


def _io_listing(name: str, ports) -> str:
    return "\n".join(f"{name}:{port}" for port in ports)


def _short_line(sink_id: int, name: str, spec: str, state: str) -> str:
    return f"{sink_id}\t{name}\tPipeWire\t{spec}\t{state}"


def _detailed_block(sink_id: int, name: str, description: str, spec: str, state: str) -> str:
    return (
        f"Sink #{sink_id}\n"
        f"\tState: {state}\n"
        f"\tName: {name}\n"
        f"\tDescription: {description}\n"
        f"\tSample Specification: {spec}\n"
    )


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


class DSPRuntimeConfigPortTests(unittest.TestCase):
    """The DSP maps its fixed outputs onto the resolved hardware ports."""

    def setUp(self):
        self.manager = DSPManager(home=Path(tempfile.mkdtemp()))
        self.manager.save_global_extras({"limiter": {"enabled": False}})

    def overview(self, mode: str, *, channels: int = 18, ports=None) -> dict:
        block = {
            "mode": mode,
            "effective_output_key": SCARLETT,
            "effective_output_channels": channels,
            "effective_output_rate": 48000,
        }
        if ports is not None:
            block["hardware_playback_ports"] = list(ports)
        if mode == "subwoofer-2.1":
            block["subwoofer"] = {"crossover_frequency_hz": 80, "main_highpass_enabled": True,
                                  "sub_level_db": -2, "sub_alignment_ms": 3, "sub_polarity": "normal"}
        if mode.startswith("subwoofer-2.2"):
            block.update({"crossover_frequency_hz": 90, "main_highpass_enabled": True,
                          "subwoofers": {"sub1": {"level_db": -1, "alignment_ms": 0, "polarity": "normal"},
                                         "sub2": {"level_db": -3, "alignment_ms": 0, "polarity": "normal"}}})
        return {"output_mode": block, "selected_output": {"key": SCARLETT, "channels": channels}}

    def test_stereo_aux_uses_first_two_ports_for_a_2ch_device(self):
        config = DSPRuntimeConfig.from_overview(
            self.overview("stereo", channels=2, ports=AUX_PORTS),
            hardware_ports=AUX_PORTS,
        )
        self.assertEqual(config.hardware_ports, ("playback_AUX0", "playback_AUX1"))

    def test_stereo_aux_multichannel_uses_first_four_ports(self):
        config = DSPRuntimeConfig.from_overview(
            self.overview("stereo", ports=AUX_PORTS), hardware_ports=AUX_PORTS
        )
        self.assertEqual(config.hardware_ports, AUX_PORTS[:4])

    def test_subwoofer_modes_use_the_first_four_resolved_ports(self):
        for mode in ("subwoofer-2.1", "subwoofer-2.2", "subwoofer-2.2-stereo"):
            config = DSPRuntimeConfig.from_overview(
                self.overview(mode, ports=AUX_PORTS), hardware_ports=AUX_PORTS
            )
            self.assertEqual(config.hardware_ports, AUX_PORTS[:4], mode)

    def test_semantic_model_is_unchanged(self):
        config = DSPRuntimeConfig.from_overview(
            self.overview("subwoofer-2.1", channels=4, ports=SEMANTIC_PORTS),
            hardware_ports=SEMANTIC_PORTS,
        )
        self.assertEqual(config.hardware_ports, SEMANTIC_PORTS)

    def test_legacy_overview_without_discovery_keeps_semantic_ports(self):
        config = DSPRuntimeConfig.from_overview(self.overview("subwoofer-2.1", channels=4))
        self.assertEqual(config.hardware_ports, SEMANTIC_PORTS)

    def test_subwoofer_mode_with_too_few_hardware_ports_is_rejected(self):
        with self.assertRaises(RuntimeError) as raised:
            DSPRuntimeConfig.from_overview(
                self.overview("subwoofer-2.1", channels=2, ports=AUX_PORTS[:2]),
                hardware_ports=AUX_PORTS[:2],
            )
        self.assertIn("requires 4 hardware playback ports", str(raised.exception))
        self.assertIn(SCARLETT, str(raised.exception))


class DSPRuntimeAuxLinkTests(unittest.IsolatedAsyncioTestCase):
    """The runtime links its DSP outputs to the resolved AUX ports."""

    def setUp(self):
        self.manager = DSPManager(home=Path(tempfile.mkdtemp()))
        self.manager.save_global_extras({"limiter": {"enabled": False}})

    def overview(self, mode: str, *, ports=None, channels: int = 18) -> dict:
        block = {
            "mode": mode,
            "effective_output_key": SCARLETT,
            "effective_output_channels": channels,
            "effective_output_rate": 48000,
        }
        if ports is not None:
            block["hardware_playback_ports"] = list(ports)
        if mode == "subwoofer-2.1":
            block["subwoofer"] = {"crossover_frequency_hz": 80, "main_highpass_enabled": True,
                                  "sub_level_db": -2, "sub_alignment_ms": 0, "sub_polarity": "normal"}
        return {"output_mode": block, "selected_output": {"key": SCARLETT, "channels": channels}}

    class _Process:
        returncode = None
        pid = 4321

        def terminate(self):
            self.returncode = 0

        async def wait(self):
            return 0

    async def _sync(self, overview: dict, *, io_listing: str = "") -> list[tuple]:
        commands: list[tuple] = []
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "fxroute-dsp"
            binary.touch()
            runtime = DSPRuntime(self.manager, binary=binary)

            async def run(command):
                commands.append(tuple(command))
                if tuple(command) == ("pw-link", "-io"):
                    return CommandResult(0, io_listing)
                return CommandResult(0, "")

            async def control(command, **_kwargs):
                return "0\n" if command == "effects bypass get" else "ok\n"

            async def launch(_command):
                return self._Process()

            runtime._run = run
            runtime._control = control
            runtime._launch = launch
            async def wait_for_ports(_config):
                return None

            runtime._wait_for_ports = wait_for_ports
            await runtime._sync(overview)
            await runtime.stop()
        return commands

    @staticmethod
    def _links(commands: list[tuple]) -> set[tuple]:
        return {
            (command[1], command[2]) for command in commands
            if len(command) == 3 and command[0] == "pw-link" and not command[1].startswith("-")
        }

    async def test_stereo_sync_links_out_1_2_to_aux0_1(self):
        commands = await self._sync(self.overview("stereo", ports=AUX_PORTS))
        links = self._links(commands)
        self.assertIn(("fxroute_dsp:output_1", f"{SCARLETT}:playback_AUX0"), links)
        self.assertIn(("fxroute_dsp:output_2", f"{SCARLETT}:playback_AUX1"), links)
        self.assertNotIn(("fxroute_dsp:output_1", f"{SCARLETT}:playback_FL"), links)

    async def test_subwoofer_sync_links_out_1_4_to_aux0_3(self):
        commands = await self._sync(self.overview("subwoofer-2.1", ports=AUX_PORTS))
        links = self._links(commands)
        self.assertIn(("fxroute_dsp:output_3", f"{SCARLETT}:playback_AUX2"), links)
        self.assertIn(("fxroute_dsp:output_4", f"{SCARLETT}:playback_AUX3"), links)

    async def test_sync_without_discovery_reads_live_ports(self):
        overview = self.overview("stereo")
        commands = await self._sync(overview, io_listing=_io_listing(SCARLETT, AUX_PORTS))
        self.assertIn(("pw-link", "-io"), commands)
        links = self._links(commands)
        self.assertIn(("fxroute_dsp:output_1", f"{SCARLETT}:playback_AUX0"), links)

    async def test_semantic_device_keeps_fl_fr_rl_rr_links(self):
        overview = self.overview("stereo", ports=SEMANTIC_PORTS, channels=4)
        links = self._links(await self._sync(overview))
        self.assertIn(("fxroute_dsp:output_1", f"{SCARLETT}:playback_FL"), links)
        self.assertIn(("fxroute_dsp:output_2", f"{SCARLETT}:playback_FR"), links)


class OutputDiscoveryPortTests(unittest.TestCase):
    """Output discovery carries the resolved list every consumer reuses."""

    def _overview(self, name: str, io_listing: str) -> dict:
        short = _short_line(1725, name, "s32le 18ch 44100Hz", "IDLE")
        detailed = _detailed_block(1725, name, "Scarlett 16i16 4th Gen", "s32le 18ch 44100Hz", "IDLE")

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

        with mock.patch.object(overview_module, "get_samplerate_status", return_value=_status(SCARLETT)), \
             mock.patch.object(overview_module, "get_bluetooth_audio_overview", return_value={"available": False}), \
             mock.patch.object(overview_module, "_load_audio_output_selection", return_value={"selected_key": SCARLETT}), \
             mock.patch.object(overview_module, "_run_command", side_effect=run):
            payload = overview_module.get_audio_output_overview()
        self.assertEqual(payload["output_mode"]["hardware_playback_ports"], [])


class GraphDiagnosisPortTests(unittest.IsolatedAsyncioTestCase):
    """The graph diagnosis verifies the same resolved hardware links."""

    def _orchestrator(self, io_text: str, links_text: str):
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

    def _overview(self, mode: str, ports=None) -> dict:
        block = {"mode": mode, "effective_output_key": SCARLETT}
        if ports is not None:
            block["hardware_playback_ports"] = list(ports)
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

    async def test_diagnosis_falls_back_to_live_port_resolution(self):
        io_text = _io_listing(SCARLETT, AUX_PORTS[:4]) + "\n" + "\n".join([
            f"{DSP_SINK}:monitor_FL", f"{DSP_SINK}:monitor_FR",
            "fxroute_dsp:input_1", "fxroute_dsp:input_2",
            "fxroute_dsp:output_1", "fxroute_dsp:output_2",
        ])
        links_text = "\n".join([
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


class SilentActiveAuxPortTests(unittest.TestCase):
    """The silent-active watcher recognizes AUX hardware links."""

    def _recovery(self) -> SilentActiveRecovery:
        return SilentActiveRecovery.__new__(SilentActiveRecovery)

    def test_aux_stereo_topology_is_recognized(self):
        links = (
            "\tmpv:output_FL\n"
            f"  |-> {DSP_SINK}:playback_FL\n"
            "\tfxroute_dsp:output_1\n"
            f"  |-> {SCARLETT}:playback_AUX0\n"
            "\tfxroute_dsp:output_2\n"
            f"  |-> {SCARLETT}:playback_AUX1\n"
        )
        output_mode = {
            "mode": "stereo",
            "effective_output_key": SCARLETT,
            "hardware_playback_ports": list(AUX_PORTS),
        }
        self.assertTrue(self._recovery()._source_links_present("local", links, output_mode))

    def test_aux_topology_without_dsp_link_is_not_recognized(self):
        links = (
            "\tmpv:output_FL\n"
            f"  |-> {DSP_SINK}:playback_FL\n"
            "\tfxroute_dsp:output_1\n"
            f"  |-> {SCARLETT}:playback_AUX0\n"
        )
        output_mode = {
            "mode": "stereo",
            "effective_output_key": SCARLETT,
            "hardware_playback_ports": list(AUX_PORTS),
        }
        self.assertFalse(self._recovery()._source_links_present("local", links, output_mode))

    def test_semantic_topology_still_recognized_without_discovery(self):
        links = (
            "\tmpv:output_FL\n"
            f"  |-> {DSP_SINK}:playback_FL\n"
            "\tfxroute_dsp:output_1\n"
            f"  |-> {UMC}:playback_FL\n"
            "\tfxroute_dsp:output_2\n"
            f"  |-> {UMC}:playback_FR\n"
        )
        self.assertTrue(self._recovery()._source_links_present(
            "local", links, {"mode": "stereo", "effective_output_key": UMC}))


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
