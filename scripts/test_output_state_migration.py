#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Lossless legacy snapshots and signal-preserving migration of four old modes."""

import copy
import json
import tempfile
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.output_state import (default_bass_management, default_output_state,
                                routing_for_device, set_crossover, set_mode_routing,
                                validate_output_state, VERSION)
from audio.output_state_migration import (drop_foreign_bank_presets, migrate_legacy_output_state,
                                          upgrade_output_state)
from audio.output_topology import derive_topology
from audio.output_state_store import OutputStateStore


TAGS = {"Room": "global", "MainEQ": "main", "MainIR": "main", "LowEQ": "low",
        "LeftLow": "left_low", "Sub1EQ": "sub1"}


def bank_state(bindings):
    state = set_crossover(default_output_state(), "stereo", True)
    state = set_mode_routing(state, "stereo", "A", ["left_low", "right_low", "left_high", "right_high"])
    state = set_mode_routing(state, "stereo-sub", "A", ["main_l", "main_r", "sub1", "sub2"])
    for (mode, role), (preset, preset_a, preset_b) in bindings.items():
        state["modes"][mode]["banks"][role] = {"preset": preset, "preset_a": preset_a, "preset_b": preset_b}
    return validate_output_state(state)


class ForeignBankPresetTests(unittest.TestCase):
    def drop(self, state):
        return drop_foreign_bank_presets(state, TAGS.get)

    def banks(self, state, mode):
        return state["modes"][mode]["banks"]

    def test_foreign_b_is_cleared_and_listening_falls_back_to_a(self):
        state = bank_state({("stereo", "left_low"): ("Room", "Neutral", "Room"),
                            ("stereo", "right_low"): ("Room", "Neutral", "Room")})
        result, changes = self.drop(state)
        for role in ("left_low", "right_low"):
            self.assertEqual(self.banks(result, "stereo")[role],
                             {"preset": "Neutral", "preset_a": "Neutral", "preset_b": None})
        self.assertEqual(len(changes), 2)
        self.assertIn("stereo/left_low", changes[0])

    def test_foreign_a_falls_back_to_neutral_and_keeps_an_owned_b(self):
        state = bank_state({("stereo-sub", "main_l"): ("MainEQ", "Room", "MainEQ"),
                            ("stereo-sub", "main_r"): ("Room", "Room", "MainIR"),
                            ("stereo-sub", "sub1"): ("Room", "Room", "Neutral")})
        banks = self.banks(self.drop(state)[0], "stereo-sub")
        self.assertEqual(banks["main_l"], {"preset": "MainEQ", "preset_a": "Neutral", "preset_b": "MainEQ"})
        self.assertEqual(banks["main_r"], {"preset": "Neutral", "preset_a": "Neutral", "preset_b": "MainIR"})
        self.assertEqual(banks["sub1"], {"preset": "Neutral", "preset_a": "Neutral", "preset_b": None})

    def test_global_drops_area_presets_and_keeps_its_own(self):
        state = bank_state({("stereo", "global"): ("Room", "Room", "MainEQ"),
                            ("stereo-sub", "global"): ("MainEQ", "Room", "MainEQ")})
        result = self.drop(state)[0]
        self.assertEqual(self.banks(result, "stereo")["global"],
                         {"preset": "Room", "preset_a": "Room", "preset_b": None})
        self.assertEqual(self.banks(result, "stereo-sub")["global"],
                         {"preset": "Room", "preset_a": "Room", "preset_b": None})

    def test_valid_bindings_are_untouched_and_the_cleanup_is_idempotent(self):
        state = bank_state({("stereo", "global"): ("Room", "Neutral", "Room"),
                            ("stereo", "left_low"): ("LowEQ", "LeftLow", "LowEQ"),
                            ("stereo", "left_high"): ("Direct", "Neutral", "Direct"),
                            ("stereo-sub", "main_l"): ("MainIR", "MainEQ", "MainIR"),
                            ("stereo-sub", "main_r"): ("Untagged", "Untagged", "MainIR"),
                            ("stereo-sub", "sub1"): ("Sub1EQ", "Sub1EQ", "Direct")})
        result, changes = self.drop(state)
        self.assertEqual(changes, [])
        self.assertEqual(result, state)
        dirty = bank_state({("stereo", "left_low"): ("Room", "Neutral", "Room")})
        cleaned = self.drop(dirty)[0]
        self.assertEqual(self.drop(cleaned), (cleaned, []))

    def test_an_unlisted_listening_preset_is_kept_unless_foreign(self):
        state = bank_state({("stereo-sub", "sub1"): ("Neutral", "Direct", "Room"),
                            ("stereo-sub", "sub2"): ("Room", "Direct", "Sub1EQ")})
        banks = self.banks(self.drop(state)[0], "stereo-sub")
        self.assertEqual(banks["sub1"], {"preset": "Neutral", "preset_a": "Direct", "preset_b": None})
        self.assertEqual(banks["sub2"], {"preset": "Direct", "preset_a": "Direct", "preset_b": None})


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
        self.assertEqual(stereo["bass_management"],
                         {**default_bass_management(), "frequency_hz": 110,
                          "main_highpass_enabled": False})
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


class VersionOneUpgradeTests(unittest.TestCase):
    def historical(self, active_mode, subs):
        state = default_output_state()
        state = set_mode_routing(state, "stereo", "A", ["main_r", "main_l"])
        state = set_crossover(state, "stereo-sub", True)
        state = set_mode_routing(state, "stereo-sub", "A",
                                ["left_low", "right_low", "left_high", "right_high", *subs])
        modes = {"stereo": state["modes"]["stereo"], "crossover": state["modes"]["stereo-sub"]}
        for role in ("main_l", "main_r"):
            modes["crossover"]["banks"].pop(role, None)
            modes["crossover"]["processing"].pop(role, None)
        for index, config in enumerate(modes.values()):
            config.pop("crossover_enabled")
            config.pop("av_sync")
            config["bass_management"] = {"frequency_hz": 95, "main_highpass_enabled": False}
            for role, processing in config["processing"].items():
                processing.pop("level_cal_db")
                processing.update(level_db=-3.5-index, alignment_ms=2.25+index, polarity="invert")
            config["banks"]["global"] = {"preset": "Room B", "preset_a": "Room A", "preset_b": "Room B"}
        return {"schema": "fxroute.output-state", "version": 1, "revision": 12,
                "active_mode": active_mode, "modes": modes, "legacy": {"original": True}}

    def test_store_load_upgrades_real_v1_shapes_without_writing_or_losing_settings(self):
        for active in ("stereo", "crossover"):
            for subs in (["sub1"], ["sub1", "sub2"], ["sub_l", "sub_r"], []):
                with self.subTest(active=active, subs=subs), tempfile.TemporaryDirectory() as directory:
                    payload = self.historical(active, subs)
                    original = copy.deepcopy(payload)
                    path = Path(directory) / "output-state.json"
                    path.write_text(json.dumps(payload))
                    before = path.read_bytes()
                    store = OutputStateStore(path)
                    result = store.load()
                    self.assertEqual(result["version"], VERSION)
                    self.assertEqual(result["revision"], 12)
                    self.assertEqual(result["legacy"]["output_state_v1"], original)
                    self.assertEqual(payload, original)
                    self.assertEqual(path.read_bytes(), before)
                    for old_mode, old_config in original["modes"].items():
                        new_mode = "stereo-sub" if old_mode == "crossover" and subs else "stereo"
                        # If both old modes resolve to one mode, the active mode wins.
                        if not subs and old_mode != active:
                            continue
                        config = result["modes"][new_mode]
                        self.assertEqual(config["routing"], old_config["routing"])
                        self.assertEqual(config["banks"], old_config["banks"])
                        self.assertEqual(config["crossover_enabled"], old_mode == "crossover")
                        for role, processing in old_config["processing"].items():
                            self.assertEqual(config["processing"][role],
                                             {**processing, "level_cal_db": {"global": 0.0, "bank": 0.0}})
                    self.assertEqual(upgrade_output_state(result), result)
                    committed = store.commit(result, expected_revision=12)
                    self.assertEqual(store.load(), committed)

    def test_v2_through_v6_upgrade_neutral_slots_and_preserve_existing_calibration(self):
        for version in range(2, VERSION + 1):
            with self.subTest(version=version):
                state = default_output_state()
                state["version"] = version
                if version < 4:
                    state.pop("input_modes")
                    state["modes"].pop("surround")
                for config in state["modes"].values():
                    if version < 6:
                        config.pop("av_sync")
                    if version < 5:
                        for processing in config["processing"].values():
                            processing.pop("level_cal_db")
                    else:
                        for processing in config["processing"].values():
                            processing["level_cal_db"] = {"global": -2.0, "bank": -1.0}
                    if version == 2:
                        config["bass_management"] = {"frequency_hz": 80, "main_highpass_enabled": True}
                before = copy.deepcopy(state)
                result = upgrade_output_state(state)
                self.assertEqual(state, before)
                for mode in before["modes"]:
                    for role in before["modes"][mode]["processing"]:
                        self.assertEqual(result["modes"][mode]["processing"][role]["level_cal_db"],
                                         {"global": 0.0, "bank": 0.0} if version < 5 else
                                         {"global": -2.0, "bank": -1.0})
                self.assertEqual(upgrade_output_state(result), result)


class VersionTwoUpgradeTests(unittest.TestCase):
    """Version two stored only the crossover frequency and the Main HPF switch."""

    def stored(self):
        state = default_output_state()
        state["version"] = 2
        state.pop("input_modes")
        state["modes"].pop("surround")
        for config in state["modes"].values():
            config["bass_management"] = {"frequency_hz": 95, "main_highpass_enabled": False}
        return state

    def test_every_mode_gains_the_shared_defaults(self):
        upgraded = upgrade_output_state(self.stored())
        self.assertEqual(upgraded["version"], VERSION)
        for mode, config in upgraded["modes"].items():
            with self.subTest(mode=mode):
                self.assertEqual(config["bass_management"],
                                 default_bass_management() if mode == "surround" else
                                 {**default_bass_management(), "frequency_hz": 95, "main_highpass_enabled": False})

    def test_unknown_fields_still_fail_closed(self):
        state = self.stored()
        state["modes"]["stereo"]["bass_management"]["typo"] = 1
        with self.assertRaises(ValueError):
            upgrade_output_state(state)
        state = self.stored()
        with self.assertRaises(ValueError):
            upgrade_output_state({**state, "modes": {"stereo": state["modes"]["stereo"]}})

    def test_current_version_roundtrips_unchanged(self):
        state = default_output_state()
        self.assertEqual(upgrade_output_state(state), validate_output_state(state))


if __name__ == "__main__":
    unittest.main()
