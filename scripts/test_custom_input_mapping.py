#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Custom input mapping: store, overview entry, routing, runtime links, API.

One mapping per audio device maps physical capture channels to program
positions and appears as a normal external input of the selected device.
"""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

import audio.samplerate.overview as overview_mod
from audio import custom_input_mapping as store
from audio.custom_mapping_api import CustomMappingApiDeps, register_custom_mapping_routes
from audio.external_input import ExternalInputRouting, ExternalInputRoutingDependencies
from audio.input_auto_select import InputAutoSelect, InputProbe, ProbeSpec, _is_digital_entry
from audio.source_feed import SourceOverviewFeed, SourceTransitionLock
from audio.source_transitions import SourceTransitionDependencies, SourceTransitions
from dsp.runtime import CommandResult, DSPRuntime, DSPRuntimeConfig, _contains_link, mapped_ingress_edges
from playback.orchestration import PlaybackOrchestrator

CARD = "usb-Focusrite_Scarlett_18i20-00"
PRO_SOURCE = f"alsa_input.{CARD}.pro-input-0"
ACP_SOURCE = f"alsa_input.{CARD}.multichannel-input"
PRO_SINK = f"alsa_output.{CARD}.pro-output-0"
OTHER_SINK = "alsa_output.usb-Other_Device-00.analog-stereo"
AUX_MAP = ",".join(f"aux{index}" for index in range(8))
ACP_MAP = "front-left,front-right,front-center,lfe,rear-left,rear-right,side-left,side-right"
MAPPING = {"name": "External Upmixer", "channels": {
    "FL": 3, "FR": 4, "FC": 5, "LFE": 6, "SL": 7, "SR": 8, "BL": 1, "BR": 2}}


def _short(source_id, name, spec):
    return f"{source_id}\t{name}\tPipeWire\t{spec}\tRUNNING"


def _detailed(number, name, spec, channel_map):
    return "\n".join([
        f"Source #{number}", f"\tName: {name}", "\tDescription: Scarlett 18i20", "\tState: RUNNING",
        f"\tSample Specification: {spec}", f"\tChannel Map: {channel_map}",
        '\t\tdevice.description = "Scarlett 18i20"', "\tFormats:", "\t\tpcm",
    ])


def _overview(*, source=PRO_SOURCE, channel_map=AUX_MAP, channels=8, mappings=None,
              output_selection=PRO_SINK, default_sink=PRO_SINK, selected_key=None, mode="app-playback"):
    spec = f"s32le {channels}ch 48000Hz"
    return _overview_of(
        _short(100, source, spec), _detailed(100, source, spec, channel_map),
        source=source, mappings={CARD: MAPPING} if mappings is None else mappings,
        output_selection=output_selection, default_sink=default_sink, selected_key=selected_key, mode=mode)


def _overview_of(short, detailed, *, source, mappings, output_selection, default_sink, selected_key, mode):
    def run(args):
        if args[:2] == ["pactl", "info"]:
            return f"Default Source: {source}\nDefault Sink: {default_sink}\n"
        if args == ["pactl", "list", "sources", "short"]:
            return short
        if args == ["pactl", "list", "sources"]:
            return detailed
        raise AssertionError(f"unexpected command: {args}")

    stored = mappings
    with patch.object(overview_mod, "_run_command", side_effect=run), patch.object(
        overview_mod, "get_bluetooth_audio_overview", return_value={"roles": {}, "receiver_session": {}}
    ), patch.object(
        overview_mod, "_load_audio_source_selection",
        return_value={"mode": mode, "selected_input_key": selected_key},
    ), patch.object(
        overview_mod, "_load_audio_output_selection", return_value={"selected_key": output_selection}
    ), patch.object(overview_mod, "_load_custom_input_mappings", return_value=stored):
        return overview_mod.get_audio_source_overview()


def _custom_entry(overview):
    return next((entry for entry in overview["inputs"] if entry["key"].startswith(store.INPUT_KEY_PREFIX)), None)


class CardBaseTests(unittest.TestCase):
    def test_profile_suffix_is_dropped_for_sink_and_source(self):
        self.assertEqual(store.card_base(PRO_SINK), CARD)
        self.assertEqual(store.card_base(PRO_SOURCE), CARD)
        self.assertEqual(store.card_base(ACP_SOURCE), CARD)

    def test_ucm_and_pci_names(self):
        self.assertEqual(store.card_base("alsa_output.usb-Dev-00.HiFi__Line1__sink"), "usb-Dev-00")
        self.assertEqual(store.card_base("alsa_output.usb-Dev-00.HiFi__combined"), "usb-Dev-00")
        self.assertEqual(store.card_base("alsa_input.pci-0000_00_1f.3.analog-stereo"), "pci-0000_00_1f.3")

    def test_non_alsa_nodes_have_no_base(self):
        for name in (None, "", "bluez_input.AA_BB.1", "alsa_input.nodots", "fxroute_dsp_sink"):
            self.assertIsNone(store.card_base(name))

    def test_key_round_trip(self):
        self.assertEqual(store.card_base_from_key(store.input_key(CARD)), CARD)
        self.assertIsNone(store.card_base_from_key("alsa_input.x::pair:1-2"))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.dict(os.environ, {"XDG_CONFIG_HOME": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_round_trip_update_and_delete(self):
        self.assertEqual(store.load_mappings(), {})
        store.save_mapping(CARD, MAPPING)
        self.assertEqual(store.load_mappings(), {CARD: MAPPING})
        changed = {"name": "", "channels": {"FL": 2, "FR": 1}}
        store.save_mapping(CARD, changed)
        self.assertEqual(store.load_mappings(), {CARD: changed})
        self.assertTrue(store.delete_mapping(CARD))
        self.assertFalse(store.delete_mapping(CARD))
        self.assertEqual(store.load_mappings(), {})

    def test_one_mapping_per_device(self):
        other = "usb-Other-00"
        store.save_mapping(CARD, MAPPING)
        store.save_mapping(other, {"name": "x", "channels": {"FL": 1, "FR": 2}})
        self.assertEqual(set(store.load_mappings()), {CARD, other})
        store.delete_mapping(other)
        self.assertEqual(set(store.load_mappings()), {CARD})

    def test_unusable_files_and_entries_are_tolerated(self):
        path = store.storage_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        for text in ("", "not json", "[]", '{"mappings": []}', '{"mappings": {"a": 3}}'):
            path.write_text(text)
            self.assertEqual(store.load_mappings(), {})
        path.write_text('{"mappings": {"good": {"name": "n", "channels": {"FL": 1, "FR": 2}},'
                        ' "no-fr": {"name": "n", "channels": {"FL": 1}},'
                        ' "dupe": {"name": "n", "channels": {"FL": 1, "FR": 1}},'
                        ' "bad-pos": {"name": "n", "channels": {"FL": 1, "FR": 2, "XX": 3}}}}')
        self.assertEqual(store.load_mappings(), {"good": {"name": "n", "channels": {"FL": 1, "FR": 2}}})

    def test_validate_normalizes_and_orders_positions(self):
        result = store.validate("  Up  ", {"BR": 4, "FR": 2, "FL": 1, "LFE": None}, 4)
        self.assertEqual(result, {"name": "Up", "channels": {"FL": 1, "FR": 2, "BR": 4}})
        self.assertEqual(list(result["channels"]), ["FL", "FR", "BR"])

    def test_validate_rejections(self):
        base = {"FL": 1, "FR": 2}
        cases = [
            ((5, base), "Name must be a string"),
            (("x" * 41, base), "at most 40"),
            (("bad\nname", base), "control characters"),
            (("", [1, 2]), "object"),
            (("", {**base, "XX": 3}), "Unknown channel"),
            (("", {"FL": 1}), "Front R must be mapped"),
            (("", {"FR": 1}), "Front L must be mapped"),
            (("", {}), "Front L and Front R must be mapped"),
            (("", {"FL": "1", "FR": 2}), "whole number"),
            (("", {"FL": True, "FR": 2}), "whole number"),
            (("", {"FL": 0, "FR": 2}), "not available"),
            (("", {"FL": 1, "FR": 9}), "not available"),
            (("", {"FL": 1, "FR": 2, "FC": 2}), "assigned to both"),
        ]
        for (name, channels), message in cases:
            with self.subTest(channels=channels, name=name), self.assertRaisesRegex(ValueError, message):
                store.validate(name, channels, 8)


class OverviewEntryTests(unittest.TestCase):
    def test_mapping_appears_as_normal_external_input_of_the_selected_device(self):
        overview = _overview()
        entry = _custom_entry(overview)
        self.assertEqual(entry["key"], f"custom-mapping::{CARD}")
        self.assertEqual(entry["label"], "External Upmixer")
        self.assertEqual(entry["source_key"], PRO_SOURCE)
        self.assertIsNone(entry["port_key"])
        self.assertIsNone(entry["pair_label"])
        self.assertEqual((entry["left_channel"], entry["right_channel"]), ("AUX2", "AUX3"))
        self.assertEqual(entry["pair_channels"], [3, 4])
        self.assertNotIn("right_source", entry)
        self.assertEqual(entry["custom_mapping"]["suffixes"]["BL"], "AUX0")
        self.assertEqual(set(entry["custom_mapping"]["sources"].values()), {PRO_SOURCE})
        self.assertEqual(entry["custom_mapping"]["channels"], MAPPING["channels"])
        self.assertTrue(entry["selectable"])
        self.assertTrue(entry["is_active_port"])
        self.assertFalse(entry["is_default"])
        # The ordinary stereo pairs of the same device stay offered.
        self.assertEqual(len([e for e in overview["inputs"] if e is not entry]), 4)
        self.assertEqual(overview["inputs"][-1], entry)

    def test_selected_mapping_is_the_selected_and_available_input(self):
        key = f"custom-mapping::{CARD}"
        overview = _overview(selected_key=key, mode="external-input")
        self.assertEqual(overview["selected_input"]["key"], key)
        self.assertTrue(overview["selected_input"]["is_selected"])
        self.assertEqual(overview["external_input_state"], "available")
        self.assertEqual([e["key"] for e in overview["inputs"] if e["is_selected"]], [key])

    def test_empty_name_falls_back_to_default_label(self):
        entry = _custom_entry(_overview(mappings={CARD: {"name": "", "channels": {"FL": 1, "FR": 2}}}))
        self.assertEqual(entry["label"], "Custom mapping")

    def test_key_survives_a_profile_switch_and_follows_the_live_suffixes(self):
        pro = _custom_entry(_overview())
        acp = _custom_entry(_overview(source=ACP_SOURCE, channel_map=ACP_MAP))
        self.assertEqual(pro["key"], acp["key"])
        self.assertEqual(acp["source_key"], ACP_SOURCE)
        self.assertEqual((acp["left_channel"], acp["right_channel"]), ("FC", "LFE"))
        self.assertEqual(acp["custom_mapping"]["suffixes"]["FC"], "RL")

    def test_no_entry_while_another_device_is_selected(self):
        overview = _overview(output_selection=OTHER_SINK, default_sink=PRO_SINK)
        self.assertIsNone(_custom_entry(overview))
        self.assertEqual(overview["custom_mapping_context"]["device"]["card_base"], "usb-Other_Device-00")
        self.assertIsNone(overview["custom_mapping_context"]["mapping"])

    def test_default_sink_selects_the_device_without_a_saved_output(self):
        self.assertIsNotNone(_custom_entry(_overview(output_selection=None, default_sink=PRO_SINK)))
        self.assertIsNone(_custom_entry(_overview(output_selection=None, default_sink=OTHER_SINK)))

    def test_no_mapping_means_no_input(self):
        overview = _overview(mappings={})
        self.assertIsNone(_custom_entry(overview))
        self.assertIsNone(overview["custom_mapping_context"]["mapping"])
        self.assertIsNone(overview["custom_mapping_context"]["input_key"])

    def test_mapping_beyond_live_channel_count_is_hidden_with_a_note(self):
        overview = _overview(source=PRO_SOURCE, channel_map="aux0,aux1,aux2,aux3", channels=4)
        self.assertIsNone(_custom_entry(overview))
        self.assertTrue(any("needs 8 input channels" in note for note in overview["notes"]))
        # The dialog still sees the stored mapping and the live channels.
        context = overview["custom_mapping_context"]
        self.assertEqual(len(context["capture"]["channels"]), 4)
        self.assertEqual(context["mapping"]["name"], "External Upmixer")

    def test_context_describes_device_capture_and_mapping(self):
        context = _overview()["custom_mapping_context"]
        self.assertEqual(context["device"], {"card_base": CARD, "label": "Scarlett 18i20"})
        self.assertEqual(context["capture"]["channels"][2], {"index": 3, "suffix": "AUX2", "source": PRO_SOURCE})
        self.assertEqual(context["input_key"], f"custom-mapping::{CARD}")

    def test_user_name_never_feeds_digital_detection(self):
        entry = _custom_entry(_overview(mappings={CARD: {
            "name": "S/PDIF optical iec958", "channels": MAPPING["channels"]}}))
        self.assertEqual(entry["label"], "S/PDIF optical iec958")
        self.assertFalse(_is_digital_entry(entry))

    def test_saved_but_absent_mapping_keeps_its_label_as_a_missing_input(self):
        key = f"custom-mapping::{CARD}"
        overview = _overview(output_selection=OTHER_SINK, default_sink=OTHER_SINK,
                             selected_key=key, mode="external-input")
        selected = overview["selected_input"]
        self.assertEqual((selected["key"], selected["label"], selected["available"]),
                         (key, "External Upmixer", False))
        self.assertIn(overview["external_input_state"], {"unconfirmed", "unavailable"})


UMC = "usb-BEHRINGER_UMC204HD_192k-00"
UMC_COMBINED = f"alsa_output.{UMC}.HiFi__combined"
UMC_MAPPING = {"name": "Line in", "channels": {"FL": 1, "FR": 2}}


def _umc_mic(number):
    return f"alsa_input.{UMC}.HiFi__Mic{number}__source"


def _mono_detailed(source_id, name, description, card_name='"UMC204HD 192k"'):
    lines = [
        f"Source #{source_id}", f"\tName: {name}", f"\tDescription: {description}", "\tState: SUSPENDED",
        "\tSample Specification: float32le 1ch 44100Hz", "\tChannel Map: mono", '\t\talsa.card = "1"',
    ]
    if card_name:
        lines.append(f"\t\talsa.card_name = {card_name}")
    return "\n".join(lines + ["\tFormats:", "\t\tpcm"])


def _umc_overview(*, numbers=(1, 2), mappings=None, selected_key=None, mode="app-playback", card_name=True,
                  extra_short=(), extra_detailed=()):
    """An interface whose inputs are separate mono nodes (UCM HiFi style)."""
    short, detailed = list(extra_short), list(extra_detailed)
    for offset, number in enumerate(numbers):
        name = _umc_mic(number)
        short.append(_short(200 + offset, name, "float32le 1ch 44100Hz"))
        detailed.append(_mono_detailed(
            200 + offset, name, f"UMC204HD 192k Input {number}", '"UMC204HD 192k"' if card_name else None))
    return _overview_of(
        "\n".join(short), "\n\n".join(detailed), source=_umc_mic(numbers[0]),
        mappings={UMC: UMC_MAPPING} if mappings is None else mappings,
        output_selection=UMC_COMBINED, default_sink=UMC_COMBINED, selected_key=selected_key, mode=mode)


class MonoNodeCaptureTests(unittest.TestCase):
    """One card whose physical inputs are spread over several mono capture nodes."""

    def test_mono_nodes_of_one_card_are_its_numbered_physical_inputs(self):
        overview = _umc_overview(mappings={})
        context = overview["custom_mapping_context"]
        self.assertEqual(context["device"], {"card_base": UMC, "label": "UMC204HD 192k"})
        self.assertEqual(context["capture"]["channels"], [
            {"index": 1, "suffix": "MONO", "source": _umc_mic(1)},
            {"index": 2, "suffix": "MONO", "source": _umc_mic(2)}])
        # A mono node is still no stereo input of its own.
        self.assertEqual(overview["inputs"], [])
        self.assertTrue(any("Mono capture without a stereo pair" in note for note in overview["notes"]))

    def test_mapping_across_nodes_is_one_input_whose_right_side_has_its_own_node(self):
        entry = _custom_entry(_umc_overview())
        self.assertEqual(entry["key"], f"custom-mapping::{UMC}")
        self.assertEqual(entry["source_key"], _umc_mic(1))
        self.assertEqual(entry["right_source"], _umc_mic(2))
        self.assertEqual((entry["left_channel"], entry["right_channel"]), ("MONO", "MONO"))
        self.assertEqual(entry["pair_channels"], [1, 2])
        self.assertEqual(entry["custom_mapping"]["sources"], {"FL": _umc_mic(1), "FR": _umc_mic(2)})
        self.assertEqual(entry["device_label"], "UMC204HD 192k")
        self.assertFalse(_is_digital_entry(entry))

    def test_swapped_mapping_follows_the_inputs(self):
        entry = _custom_entry(_umc_overview(mappings={UMC: {"name": "", "channels": {"FL": 2, "FR": 1}}}))
        self.assertEqual((entry["source_key"], entry["right_source"]), (_umc_mic(2), _umc_mic(1)))

    def test_inputs_are_numbered_in_natural_node_order_not_listing_order(self):
        overview = _umc_overview(numbers=(10, 2, 1), mappings={})
        sources = [item["source"] for item in overview["custom_mapping_context"]["capture"]["channels"]]
        self.assertEqual(sources, [_umc_mic(1), _umc_mic(2), _umc_mic(10)])

    def test_a_missing_node_hides_the_mapping_with_a_note(self):
        overview = _umc_overview(numbers=(1,))
        self.assertIsNone(_custom_entry(overview))
        self.assertTrue(any("needs 2 input channels" in note for note in overview["notes"]))
        self.assertEqual(len(overview["custom_mapping_context"]["capture"]["channels"]), 1)

    def test_nodes_of_other_cards_are_not_mixed_in(self):
        other = "alsa_input.usb-Other_Device-00.analog-stereo"
        overview = _umc_overview(
            mappings={}, extra_short=(_short(300, other, "s16le 2ch 48000Hz"),),
            extra_detailed=(_detailed(300, other, "s16le 2ch 48000Hz", "front-left,front-right"),))
        self.assertEqual({item["source"] for item in overview["custom_mapping_context"]["capture"]["channels"]},
                         {_umc_mic(1), _umc_mic(2)})
        self.assertEqual([item["source_key"] for item in overview["inputs"]], [other])

    def test_label_falls_back_to_the_first_node_without_a_card_name(self):
        context = _umc_overview(mappings={}, card_name=False)["custom_mapping_context"]
        self.assertEqual(context["device"]["label"], "UMC204HD 192k Input 1")

    def test_a_single_node_card_keeps_its_node_description_as_label(self):
        self.assertEqual(_overview()["custom_mapping_context"]["device"]["label"], "Scarlett 18i20")


def _routing(overview, mode, **deps):
    return ExternalInputRouting(ExternalInputRoutingDependencies(
        get_audio_source_overview=lambda: overview, get_output_mode=lambda: mode, **deps))


def _selected_overview():
    overview = _overview(selected_key=f"custom-mapping::{CARD}", mode="external-input")
    return overview


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_stereo_modes_use_the_normal_loopback_with_the_mapped_pair(self):
        for mode in ("stereo", "stereo-sub"):
            with self.subTest(mode=mode):
                routing = _routing(_selected_overview(), mode)
                connect = AsyncMock()
                with patch("audio.pw_link.connect_ports", connect), patch(
                        "audio.pw_link.disconnect_ports", AsyncMock()), patch(
                        "audio.external_input.input_links_present", AsyncMock(return_value=True)):
                    await routing.sync()
                self.assertEqual(
                    [(call.args[0][0], call.args[1]) for call in connect.await_args_list],
                    [(f"{PRO_SOURCE}:capture_AUX2", "fxroute_dsp_sink:playback_FL"),
                     (f"{PRO_SOURCE}:capture_AUX3", "fxroute_dsp_sink:playback_FR")])
                self.assertEqual(routing.loopback_source_name, PRO_SOURCE)
                self.assertIsNone(routing.mapped_source_name)
                self.assertEqual(routing.active_channels, ("AUX2", "AUX3"))

    async def test_surround_links_every_mapped_channel_directly_without_the_decoder(self):
        sync_mapped, stop_mapped = AsyncMock(), AsyncMock()
        digital = AsyncMock()
        routing = _routing(_selected_overview(), "surround", sync_mapped_input=sync_mapped,
                           stop_mapped_input=stop_mapped, sync_digital_input=digital,
                           stop_digital_input=AsyncMock())
        with patch("audio.pw_link.connect_ports", AsyncMock(side_effect=AssertionError("sink loopback used"))):
            await routing.sync()
            await routing.sync()
        digital.assert_not_awaited()
        expected = (
            (f"{PRO_SOURCE}:capture_AUX2", "fxroute_dsp:input_1"),
            (f"{PRO_SOURCE}:capture_AUX3", "fxroute_dsp:input_2"),
            (f"{PRO_SOURCE}:capture_AUX4", "fxroute_dsp:input_3"),
            (f"{PRO_SOURCE}:capture_AUX5", "fxroute_dsp:input_4"),
            (f"{PRO_SOURCE}:capture_AUX6", "fxroute_dsp:input_5"),
            (f"{PRO_SOURCE}:capture_AUX7", "fxroute_dsp:input_6"),
            (f"{PRO_SOURCE}:capture_AUX0", "fxroute_dsp:input_7"),
            (f"{PRO_SOURCE}:capture_AUX1", "fxroute_dsp:input_8"),
        )
        self.assertEqual([call.args[0] for call in sync_mapped.await_args_list], [expected, expected])
        self.assertEqual(routing.mapped_source_name, PRO_SOURCE)
        self.assertEqual(routing.mapped_ingress_links, expected)
        self.assertIsNone(routing.loopback_source_name)
        await routing.disable()
        stop_mapped.assert_awaited_once()
        self.assertIsNone(routing.mapped_source_name)
        self.assertEqual(routing.mapped_ingress_links, ())

    async def test_unmapped_positions_are_left_unlinked(self):
        overview = _overview(
            mappings={CARD: {"name": "", "channels": {"FL": 1, "FR": 2}}},
            selected_key=f"custom-mapping::{CARD}", mode="external-input")
        sync_mapped = AsyncMock()
        routing = _routing(overview, "surround", sync_mapped_input=sync_mapped, stop_mapped_input=AsyncMock())
        await routing.sync()
        self.assertEqual(len(sync_mapped.await_args.args[0]), 2)

    async def test_switching_from_a_mapped_to_a_digital_input_stops_the_mapped_ingress(self):
        overview = _selected_overview()
        stop_mapped, digital = AsyncMock(), AsyncMock()
        routing = _routing(overview, "surround", sync_mapped_input=AsyncMock(), stop_mapped_input=stop_mapped,
                           sync_digital_input=digital, stop_digital_input=AsyncMock())
        await routing.sync()
        tv = {"key": "scarlett::pair:9-10", "source_key": "scarlett", "alsa_card": 3,
              "left_channel": "AUX8", "right_channel": "AUX9"}
        await routing.sync({**overview, "selected_input": tv})
        stop_mapped.assert_awaited_once()
        digital.assert_awaited_once()
        self.assertIsNone(routing.mapped_source_name)
        self.assertEqual(routing.digital_source_name, "scarlett")

    async def test_leaving_surround_replaces_the_mapped_ingress_by_the_loopback(self):
        mode = {"value": "surround"}
        overview = _selected_overview()
        stop_mapped = AsyncMock()
        routing = ExternalInputRouting(ExternalInputRoutingDependencies(
            get_audio_source_overview=lambda: overview, get_output_mode=lambda: mode["value"],
            sync_mapped_input=AsyncMock(), stop_mapped_input=stop_mapped))
        await routing.sync()
        mode["value"] = "stereo"
        with patch("audio.pw_link.connect_ports", AsyncMock()), patch(
                "audio.pw_link.disconnect_ports", AsyncMock()), patch(
                "audio.external_input.input_links_present", AsyncMock(return_value=True)):
            await routing.sync()
        stop_mapped.assert_awaited_once()
        self.assertIsNone(routing.mapped_source_name)
        self.assertEqual(routing.loopback_source_name, PRO_SOURCE)

    async def test_failed_mapped_sync_stops_and_clears_the_state(self):
        stop_mapped = AsyncMock()
        routing = _routing(_selected_overview(), "surround",
                           sync_mapped_input=AsyncMock(side_effect=RuntimeError("links failed")),
                           stop_mapped_input=stop_mapped)
        with self.assertRaisesRegex(RuntimeError, "links failed"):
            await routing.sync()
        stop_mapped.assert_awaited_once()
        self.assertIsNone(routing.mapped_source_name)
        self.assertEqual(routing.mapped_ingress_links, ())

    async def test_missing_surround_dependency_fails_closed(self):
        routing = _routing(_selected_overview(), "surround")
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            await routing.sync()


def _umc_selected(**kwargs):
    return _umc_overview(selected_key=f"custom-mapping::{UMC}", mode="external-input", **kwargs)


class SplitNodeRoutingTests(unittest.IsolatedAsyncioTestCase):
    """Front L and Front R on different capture nodes (one mono node per input)."""

    FL_LINK = (f"{_umc_mic(1)}:capture_MONO", "fxroute_dsp_sink:playback_FL")
    FR_LINK = (f"{_umc_mic(2)}:capture_MONO", "fxroute_dsp_sink:playback_FR")

    async def test_stereo_loopback_links_each_side_from_its_own_node(self):
        for mode in ("stereo", "stereo-sub"):
            with self.subTest(mode=mode):
                routing = _routing(_umc_selected(), mode)
                connect, disconnect = AsyncMock(), AsyncMock()
                present = AsyncMock(return_value=True)
                with patch("audio.pw_link.connect_ports", connect), patch(
                        "audio.pw_link.disconnect_ports", disconnect), patch(
                        "audio.external_input.input_links_present", present):
                    await routing.sync()
                    await routing.sync()
                    self.assertEqual(
                        [(c.args[0][0], c.args[1]) for c in connect.await_args_list], [self.FL_LINK, self.FR_LINK],
                        "the second sync finds both links and does not reconnect")
                    self.assertIn(call(_umc_mic(1), (("MONO", "FL"),)), present.await_args_list)
                    self.assertIn(call(_umc_mic(2), (("MONO", "FR"),)), present.await_args_list)
                    self.assertEqual(routing.loopback_source_name, _umc_mic(1))
                    self.assertEqual(routing.active_right_source, _umc_mic(2))
                    self.assertEqual(routing.active_channels, ("MONO", "MONO"))
                    await routing.disable()
                self.assertEqual(
                    [(c.args[0][0], c.args[1]) for c in disconnect.await_args_list], [self.FL_LINK, self.FR_LINK])
                self.assertIsNone(routing.active_right_source)

    async def test_a_missing_right_link_is_relinked(self):
        routing = _routing(_umc_selected(), "stereo")
        connect = AsyncMock()
        present = AsyncMock(return_value=True)
        with patch("audio.pw_link.connect_ports", connect), patch("audio.pw_link.disconnect_ports", AsyncMock()), \
                patch("audio.external_input.input_links_present", present):
            await routing.sync()
            present.side_effect = lambda source, channels: source != _umc_mic(2)
            with self.assertRaisesRegex(RuntimeError, "links missing"):
                await routing.sync()
        self.assertGreater(len(connect.await_args_list), 2, "the lost link triggered a reconnect")

    async def test_a_failed_right_link_unlinks_both_legs(self):
        routing = _routing(_umc_selected(), "stereo")
        connect = AsyncMock(side_effect=[None, RuntimeError("no right node")])
        disconnect = AsyncMock()
        with patch("audio.pw_link.connect_ports", connect), patch("audio.pw_link.disconnect_ports", disconnect):
            with self.assertRaisesRegex(RuntimeError, "no right node"):
                await routing.sync()
        self.assertEqual(
            [(c.args[0][0], c.args[1]) for c in disconnect.await_args_list], [self.FL_LINK, self.FR_LINK])
        self.assertIsNone(routing.loopback_source_name)
        self.assertIsNone(routing.active_right_source)

    async def test_changing_to_a_single_node_input_drops_the_right_source(self):
        routing = _routing(_umc_selected(), "stereo")
        with patch("audio.pw_link.connect_ports", AsyncMock()), patch(
                "audio.pw_link.disconnect_ports", AsyncMock()), patch(
                "audio.external_input.input_links_present", AsyncMock(return_value=True)):
            await routing.sync()
            await routing.sync(_selected_overview())
        self.assertEqual(routing.loopback_source_name, PRO_SOURCE)
        self.assertIsNone(routing.active_right_source)
        self.assertEqual(routing.active_channels, ("AUX2", "AUX3"))

    async def test_surround_links_every_mapped_channel_from_its_node(self):
        sync_mapped = AsyncMock()
        routing = _routing(_umc_selected(), "surround", sync_mapped_input=sync_mapped,
                           stop_mapped_input=AsyncMock())
        await routing.sync()
        self.assertEqual(sync_mapped.await_args.args[0], (
            (f"{_umc_mic(1)}:capture_MONO", "fxroute_dsp:input_1"),
            (f"{_umc_mic(2)}:capture_MONO", "fxroute_dsp:input_2")))
        self.assertEqual(routing.mapped_source_name, _umc_mic(1))

    def test_one_channel_on_both_sides_of_one_node_still_collapses(self):
        from audio.external_input import _pair_channels
        mono = {"left_channel": "MONO", "right_channel": "MONO"}
        self.assertEqual(_pair_channels(mono), ("FL", "FR"))
        self.assertEqual(_pair_channels(mono, right_source="other"), ("MONO", "MONO"))

    async def test_loopback_rejects_one_channel_of_one_node_on_both_sides(self):
        routing = _routing(_umc_selected(), "stereo")
        with self.assertRaisesRegex(RuntimeError, "two distinct"):
            await routing._ensure_loopback(_umc_mic(1), "MONO", "MONO", right_source=_umc_mic(1))


class SplitNodeProbeTests(unittest.IsolatedAsyncioTestCase):
    def _spec(self, entry):
        return InputAutoSelect._probe_spec(SimpleNamespace(_capture_rate=48000), entry)

    def _entry(self, **extra):
        return {"source_key": _umc_mic(1), "left_channel": "MONO", "right_channel": "MONO",
                "is_active_port": True, **extra}

    def test_spec_names_the_right_node_when_it_differs(self):
        spec = self._spec(self._entry(right_source=_umc_mic(2)))
        self.assertEqual((spec.source_name, spec.right_source), (_umc_mic(1), _umc_mic(2)))

    def test_one_channel_of_one_node_has_no_probe(self):
        self.assertIsNone(self._spec(self._entry()))
        self.assertIsNone(self._spec(self._entry(right_source=_umc_mic(1))))

    def test_single_node_pair_is_unchanged(self):
        spec = self._spec({"source_key": PRO_SOURCE, "left_channel": "AUX2", "right_channel": "AUX3"})
        self.assertEqual((spec.left_channel, spec.right_channel, spec.right_source), ("AUX2", "AUX3", None))

    async def test_probe_links_each_side_from_its_own_node(self):
        spec = ProbeSpec(_umc_mic(1), "MONO", "MONO", 48000, None, False, right_source=_umc_mic(2))
        connect = AsyncMock()
        with patch("audio.pw_link.connect_ports", connect):
            await InputProbe(spec, SimpleNamespace())._link(SimpleNamespace(returncode=None), "probe_1")
        self.assertEqual([(c.args[0][0], c.args[1]) for c in connect.await_args_list], [
            (f"{_umc_mic(1)}:capture_MONO", "probe_1:input_FL"),
            (f"{_umc_mic(2)}:capture_MONO", "probe_1:input_FR")])

    async def test_probe_of_a_single_node_pair_links_both_sides_from_that_node(self):
        spec = ProbeSpec(PRO_SOURCE, "AUX2", "AUX3", 48000, None, False)
        connect = AsyncMock()
        with patch("audio.pw_link.connect_ports", connect):
            await InputProbe(spec, SimpleNamespace())._link(SimpleNamespace(returncode=None), "probe_1")
        self.assertEqual([c.args[0][0] for c in connect.await_args_list],
                         [f"{PRO_SOURCE}:capture_AUX2", f"{PRO_SOURCE}:capture_AUX3"])


class _Graph:
    """Fake pw-link graph for the runtime."""

    def __init__(self, missing_sources=()):
        self.links = set()
        self.missing_sources = tuple(missing_sources)
        self.calls = []

    async def run(self, args):
        self.calls.append(tuple(args))
        if args == ("pw-link", "-l"):
            return CommandResult(0, "\n".join(f"{a} -> {b}" for a, b in sorted(self.links)))
        if args[1] == "-d":
            self.links.discard(tuple(args[2:]))
            return CommandResult(0)
        if any(args[1].startswith(prefix) for prefix in self.missing_sources):
            return CommandResult(1, "", "failed to link ports: No such file or directory")
        self.links.add(tuple(args[1:]))
        return CommandResult(0)


def _native(graph):
    native = DSPRuntime(SimpleNamespace(), command_runner=graph.run)
    native._process = SimpleNamespace(returncode=None)
    native._config = DSPRuntimeConfig("surround", "A", 48000, ("playback_AUX0",), ())
    return native


def _mapped_links(graph):
    return {link for link in graph.links if link[0].startswith("alsa_input.")}


SUFFIXES = {"FL": "AUX2", "FR": "AUX3", "FC": "AUX4", "LFE": "AUX5"}


class RuntimeIngressTests(unittest.IsolatedAsyncioTestCase):
    def test_edges_follow_the_program_position_order(self):
        self.assertEqual(mapped_ingress_edges("src", {"FR": "AUX1", "BR": "AUX7"}), (
            ("src:capture_AUX1", "fxroute_dsp:input_2"), ("src:capture_AUX7", "fxroute_dsp:input_8")))

    def test_edges_take_each_positions_node_from_sources(self):
        self.assertEqual(
            mapped_ingress_edges("a", {"FL": "MONO", "FR": "MONO", "FC": "AUX3"}, {"FR": "b", "FC": "c"}),
            (("a:capture_MONO", "fxroute_dsp:input_1"), ("b:capture_MONO", "fxroute_dsp:input_2"),
             ("c:capture_AUX3", "fxroute_dsp:input_3")))

    async def test_mapped_links_are_set_replaced_and_cleared(self):
        graph = _Graph()
        native = _native(graph)
        await native.set_mapped_ingress(mapped_ingress_edges("alsa_input.A", SUFFIXES))
        self.assertEqual(_mapped_links(graph), set(mapped_ingress_edges("alsa_input.A", SUFFIXES)))
        self.assertIn(("fxroute_dsp_sink:monitor_FL", "fxroute_dsp:input_1"), graph.links)
        self.assertEqual(len(native.snapshot()["mapped_ingress"]), 4)
        changed = {"FL": "AUX0", "FR": "AUX1"}
        await native.set_mapped_ingress(mapped_ingress_edges("alsa_input.B", changed))
        self.assertEqual(_mapped_links(graph), set(mapped_ingress_edges("alsa_input.B", changed)))
        await native.set_mapped_ingress(())
        self.assertEqual(_mapped_links(graph), set())
        self.assertIn(("fxroute_dsp_sink:monitor_FR", "fxroute_dsp:input_2"), graph.links)

    async def test_unrelated_links_into_the_engine_are_not_pruned(self):
        graph = _Graph()
        graph.links.add(("someone_else:out", "fxroute_dsp:input_3"))
        native = _native(graph)
        await native.set_mapped_ingress(mapped_ingress_edges("alsa_input.A", SUFFIXES))
        await native.set_mapped_ingress(())
        self.assertIn(("someone_else:out", "fxroute_dsp:input_3"), graph.links)

    async def test_mapped_and_decoded_ingress_are_independent(self):
        graph = _Graph()
        native = _native(graph)
        await native.set_surround_input(True)
        await native.set_mapped_ingress(mapped_ingress_edges("alsa_input.A", SUFFIXES))
        await native.set_surround_input(False)
        self.assertFalse(any(a.startswith("fxroute_surround_input:") for a, _ in graph.links))
        self.assertEqual(len(_mapped_links(graph)), 4)

    async def test_edges_beyond_the_engine_inputs_are_not_linked(self):
        graph = _Graph()
        native = _native(graph)
        native._config = DSPRuntimeConfig("stereo", "A", 48000, ("playback_AUX0",), ())
        await native.set_mapped_ingress(mapped_ingress_edges("alsa_input.A", SUFFIXES))
        self.assertEqual({target for _, target in _mapped_links(graph)}, {"fxroute_dsp:input_1", "fxroute_dsp:input_2"})

    async def test_engine_rebuild_relinks_without_failing_when_the_capture_node_is_gone(self):
        graph = _Graph(missing_sources=("alsa_input.A:",))
        native = _native(graph)
        native._mapped_ingress = mapped_ingress_edges("alsa_input.A", SUFFIXES)
        await native._reconcile_ingress_links()
        self.assertEqual(_mapped_links(graph), set())
        self.assertFalse(any(link.source.startswith("alsa_input.") for link in native._links))
        self.assertTrue(await native.verify())
        with self.assertRaisesRegex(RuntimeError, "failed to link ports"):
            await native.set_mapped_ingress(mapped_ingress_edges("alsa_input.A", SUFFIXES))
        graph.missing_sources = ()
        await native._reconcile_ingress_links(strict_mapped=True)
        self.assertEqual(len(_mapped_links(graph)), 4)

    async def test_measurement_suspends_and_restores_the_mapped_ingress(self):
        graph = _Graph()
        native = _native(graph)
        edges = mapped_ingress_edges("alsa_input.A", SUFFIXES)
        await native.set_mapped_ingress(edges)
        previous = await native.enter_active_measurement()
        self.assertEqual(_mapped_links(graph), set())
        self.assertEqual(native.snapshot()["mapped_ingress"], [])
        await native.exit_active_measurement(previous)
        self.assertEqual(_mapped_links(graph), set(edges))
        self.assertEqual(native._mapped_ingress, edges)

    async def test_measurement_without_mapped_ingress_changes_nothing(self):
        graph = _Graph()
        native = _native(graph)
        previous = await native.enter_active_measurement()
        await native.exit_active_measurement(previous)
        self.assertEqual(native._mapped_ingress, ())
        self.assertEqual(_mapped_links(graph), set())


class GraphDiagnosisTests(unittest.IsolatedAsyncioTestCase):
    async def test_mapped_edges_are_part_of_the_expected_surround_links(self):
        edges = mapped_ingress_edges(PRO_SOURCE, {
            position: f"AUX{index}" for index, position in enumerate(("FL", "FR", "FC", "LFE", "SL", "SR", "BL", "BR"))})
        links = [("fxroute_dsp_sink:monitor_FL", "fxroute_dsp:input_1"),
                 ("fxroute_dsp_sink:monitor_FR", "fxroute_dsp:input_2"),
                 ("fxroute_dsp:output_1", f"alsa_output.{CARD}.pro-output-0:playback_FL"),
                 ("fxroute_dsp:output_2", f"alsa_output.{CARD}.pro-output-0:playback_FR"), *edges]

        async def read(*args):
            return "\n".join(f"{a} -> {b}" for a, b in links)

        overview = {"output_mode": {"mode": "stereo", "effective_output_key": f"alsa_output.{CARD}.pro-output-0",
                                    "hardware_playback_ports": ["playback_FL", "playback_FR"]}}
        deps = SimpleNamespace(
            run_pw_link_command=read, output_mode_subwoofer_modes=frozenset(),
            get_dsp_snapshot=lambda: {"active": True, "mapped_ingress": [list(edge) for edge in edges],
                                      "config": {"sample_rate": 48000, "output_mode": "surround"}},
            contains_link=_contains_link, helper_argument_sample_rate=lambda snapshot: 48000)
        orchestrator = PlaybackOrchestrator(deps)
        diagnosis = await orchestrator.playback_graph_diagnosis(overview, target_rate=48000)
        self.assertTrue(diagnosis["links_complete"])
        self.assertEqual(sum(1 for link in diagnosis["links"] if link.startswith(PRO_SOURCE)), 8)
        links.remove(edges[4])
        self.assertFalse((await orchestrator.playback_graph_diagnosis(overview, target_rate=48000))["links_complete"])


class MainWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_links_verifies_and_stop_clears(self):
        import main
        dsp = SimpleNamespace(set_mapped_ingress=AsyncMock(), verify=AsyncMock(return_value=True))
        edges = mapped_ingress_edges(PRO_SOURCE, SUFFIXES)
        with patch.object(main.runtime, "dsp_runtime", dsp), patch.object(
                main, "_measurement_session_owns_live_rate", return_value=False):
            await main._sync_mapped_input(edges)
            dsp.set_mapped_ingress.assert_awaited_once_with(edges)
            await main._stop_mapped_input()
            self.assertEqual(dsp.set_mapped_ingress.await_args.args, ((),))

    async def test_unverified_links_fail(self):
        import main
        dsp = SimpleNamespace(set_mapped_ingress=AsyncMock(), verify=AsyncMock(return_value=False))
        with patch.object(main.runtime, "dsp_runtime", dsp), patch.object(
                main, "_measurement_session_owns_live_rate", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "could not be verified"):
                await main._sync_mapped_input(mapped_ingress_edges(PRO_SOURCE, SUFFIXES))

    async def test_measurement_owning_the_rate_keeps_the_ingress_unlinked(self):
        import main
        dsp = SimpleNamespace(set_mapped_ingress=AsyncMock(), verify=AsyncMock(side_effect=AssertionError))
        with patch.object(main.runtime, "dsp_runtime", dsp), patch.object(
                main, "_measurement_session_owns_live_rate", return_value=True):
            await main._sync_mapped_input(mapped_ingress_edges(PRO_SOURCE, SUFFIXES))
        dsp.set_mapped_ingress.assert_awaited_once_with(())

    async def test_missing_runtime_is_an_error_but_stop_is_a_no_op(self):
        import main
        with patch.object(main.runtime, "dsp_runtime", None):
            with self.assertRaisesRegex(RuntimeError, "native DSP runtime"):
                await main._sync_mapped_input(mapped_ingress_edges(PRO_SOURCE, SUFFIXES))
            await main._stop_mapped_input()

    def test_routes_are_registered_on_the_application(self):
        import main
        methods = {method for route in main.app.routes if getattr(route, "path", "") == "/api/audio/custom-mapping"
                   for method in route.methods}
        self.assertEqual(methods, {"GET", "PUT", "DELETE"})


class _SelectionHarness:
    def __init__(self, selected):
        self.current = {"mode": "external-input" if selected else "app-playback",
                        "selected_input_key": selected}
        self.stored = {"value": "new"}
        self.events = []

        async def route(overview):
            self.events.append(("route", dict(self.stored)))
            if self.fail_routes:
                self.fail_routes -= 1
                raise RuntimeError("routing failed")
            return overview

        self.fail_routes = 0

        def save(mode, key):
            self.current.update(mode=mode, selected_input_key=key)
            return dict(self.current)

        self.transitions = SourceTransitions(SourceTransitionDependencies(
            get_lock=SourceTransitionLock, feed=SourceOverviewFeed(AsyncMock()),
            build_overview=lambda: dict(self.current), save_selection=save,
            load_selection=lambda: dict(self.current), with_current_stdin_status=lambda value: value,
            sync_external_input=route, sync_bluetooth_input=AsyncMock(side_effect=lambda value: value),
            get_stdin_input=lambda: None, note_source_selection=lambda mode: None,
            pause_app_playback=AsyncMock(), sync_peak_monitor=AsyncMock(),
            sync_peak_monitor_for_stdin=AsyncMock()))


class ChangeInputConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_write_then_route_then_publish(self):
        harness = _SelectionHarness(f"custom-mapping::{CARD}")
        result = await harness.transitions.change_input_config(
            lambda: harness.stored.update(value="written"), lambda: harness.stored.update(value="old"))
        self.assertEqual(harness.events, [("route", {"value": "written"})])
        self.assertEqual(result["mode"], "external-input")
        self.assertIn("revision", result)

    async def test_failed_mutation_changes_nothing(self):
        harness = _SelectionHarness(None)

        def mutate():
            raise ValueError("invalid")

        restore_calls = []
        with self.assertRaisesRegex(ValueError, "invalid"):
            await harness.transitions.change_input_config(mutate, lambda: restore_calls.append(1))
        self.assertEqual((harness.events, restore_calls), ([], []))

    async def test_routing_failure_restores_the_previous_configuration(self):
        harness = _SelectionHarness(f"custom-mapping::{CARD}")
        harness.fail_routes = 1
        with self.assertRaisesRegex(RuntimeError, "routing failed"):
            await harness.transitions.change_input_config(
                lambda: harness.stored.update(value="written"), lambda: harness.stored.update(value="old"))
        self.assertEqual(harness.stored["value"], "old")
        self.assertEqual(len(harness.events), 2, "routing follows the restored configuration")

    async def test_deleting_the_selected_input_falls_back_to_app_playback(self):
        key = f"custom-mapping::{CARD}"
        harness = _SelectionHarness(key)
        result = await harness.transitions.change_input_config(
            lambda: harness.stored.update(value="deleted"), lambda: None, deselect_key=key)
        self.assertEqual(harness.current, {"mode": "app-playback", "selected_input_key": None})
        self.assertEqual(result["mode"], "app-playback")
        self.assertEqual(harness.events, [("route", {"value": "deleted"})])

    async def test_deleting_another_selection_keeps_it(self):
        harness = _SelectionHarness("alsa_input.other")
        await harness.transitions.change_input_config(
            lambda: None, lambda: None, deselect_key=f"custom-mapping::{CARD}")
        self.assertEqual(harness.current["selected_input_key"], "alsa_input.other")


class _ApiHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.dict(os.environ, {"XDG_CONFIG_HOME": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.context = {
            "device": {"card_base": CARD, "label": "Scarlett 18i20"},
            "capture": {"channels": [
                {"index": i + 1, "suffix": f"AUX{i}", "source": PRO_SOURCE} for i in range(8)]},
            "mapping": None, "input_key": None,
        }
        self.change_calls = []
        self.disarmed = []

        async def read_overview():
            return {"custom_mapping_context": self.context}

        async def change(mutate, restore, **kwargs):
            self.change_calls.append(kwargs)
            mutate()
            mapping = store.load_mappings().get(CARD)
            self.context = {**self.context, "mapping": mapping,
                            "input_key": store.input_key(CARD) if mapping else None}
            return {"custom_mapping_context": self.context}

        async def disarm(key):
            self.disarmed.append(key)

        app = FastAPI()
        register_custom_mapping_routes(app, CustomMappingApiDeps(
            read_overview=read_overview, change_input_config=change, disarm_auto_select=disarm))
        self.client = TestClient(app)


class ApiTests(_ApiHarness):
    def test_get_returns_device_capture_positions_and_no_mapping(self):
        body = self.client.get("/api/audio/custom-mapping").json()
        self.assertEqual(body["device"]["card_base"], CARD)
        self.assertEqual(len(body["capture"]["channels"]), 8)
        self.assertIsNone(body["mapping"])
        self.assertEqual([p["id"] for p in body["positions"]],
                         ["FL", "FR", "FC", "LFE", "SL", "SR", "BL", "BR"])

    def test_put_saves_then_get_and_second_put_update_the_single_mapping(self):
        response = self.client.put("/api/audio/custom-mapping", json={
            "name": " Upmixer ", "channels": {"FL": 3, "FR": 4, "FC": None}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["mapping"], {"name": "Upmixer", "channels": {"FL": 3, "FR": 4}})
        self.assertEqual(response.json()["input_key"], f"custom-mapping::{CARD}")
        self.client.put("/api/audio/custom-mapping", json={"channels": {"FL": 1, "FR": 2}})
        self.assertEqual(store.load_mappings(), {CARD: {"name": "", "channels": {"FL": 1, "FR": 2}}})

    def test_put_rejections_leave_the_store_untouched(self):
        bodies = [
            ({"channels": {"FL": 1}}, "Front R must be mapped"),
            ({"channels": {"FL": 1, "FR": 1}}, "assigned to both"),
            ({"channels": {"FL": 1, "FR": 9}}, "not available"),
            ({"channels": {"FL": 1, "FR": 2, "XX": 3}}, "Unknown channel"),
            ({"name": 3, "channels": {"FL": 1, "FR": 2}}, "Name must be a string"),
            ({"name": "x" * 41, "channels": {"FL": 1, "FR": 2}}, "at most 40"),
            ({"channels": []}, "object"),
            ({}, "object"),
        ]
        for body, message in bodies:
            with self.subTest(body=body):
                response = self.client.put("/api/audio/custom-mapping", json=body)
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.json()["detail"])
        self.assertEqual(store.load_mappings(), {})
        self.assertEqual(self.change_calls, [])

    def test_put_rejects_a_non_object_body(self):
        for raw in (b"[]", b"not json", b"3"):
            response = self.client.put("/api/audio/custom-mapping", content=raw,
                                       headers={"content-type": "application/json"})
            self.assertEqual(response.status_code, 400)
            self.assertIn("Invalid JSON body", response.json()["detail"])

    def test_put_needs_a_device_with_input_channels(self):
        self.context["capture"] = None
        response = self.client.put("/api/audio/custom-mapping", json={"channels": {"FL": 1, "FR": 2}})
        self.assertEqual(response.status_code, 400)
        self.assertIn("no input channels", response.json()["detail"])
        self.context["device"] = None
        response = self.client.put("/api/audio/custom-mapping", json={"channels": {"FL": 1, "FR": 2}})
        self.assertIn("No audio output device", response.json()["detail"])

    def test_delete_removes_the_mapping_deselects_it_and_clears_auto(self):
        store.save_mapping(CARD, MAPPING)
        self.context = {**self.context, "mapping": MAPPING, "input_key": store.input_key(CARD)}
        response = self.client.delete("/api/audio/custom-mapping")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["mapping"])
        self.assertEqual(store.load_mappings(), {})
        self.assertEqual(self.change_calls, [{"deselect_key": store.input_key(CARD)}])
        self.assertEqual(self.disarmed, [store.input_key(CARD)])

    def test_delete_without_a_mapping_is_a_404(self):
        response = self.client.delete("/api/audio/custom-mapping")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.disarmed, [])


if __name__ == "__main__":
    unittest.main()
