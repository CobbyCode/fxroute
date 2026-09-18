#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Lossless legacy snapshots and signal-preserving migration of four old modes."""

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.output_state import routing_for_device
from audio.output_state_migration import migrate_legacy_output_state
from audio.output_topology import derive_topology


class OutputStateMigrationTests(unittest.TestCase):
    def migrate(self, mode, routing=None, **overrides):
        values = {"mode": mode, "routing": routing or {}, "active_preset": "Room IR -1.5dB",
                  "compare": {"presetA": "Room", "presetB": "Room IR -1.5dB", "activeSide": "B"},
                  "extras": {"loudness": {"enabled": True}}, "output_key": "A", "channels": 6}
        values.update(overrides)
        return migrate_legacy_output_state(**values)

    def test_default_routes_preserve_silence_mono_fanout_and_stereo_bass(self):
        cases = [
            ("stereo", "stereo", ["main_l", "main_r", "off", "off", "off", "off"], "none"),
            ("subwoofer-2.1", "stereo-sub", ["main_l", "main_r", "sub1", "sub1", "off", "off"], "mono"),
            ("subwoofer-2.2", "stereo-sub", ["main_l", "main_r", "sub1", "sub2", "off", "off"], "dual-mono"),
            ("subwoofer-2.2-stereo", "stereo-sub", ["main_l", "main_r", "sub_l", "sub_r", "off", "off"], "stereo"),
        ]
        for legacy, mode, expected, sub_mode in cases:
            with self.subTest(legacy=legacy):
                result = self.migrate({"mode": legacy})
                self.assertEqual(result["active_mode"], mode)
                self.assertFalse(result["modes"][mode]["crossover_enabled"])
                self.assertEqual(routing_for_device(result, mode, "A"), expected)
                self.assertEqual(derive_topology(mode, expected).sub_mode, sub_mode)
                other = "stereo-sub" if mode == "stereo" else "stereo"
                self.assertEqual(result["modes"][other]["routing"], {})

    def test_custom_port_order_and_dormant_ports_survive(self):
        result = self.migrate({"mode": "subwoofer-2.1"}, {"A": [2, 0, 3, 1, 4, 1]}, channels=4)
        self.assertEqual(routing_for_device(result, "stereo-sub", "A"), ["main_r", "off", "sub1", "main_l", "sub1", "main_l"])

    def test_active_stereo_bass_block_wins_without_erasing_other_legacy_data(self):
        mode = {"mode": "subwoofer-2.2-stereo", "crossover_frequency_hz": 110,
                "main_highpass_enabled": False,
                "subwoofer": {"crossover_frequency_hz": 60, "sub_level_db": -5},
                "subwoofers": {"sub1": {"level_db": -9}},
                "subwoofers_22": {"sub1": {"alignment_ms": 9}},
                "subwoofers_22_stereo": {"sub1": {"level_db": -3, "alignment_ms": -2.5, "polarity": "invert"},
                                         "sub2": {"level_db": 2, "alignment_ms": 3.25}}}
        before = copy.deepcopy(mode)
        result = self.migrate(mode)
        stereo = result["modes"]["stereo-sub"]
        self.assertEqual(stereo["bass_management"], {"frequency_hz": 110, "main_highpass_enabled": False})
        self.assertEqual(stereo["processing"]["sub_l"]["level_db"], -3)
        self.assertEqual(stereo["processing"]["sub_l"]["alignment_ms"], -2.5)
        self.assertEqual(stereo["processing"]["sub_l"]["polarity"], "invert")
        self.assertEqual(stereo["processing"]["sub_r"]["alignment_ms"], 3.25)
        self.assertEqual(result["legacy"]["mode"], before)
        self.assertEqual(mode, before)
        result["legacy"]["mode"]["subwoofers_22"]["sub1"]["alignment_ms"] = 0
        self.assertEqual(mode, before)

    def test_21_controls_and_compare_are_only_imported_into_stereo(self):
        result = self.migrate({"mode": "subwoofer-2.1", "subwoofer": {
            "sub_level_db": -7, "sub_alignment_ms": -4, "sub_polarity": "invert", "crossover_frequency_hz": 90}})
        stereo, other = result["modes"]["stereo-sub"], result["modes"]["stereo"]
        self.assertEqual(stereo["banks"]["global"], {"preset": "Room IR -1.5dB", "preset_a": "Room", "preset_b": "Room IR -1.5dB"})
        self.assertEqual(stereo["processing"]["sub1"]["level_db"], -7)
        self.assertEqual(stereo["processing"]["sub1"]["alignment_ms"], -4)
        self.assertEqual(stereo["banks"]["sub1"]["preset"], "Neutral")
        self.assertEqual(stereo["extras"], {"loudness": {"enabled": True}})
        self.assertEqual(other["extras"], {})
        self.assertEqual(other["banks"]["global"]["preset"], "Neutral")

    def test_conflicting_device_aliases_and_invalid_legacy_routing_fail(self):
        usb = "alsa_output.usb-Test-00.multichannel-output"
        pro = "alsa_output.usb-Test-00.pro-output-0"
        for routing in ({usb: [1, 2, 3, 4], pro: [2, 1, 3, 4]}, {"A": [True, 2]}, {"A": [1, 5]}):
            with self.subTest(routing=routing), self.assertRaises(ValueError):
                self.migrate({"mode": "stereo"}, routing)
        with self.assertRaises(ValueError):
            self.migrate({"mode": "future"})


if __name__ == "__main__":
    unittest.main()
