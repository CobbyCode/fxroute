#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""HiFi split-group helpers: one device entry for UMC Line A/B sinks."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.samplerate.parsing import (
    card_name_for_sink_key,
    combined_hifi_key_for_card,
    group_hifi_split_sinks,
    is_hifi_combined_key,
    is_hifi_split_sink_key,
)
from audio.output_ports import hardware_link_target
from audio.output_routing import device_key


LINE1 = "alsa_output.usb-BEHRINGER_UMC204HD_192k-00.HiFi__Line1__sink"
LINE2 = "alsa_output.usb-BEHRINGER_UMC204HD_192k-00.HiFi__Line2__sink"
CARD = "alsa_card.usb-BEHRINGER_UMC204HD_192k-00"
COMBINED = "alsa_output.usb-BEHRINGER_UMC204HD_192k-00.HiFi__combined"
OTHER = "alsa_output.usb-SMSL_SMSL_USB_AUDIO-00.iec958-stereo"
OLD_UMC = "alsa_output.usb-BEHRINGER_UMC204HD_192k-00.analog-surround-40"


class HifiSplitKeyTests(unittest.TestCase):
    def test_split_detection(self) -> None:
        self.assertTrue(is_hifi_split_sink_key(LINE1))
        self.assertTrue(is_hifi_split_sink_key(LINE2))
        self.assertFalse(is_hifi_split_sink_key(OTHER))
        self.assertFalse(is_hifi_split_sink_key("fxroute_dsp_sink"))
        self.assertFalse(is_hifi_split_sink_key(COMBINED))

    def test_combined_key_roundtrip(self) -> None:
        self.assertEqual(combined_hifi_key_for_card(CARD), COMBINED)
        self.assertTrue(is_hifi_combined_key(COMBINED))
        self.assertFalse(is_hifi_combined_key(LINE1))
        self.assertFalse(is_hifi_combined_key(OTHER))
        self.assertEqual(card_name_for_sink_key(COMBINED), CARD)
        self.assertEqual(card_name_for_sink_key(LINE1), CARD)
        self.assertEqual(card_name_for_sink_key(LINE2), CARD)


class HifiGroupTests(unittest.TestCase):
    def test_groups_split_siblings_by_card(self) -> None:
        groups = group_hifi_split_sinks([LINE2, OTHER, LINE1])
        self.assertEqual(set(groups), {CARD})
        self.assertEqual(groups[CARD], [LINE1, LINE2])

    def test_single_split_has_no_group(self) -> None:
        self.assertEqual(group_hifi_split_sinks([LINE1, OTHER]), {})
        self.assertEqual(group_hifi_split_sinks([OTHER]), {})
        self.assertEqual(group_hifi_split_sinks([]), {})


class HifiLinkTargetTests(unittest.TestCase):
    def test_qualified_ports_pass_through(self) -> None:
        self.assertEqual(
            hardware_link_target(COMBINED, f"{LINE1}:playback_FL"),
            f"{LINE1}:playback_FL",
        )

    def test_bare_ports_use_output_key(self) -> None:
        self.assertEqual(
            hardware_link_target(OTHER, "playback_FL"),
            f"{OTHER}:playback_FL",
        )


class HifiRoutingKeyTests(unittest.TestCase):
    def test_split_members_and_combined_share_routing_key(self) -> None:
        self.assertEqual(device_key(LINE1), device_key(LINE2))
        self.assertEqual(device_key(COMBINED), device_key(LINE1))


def _short_line(sink_id: int, name: str, spec: str, state: str) -> str:
    return f"{sink_id}\t{name}\tPipeWire\t{spec}\t{state}"


def _detailed_block(
    name: str,
    description: str,
    device_description: str,
    spec: str,
    port_key: str,
    port_label: str,
    channel_map: str = "front-left,front-right",
) -> str:
    return (
        "Sink #99\n"
        f"\tName: {name}\n"
        f"\tDescription: {description}\n"
        f"\tSample Specification: {spec}\n"
        f"\tChannel Map: {channel_map}\n"
        "\tProperties:\n"
        f"\t\tdevice.description = \"{device_description}\"\n"
        "\tPorts:\n"
        f"\t\t{port_key}: {port_label} (type: Line, priority: 100, availability unknown)\n"
        f"\tActive Port: {port_key}\n"
        "\tFormats:\n"
        "\t\tpcm\n"
    )


class HifiOverviewGroupTests(unittest.TestCase):
    """The Device inventory shows one UMC device, not Line A/B entries."""

    def _overview(self, selected_key=None):
        from unittest import mock

        from audio.samplerate import overview as ov

        short = "\n".join([
            _short_line(80, OTHER, "s32le 2ch 44100Hz", "RUNNING"),
            _short_line(83, LINE2, "float32le 2ch 44100Hz", "SUSPENDED"),
            _short_line(86, LINE1, "float32le 2ch 44100Hz", "SUSPENDED"),
        ])
        detailed = (
            _detailed_block(
                OTHER, "SMSL USB AUDIO Digital Stereo (IEC958)", "SMSL USB AUDIO",
                "s32le 2ch 44100Hz", "iec958-stereo-output", "Digital Output (S/PDIF)",
            )
            + _detailed_block(
                LINE2, "UMC204HD 192k Line B", "UMC204HD 192k",
                "float32le 2ch 44100Hz", "[Out] Line2", "Line B",
            )
            + _detailed_block(
                LINE1, "UMC204HD 192k Line A", "UMC204HD 192k",
                "float32le 2ch 44100Hz", "[Out] Line1", "Line A",
            )
        )
        status = {
            "available": True,
            "sink": {"id": 80, "name": OTHER, "description": "SMSL USB AUDIO Digital Stereo (IEC958)"},
            "relevant_sink": {"name": OTHER},
            "notes": [],
        }

        def fake_run(args):
            cmd = list(args)
            if cmd == ["pactl", "list", "sinks", "short"]:
                return short
            if cmd == ["pactl", "list", "sinks"]:
                return detailed
            if cmd == ["pw-cli", "ls", "Node"]:
                return ""
            if cmd == ["pw-link", "-io"]:
                return (
                    f"{LINE1}:playback_FL\n{LINE1}:playback_FR\n"
                    f"{LINE2}:playback_FL\n{LINE2}:playback_FR\n"
                    f"{OTHER}:playback_FL\n{OTHER}:playback_FR\n"
                )
            raise AssertionError(f"unexpected command: {cmd}")

        with mock.patch.object(ov, "_run_command", side_effect=fake_run), \
                mock.patch.object(ov, "get_bluetooth_audio_overview", return_value={"available": False, "devices": [], "roles": {}}), \
                mock.patch.object(ov, "_load_audio_output_selection", return_value={"selected_key": selected_key}):
            return ov.get_audio_output_overview(status=dict(status))

    def test_combined_device_replaces_line_entries(self) -> None:
        overview = self._overview(selected_key=COMBINED)
        by_key = {item.get("key"): item for item in overview.get("outputs") or []}
        self.assertIn(COMBINED, by_key)
        combined = by_key[COMBINED]
        self.assertTrue(combined.get("selectable"))
        self.assertEqual(combined.get("channels"), 4)
        self.assertEqual(combined.get("label"), "UMC204HD 192k")
        self.assertEqual(combined.get("member_sinks"), [LINE1, LINE2])
        # Split siblings stay in the inventory for routing diagnostics
        # but never appear as separate Device selector entries.
        self.assertFalse(by_key[LINE1].get("selectable"))
        self.assertFalse(by_key[LINE2].get("selectable"))
        selector_keys = [item.get("key") for item in overview.get("outputs") or [] if item.get("selectable")]
        self.assertIn(COMBINED, selector_keys)
        self.assertNotIn(LINE1, selector_keys)
        self.assertNotIn(LINE2, selector_keys)
        # Four hardware channels stay available for routing.
        ports = (overview.get("output_mode") or {}).get("hardware_playback_ports") or []
        self.assertEqual(len(ports), 4)
        self.assertTrue(all(":" in str(port) for port in ports))

    def test_legacy_single_sink_selection_migrates_to_combined(self) -> None:
        overview = self._overview(selected_key=OLD_UMC)
        selected = overview.get("selected_output") or {}
        self.assertEqual(selected.get("key"), COMBINED)


if __name__ == "__main__":
    unittest.main()
