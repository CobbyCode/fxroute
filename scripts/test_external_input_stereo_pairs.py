#!/usr/bin/env python3
"""External-input stereo-pair discovery, selection and routing tests.

Multichannel capture devices must be offered as adjacent stereo pairs
(Input 1-2, Input 3-4, ...) derived from the real PipeWire channel
designations; a lone mono channel must never appear as a full stereo input
or be duplicated onto both sides.
"""

import asyncio
import pathlib
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from audio.external_input import ExternalInputRouting, ExternalInputRoutingDependencies
import audio.samplerate.overview as overview_mod


UMC_NAME = "alsa_input.usb-BEHRINGER_UMC204HD_192k-00.analog-stereo"
FOURCH_NAME = "alsa_input.test-4ch.multichannel"
MONO_NAME = "alsa_input.test-mono.mono-fallback"
THREECH_NAME = "alsa_input.test-3ch.multichannel"


def _pactl_info(default_source: str = UMC_NAME) -> str:
    return f"Server Name: PulseAudio\nDefault Source: {default_source}\n"


def _short_line(source_id: int, name: str, spec: str, state: str = "RUNNING") -> str:
    return f"{source_id}\t{name}\tPipeWire\t{spec}\t{state}"


def _detailed_block(
    *,
    number: int,
    name: str,
    description: str,
    spec: str | None,
    channel_map: str | None,
    ports: list[str] | None = None,
    active_port: str | None = None,
) -> str:
    lines = [
        f"Source #{number}",
        f"\tName: {name}",
        f"\tDescription: {description}",
        "\tState: RUNNING",
    ]
    if spec is not None:
        lines.append(f"\tSample Specification: {spec}")
    if channel_map is not None:
        lines.append(f"\tChannel Map: {channel_map}")
    lines.append('\t\tdevice.description = "Test Device"')
    if ports:
        lines.append("\tPorts:")
        lines.extend(f"\t\t{port}" for port in ports)
    if active_port is not None:
        lines.append(f"\tActive Port: {active_port}")
    lines.append("\tFormats:")
    lines.append("\t\tpcm")
    return "\n".join(lines)


UMC_PORTS = ["analog-input-mic: Microphone (type: Mic, priority: 8700, availability unknown)"]


def _run_command_for(short: str, detailed: str, info: str | None = None):
    def _run(args: list[str]) -> str:
        if args[:2] == ["pactl", "info"]:
            return info if info is not None else _pactl_info()
        if args == ["pactl", "list", "sources", "short"]:
            return short
        if args == ["pactl", "list", "sources"]:
            return detailed
        raise AssertionError(f"unexpected command: {args}")

    return _run


def _bluetooth_stub() -> dict:
    return {"roles": {}, "receiver_session": {}}


def _overview_with(run_command, selected_input_key=None, mode="app-playback"):
    with patch.object(overview_mod, "_run_command", side_effect=run_command), patch.object(
        overview_mod, "get_bluetooth_audio_overview", return_value=_bluetooth_stub()
    ), patch.object(
        overview_mod, "_load_audio_source_selection",
        return_value={"mode": mode, "selected_input_key": selected_input_key},
    ):
        return overview_mod.get_audio_source_overview()


def _umc_fixture(*, spec="s32le 2ch 44100Hz", channel_map="front-left,front-right",
                 ports=None, active_port="analog-input-mic"):
    short = _short_line(100, UMC_NAME, spec)
    detailed = _detailed_block(
        number=100, name=UMC_NAME, description="UMC204HD 192k Analog Stereo",
        spec=spec, channel_map=channel_map,
        ports=UMC_PORTS if ports is None else ports, active_port=active_port,
    )
    return short, detailed


class StereoPairDiscoveryTests(unittest.TestCase):
    def test_two_channel_device_keeps_legacy_key(self):
        short, detailed = _umc_fixture()
        result = _overview_with(_run_command_for(short, detailed))
        self.assertEqual(len(result["inputs"]), 1)
        entry = result["inputs"][0]
        self.assertEqual(entry["key"], f"{UMC_NAME}::analog-input-mic")
        self.assertEqual(entry["source_key"], UMC_NAME)
        self.assertEqual(entry["port_key"], "analog-input-mic")
        self.assertEqual(entry["left_channel"], "FL")
        self.assertEqual(entry["right_channel"], "FR")
        self.assertEqual(entry["pair_channels"], [1, 2])
        self.assertEqual(entry["pair_label"], "Input 1\u20132")
        self.assertEqual(entry["pair_count"], 1)
        self.assertEqual(entry["channels"], 2)

    def test_four_channel_device_offers_two_pairs(self):
        short = _short_line(200, FOURCH_NAME, "s32le 4ch 48000Hz")
        detailed = _detailed_block(
            number=200, name=FOURCH_NAME, description="Test 4ch Interface",
            spec="s32le 4ch 48000Hz",
            channel_map="front-left,front-right,rear-left,rear-right",
        )
        result = _overview_with(_run_command_for(short, detailed))
        self.assertEqual(len(result["inputs"]), 2)
        first, second = result["inputs"]
        self.assertEqual(first["key"], f"{FOURCH_NAME}::pair:1-2")
        self.assertEqual(second["key"], f"{FOURCH_NAME}::pair:3-4")
        self.assertTrue(first["label"].endswith("\u2014 Input 1\u20132"))
        self.assertTrue(second["label"].endswith("\u2014 Input 3\u20134"))
        self.assertEqual((first["left_channel"], first["right_channel"]), ("FL", "FR"))
        self.assertEqual((second["left_channel"], second["right_channel"]), ("RL", "RR"))
        self.assertEqual(first["pair_count"], 2)

    def test_mono_source_is_not_offered_as_stereo(self):
        short = "\n".join([
            _short_line(100, UMC_NAME, "s32le 2ch 44100Hz"),
            _short_line(300, MONO_NAME, "s16le 1ch 44100Hz"),
        ])
        umc_short, umc_detailed = _umc_fixture()
        mono_detailed = _detailed_block(
            number=300, name=MONO_NAME, description="Test Mono Mic",
            spec="s16le 1ch 44100Hz", channel_map="mono",
        )
        result = _overview_with(_run_command_for(short, umc_detailed + "\n" + mono_detailed))
        keys = [item["key"] for item in result["inputs"]]
        self.assertEqual(keys, [f"{UMC_NAME}::analog-input-mic"])
        self.assertTrue(any("Mono" in note or "mono" in note for note in result["notes"]))

    def test_odd_channel_leftover_is_ignored_not_duplicated(self):
        short = _short_line(400, THREECH_NAME, "s32le 3ch 48000Hz")
        detailed = _detailed_block(
            number=400, name=THREECH_NAME, description="Test 3ch Interface",
            spec="s32le 3ch 48000Hz",
            channel_map="front-left,front-right,front-center",
        )
        result = _overview_with(_run_command_for(short, detailed))
        self.assertEqual(len(result["inputs"]), 1)
        entry = result["inputs"][0]
        self.assertEqual(entry["pair_channels"], [1, 2])
        self.assertEqual((entry["left_channel"], entry["right_channel"]), ("FL", "FR"))

    def test_missing_channel_map_falls_back_to_positional_order(self):
        short = _short_line(200, FOURCH_NAME, "s32le 4ch 48000Hz")
        detailed = _detailed_block(
            number=200, name=FOURCH_NAME, description="Test 4ch Interface",
            spec="s32le 4ch 48000Hz", channel_map=None,
        )
        result = _overview_with(_run_command_for(short, detailed))
        self.assertEqual(len(result["inputs"]), 2)
        self.assertEqual(
            [(item["left_channel"], item["right_channel"]) for item in result["inputs"]],
            [("FL", "FR"), ("RL", "RR")],
        )

    def test_unknown_channel_count_keeps_stereo_availability(self):
        short = _short_line(100, UMC_NAME, "")
        detailed = _detailed_block(
            number=100, name=UMC_NAME, description="UMC204HD 192k Analog Stereo",
            spec=None, channel_map=None,
            ports=UMC_PORTS, active_port="analog-input-mic",
        )
        result = _overview_with(_run_command_for(short, detailed))
        self.assertEqual(len(result["inputs"]), 1)
        self.assertEqual(
            (result["inputs"][0]["left_channel"], result["inputs"][0]["right_channel"]),
            ("FL", "FR"),
        )

    def test_pair_labels_carry_no_mic_line_semantics(self):
        short = _short_line(200, FOURCH_NAME, "s32le 4ch 48000Hz")
        detailed = _detailed_block(
            number=200, name=FOURCH_NAME, description="Test 4ch Interface",
            spec="s32le 4ch 48000Hz",
            channel_map="front-left,front-right,rear-left,rear-right",
        )
        result = _overview_with(_run_command_for(short, detailed))
        for item in result["inputs"]:
            lowered = f"{item['label']} {item['pair_label']}".lower()
            self.assertNotIn("mic", lowered)
            self.assertNotIn("line", lowered)


class SourceSelectionKeyTests(unittest.TestCase):
    def test_key_roundtrip_with_port_and_pair(self):
        key = overview_mod._build_source_selection_key(FOURCH_NAME, None, (3, 4))
        self.assertEqual(key, f"{FOURCH_NAME}::pair:3-4")
        source, port, pair = overview_mod._split_source_selection_key(key)
        self.assertEqual((source, port, pair), (FOURCH_NAME, None, (3, 4)))

    def test_key_roundtrip_with_alsa_port_and_pair(self):
        key = overview_mod._build_source_selection_key(UMC_NAME, "analog-input-mic", (1, 2))
        self.assertEqual(key, f"{UMC_NAME}::analog-input-mic::pair:1-2")
        source, port, pair = overview_mod._split_source_selection_key(key)
        self.assertEqual((source, port, pair), (UMC_NAME, "analog-input-mic", (1, 2)))

    def test_legacy_key_parses_without_pair(self):
        source, port, pair = overview_mod._split_source_selection_key(f"{UMC_NAME}::analog-input-mic")
        self.assertEqual((source, port, pair), (UMC_NAME, "analog-input-mic", None))
        source, port, pair = overview_mod._split_source_selection_key(UMC_NAME)
        self.assertEqual((source, port, pair), (UMC_NAME, None, None))

    def test_legacy_key_migrates_to_first_pair(self):
        short = _short_line(200, FOURCH_NAME, "s32le 4ch 48000Hz")
        detailed = _detailed_block(
            number=200, name=FOURCH_NAME, description="Test 4ch Interface",
            spec="s32le 4ch 48000Hz",
            channel_map="front-left,front-right,rear-left,rear-right",
        )
        result = _overview_with(
            _run_command_for(short, detailed), selected_input_key=FOURCH_NAME,
        )
        selected = result["selected_input"]
        self.assertIsNotNone(selected)
        self.assertEqual(selected["key"], f"{FOURCH_NAME}::pair:1-2")
        self.assertTrue(selected["is_selected"])

    def test_pair_selection_sets_only_alsa_port(self):
        short = _short_line(200, FOURCH_NAME, "s32le 4ch 48000Hz")
        detailed = _detailed_block(
            number=200, name=FOURCH_NAME, description="Test 4ch Interface",
            spec="s32le 4ch 48000Hz",
            channel_map="front-left,front-right,rear-left,rear-right",
        )
        run = _run_command_for(short, detailed)
        pair_key = f"{FOURCH_NAME}::pair:3-4"
        load_states = [
            {"mode": "app-playback", "selected_input_key": None},
            {"mode": "external-input", "selected_input_key": pair_key},
        ]
        with patch.object(overview_mod, "_run_command", side_effect=run), patch.object(
            overview_mod, "get_bluetooth_audio_overview", return_value=_bluetooth_stub()
        ), patch.object(
            overview_mod, "_load_audio_source_selection", side_effect=load_states,
        ), patch.object(overview_mod, "_set_source_port") as set_port, patch.object(
            overview_mod, "_save_audio_source_selection"
        ) as save_selection:
            result = overview_mod.set_audio_source_selection("external-input", pair_key)
        set_port.assert_not_called()
        save_selection.assert_called_once_with("external-input", pair_key)
        self.assertEqual(result["selected_input"]["key"], pair_key)

    def test_pair_selection_with_alsa_port_sets_that_port(self):
        short, detailed = _umc_fixture()
        run = _run_command_for(short, detailed)
        umc_key = f"{UMC_NAME}::analog-input-mic"
        with patch.object(overview_mod, "_run_command", side_effect=run), patch.object(
            overview_mod, "get_bluetooth_audio_overview", return_value=_bluetooth_stub()
        ), patch.object(
            overview_mod, "_load_audio_source_selection",
            return_value={"mode": "app-playback", "selected_input_key": None},
        ), patch.object(overview_mod, "_set_source_port") as set_port, patch.object(
            overview_mod, "_save_audio_source_selection"
        ):
            overview_mod.set_audio_source_selection("external-input", umc_key)
        set_port.assert_called_once_with(UMC_NAME, "analog-input-mic")


class ExternalInputRoutingTests(unittest.IsolatedAsyncioTestCase):
    def _routing(self, current_input: dict) -> ExternalInputRouting:
        overview = {"mode": "external-input", "selected_input": current_input}
        return ExternalInputRouting(ExternalInputRoutingDependencies(
            get_audio_source_overview=lambda: overview,
        ))

    async def test_sync_routes_selected_pair_to_dsp(self):
        routing = self._routing({
            "key": f"{FOURCH_NAME}::pair:3-4",
            "source_key": FOURCH_NAME,
            "left_channel": "RL",
            "right_channel": "RR",
        })
        with patch("audio.pw_link.connect_ports", new=AsyncMock()) as connect:
            await routing.sync()
        self.assertEqual(connect.await_count, 2)
        first, second = connect.await_args_list
        self.assertEqual(
            first.args,
            ((f"{FOURCH_NAME}:capture_RL", f"{FOURCH_NAME}:output_RL"),
             "fxroute_dsp_sink:playback_FL"),
        )
        self.assertEqual(
            second.args,
            ((f"{FOURCH_NAME}:capture_RR", f"{FOURCH_NAME}:output_RR"),
             "fxroute_dsp_sink:playback_FR"),
        )
        self.assertEqual(routing.loopback_selection_key, f"{FOURCH_NAME}::pair:3-4")

    async def test_sync_is_idempotent_for_same_pair(self):
        routing = self._routing({
            "key": f"{FOURCH_NAME}::pair:3-4",
            "source_key": FOURCH_NAME,
            "left_channel": "RL",
            "right_channel": "RR",
        })
        with patch("audio.pw_link.connect_ports", new=AsyncMock()) as connect:
            await routing.sync()
            await routing.sync()
        self.assertEqual(connect.await_count, 2)

    async def test_pair_switch_disconnects_previous_pair(self):
        first_input = {
            "key": f"{FOURCH_NAME}::pair:1-2",
            "source_key": FOURCH_NAME,
            "left_channel": "FL",
            "right_channel": "FR",
        }
        overview: dict = {"mode": "external-input", "selected_input": first_input}
        routing = ExternalInputRouting(ExternalInputRoutingDependencies(
            get_audio_source_overview=lambda: overview,
        ))
        with patch("audio.pw_link.connect_ports", new=AsyncMock()), patch(
            "audio.pw_link.disconnect_ports", new=AsyncMock()
        ) as disconnect:
            await routing.sync()
            overview["selected_input"] = {
                "key": f"{FOURCH_NAME}::pair:3-4",
                "source_key": FOURCH_NAME,
                "left_channel": "RL",
                "right_channel": "RR",
            }
            await routing.sync()
        disconnected_sources = [call.args[0] for call in disconnect.await_args_list]
        self.assertIn(
            (f"{FOURCH_NAME}:capture_FL", f"{FOURCH_NAME}:output_FL"), disconnected_sources,
        )
        self.assertIn(
            (f"{FOURCH_NAME}:capture_FR", f"{FOURCH_NAME}:output_FR"), disconnected_sources,
        )

    async def test_legacy_input_without_pair_info_routes_fl_fr(self):
        routing = self._routing({"source_key": UMC_NAME})
        with patch("audio.pw_link.connect_ports", new=AsyncMock()) as connect:
            await routing.sync()
        self.assertEqual(
            connect.await_args_list[0].args[0],
            (f"{UMC_NAME}:capture_FL", f"{UMC_NAME}:output_FL"),
        )
        self.assertEqual(
            connect.await_args_list[1].args[0],
            (f"{UMC_NAME}:capture_FR", f"{UMC_NAME}:output_FR"),
        )

    async def test_identical_channels_are_rejected_not_duplicated(self):
        routing = self._routing({
            "key": "mono-source",
            "source_key": "mono-source",
            "left_channel": "MONO",
            "right_channel": "MONO",
        })
        with patch("audio.pw_link.connect_ports", new=AsyncMock()) as connect:
            # MONO/MONO falls back to the FL/FR stereo pair instead of
            # duplicating one channel onto both sides.
            await routing.sync()
        self.assertEqual(connect.await_count, 2)
        self.assertNotEqual(
            connect.await_args_list[0].args[0], connect.await_args_list[1].args[0],
        )
        with self.assertRaisesRegex(RuntimeError, "distinct stereo channels"):
            await routing._ensure_loopback("mono-source", "MONO", "MONO")

    async def test_pair_rollback_disconnects_attempted_pair(self):
        routing = self._routing({
            "key": f"{FOURCH_NAME}::pair:3-4",
            "source_key": FOURCH_NAME,
            "left_channel": "RL",
            "right_channel": "RR",
        })
        connect = AsyncMock(side_effect=[None, RuntimeError("RR failed")])
        with patch("audio.pw_link.connect_ports", connect), patch(
            "audio.pw_link.disconnect_ports", new=AsyncMock()
        ) as disconnect:
            with self.assertRaisesRegex(RuntimeError, "RR failed"):
                await routing.sync()
        disconnected = [call.args[0] for call in disconnect.await_args_list]
        self.assertIn(
            (f"{FOURCH_NAME}:capture_RL", f"{FOURCH_NAME}:output_RL"), disconnected,
        )

    async def test_disable_clears_pair_state(self):
        routing = self._routing({
            "key": f"{FOURCH_NAME}::pair:3-4",
            "source_key": FOURCH_NAME,
            "left_channel": "RL",
            "right_channel": "RR",
        })
        with patch("audio.pw_link.connect_ports", new=AsyncMock()), patch(
            "audio.pw_link.disconnect_ports", new=AsyncMock()
        ):
            await routing.sync()
            await routing.disable()
        self.assertIsNone(routing.loopback_source_name)
        self.assertIsNone(routing.loopback_selection_key)


if __name__ == "__main__":
    unittest.main(verbosity=2)
