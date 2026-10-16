#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Equal A/B choices preserve side identity, ownership and processing."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.filter_banks import bank_catalog, summarize_all_banks
from audio.output_state import (default_output_state, referenced_presets, set_bank_preset,
                                set_mode_routing, switch_all_banks, switch_mode)
from audio.output_state_migration import upgrade_output_state
from audio.output_state_store import OutputStateStore
from dsp.banks import BankState
from dsp.processing_plan import compile_processing_plan
from dsp.surround_global import neutral_selection


ROLES = ["main_l", "main_r", "rear_l", "rear_r", "sub1", "sub2"]


class ABSelectionDefaultsTests(unittest.TestCase):
    def state(self):
        return switch_mode(set_mode_routing(default_output_state(), "surround", "A", ROLES), "surround")

    def test_new_and_legacy_empty_slots_use_neutral_without_changing_listening(self):
        state = self.state()
        for config in state["modes"].values():
            for bank in config["banks"].values():
                self.assertEqual(bank["preset_b"], bank["preset_a"])
        legacy = copy.deepcopy(state)
        for config in legacy["modes"].values():
            for bank in config["banks"].values():
                bank["preset_b"] = None
        legacy["modes"]["surround"]["banks"]["global"]["preset_a"]["rear"] = "Rear FIR"
        legacy["modes"]["surround"]["banks"]["global"]["preset"]["rear"] = "Rear FIR"
        before = copy.deepcopy(legacy)
        loaded = upgrade_output_state(legacy)
        self.assertEqual(legacy, before)
        for mode, config in loaded["modes"].items():
            for role, bank in config["banks"].items():
                expected = neutral_selection() if mode == "surround" and role == "global" else "Neutral"
                self.assertEqual(bank["preset_b"], expected)
                self.assertEqual(bank["preset"], legacy["modes"][mode]["banks"][role]["preset"])

    def test_equal_area_slots_switch_persist_and_do_not_change_other_banks(self):
        state = self.state()
        state = set_bank_preset(state, "surround", "main", preset="Room")
        state = set_bank_preset(state, "surround", "main", preset_b="Room")
        before = copy.deepcopy(state)
        selected = set_bank_preset(state, "surround", "main", active_side="B")
        self.assertEqual(bank_catalog(selected["modes"]["surround"], ROLES)["main"]["active_side"], "B")
        for role in ("global", "rear_l", "rear_r", "sub1", "sub2"):
            self.assertEqual(selected["modes"]["surround"]["banks"][role], before["modes"]["surround"]["banks"][role])
        self.assertEqual(selected["modes"]["stereo"], before["modes"]["stereo"])
        self.assertEqual(referenced_presets(selected, None, "main"), {"Room", "Neutral"})
        with tempfile.TemporaryDirectory() as directory:
            store = OutputStateStore(Path(directory) / "state.json")
            store.commit(selected, expected_revision=0)
            loaded = store.load()
            self.assertEqual(BankState.from_dict(loaded["modes"]["surround"]["banks"]["main_l"]).active_side, "B")
        back = set_bank_preset(selected, "surround", "main", active_side="A")
        self.assertEqual(bank_catalog(back["modes"]["surround"], ROLES)["main"]["active_side"], "A")

    def test_equal_global_groups_and_joint_switch_keep_processing_unchanged(self):
        state = self.state()
        for slot in ("preset_a", "preset_b"):
            state = set_bank_preset(state, "surround", "global", **{slot: "Rear FIR"}, group="rear")
        before = copy.deepcopy(state)
        switched = set_bank_preset(state, "surround", "global", active_side="B")
        self.assertEqual(BankState.from_dict(switched["modes"]["surround"]["banks"]["global"]).active_side, "B")
        def loader(_mode, _bank, name):
            return {"metadata": {"layout": "surround"}, "chain": [{
                "id": "equalizer#0", "type": "equalizer", "enabled": True, "group": "rear",
                "params": {"channelMode": "stereo-linked", "eqMode": "IIR", "bands": [{
                    "enabled": True, "filterType": "bell", "frequencyHz": 100.0,
                    "gainDb": -3.0, "q": 2.0, "delayMs": 0.0}]},
            }] if name == "Rear FIR" else []}
        plan = lambda value: compile_processing_plan(value, output_key="A", channels=6,
                                                     sample_rate_hz=48000, preset_loader=loader)
        self.assertEqual(plan(switched), plan(before))
        switched = switch_all_banks(switched, "surround", "A", 6, "B")
        config = switched["modes"]["surround"]
        self.assertEqual(summarize_all_banks([config["banks"][role] for role in ROLES])["active_side"], "B")
        self.assertEqual(config["banks"]["global"], set_bank_preset(before, "surround", "global", active_side="B")["modes"]["surround"]["banks"]["global"])
        self.assertEqual(plan(switched), plan(before))


if __name__ == "__main__":
    unittest.main()
