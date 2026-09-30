#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Shared bank operations preserve channel filters and exclude unrelated banks."""

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio import output_state as state_api
from audio.filter_banks import summarize_all_banks
from audio.output_topology import derive_topology
from measurement.target import freeze_measurement_target, target_output_mask, sweep_output_masks


def sub_state():
    return state_api.switch_mode(state_api.set_mode_routing(
        state_api.default_output_state(), "stereo-sub", "A",
        ["main_l", "main_r", "sub_l", "sub_r"]), "stereo-sub")


class FilterBankGroupsTests(unittest.TestCase):
    def test_topology_groups_pairs_and_deduplicates_fanout(self):
        topology = derive_topology("stereo-sub", ["main_r", "sub_l", "main_l", "sub_r", "main_l"])
        self.assertEqual(topology.bank_ids, ("global", "main", "sub"))

    def test_crossover_pairs_have_one_bank_per_way(self):
        topology = derive_topology("stereo-sub", [
            "left_low", "right_low", "left_low_mid", "right_low_mid",
            "left_mid", "right_mid", "left_high", "right_high", "sub1", "sub2"], crossover_enabled=True)
        self.assertEqual(topology.bank_ids, ("global", "low", "low_mid", "mid", "high", "sub1", "sub2"))

    def test_assignment_updates_both_channels_and_no_other_bank(self):
        before = sub_state()
        after = state_api.set_bank_preset(before, "stereo-sub", "main", preset="Room LR")
        banks = after["modes"]["stereo-sub"]["banks"]
        self.assertEqual(banks["main_l"]["preset"], "Room LR")
        self.assertEqual(banks["main_r"]["preset"], "Room LR")
        self.assertEqual(banks["global"], before["modes"]["stereo-sub"]["banks"]["global"])
        self.assertEqual(banks["sub_l"]["preset"], "Neutral")
        self.assertEqual(before, sub_state())

    def test_pair_measurement_keeps_both_roles_with_separate_sweeps(self):
        target = freeze_measurement_target(sub_state(), bank_id="main", output_key="A", channels=4,
                                           sample_rate_hz=48000, fingerprint="paired-test")
        self.assertEqual(target["measured_roles"], ["main_l", "main_r"])
        self.assertEqual(target_output_mask(target, roles=target["roles"]), 12)
        self.assertEqual(sweep_output_masks(target, roles=target["roles"]), {"left": 14, "right": 13})

    def test_all_switches_only_configured_banks_in_one_candidate(self):
        state = sub_state()
        state = state_api.set_bank_preset(state, "stereo-sub", "main", preset_b="Main B")
        state = state_api.set_bank_preset(state, "stereo-sub", "sub", preset_b="Sub B")
        state = state_api.set_mode_routing(state, "stereo-sub", "Other", ["main_l", "main_r", "sub1"])
        before = copy.deepcopy(state)
        after = state_api.switch_all_banks(state, "stereo-sub", "A", 4, "B")
        banks = after["modes"]["stereo-sub"]["banks"]
        self.assertEqual([banks[role]["preset"] for role in ("main_l", "main_r", "sub_l", "sub_r")],
                         ["Main B", "Main B", "Sub B", "Sub B"])
        self.assertEqual(banks["global"], before["modes"]["stereo-sub"]["banks"]["global"])
        self.assertEqual(banks["sub1"]["preset"], "Neutral")
        self.assertNotIn("all", banks)
        self.assertEqual(state, before)

    def test_all_switches_banks_with_b_and_keeps_the_rest_on_a(self):
        state = state_api.set_bank_preset(sub_state(), "stereo-sub", "main", preset_b="Main B")
        after = state_api.switch_all_banks(state, "stereo-sub", "A", 4, "B")
        banks = after["modes"]["stereo-sub"]["banks"]
        self.assertEqual([banks[role]["preset"] for role in ("main_l", "main_r", "sub_l", "sub_r")],
                         ["Main B", "Main B", "Neutral", "Neutral"])
        summary = summarize_all_banks([banks[role] for role in ("main_l", "main_r", "sub_l", "sub_r")])
        self.assertEqual((summary["active_side"], summary["can_b"]), ("B", True))
        back = state_api.switch_all_banks(after, "stereo-sub", "A", 4, "A")
        self.assertEqual(back["modes"]["stereo-sub"]["banks"], state["modes"]["stereo-sub"]["banks"])

    def test_all_without_any_b_refuses_the_switch(self):
        state = sub_state()
        before = copy.deepcopy(state)
        banks = state["modes"]["stereo-sub"]["banks"]
        self.assertFalse(summarize_all_banks([banks[role] for role in ("main_l", "sub_l")])["can_b"])
        with self.assertRaisesRegex(ValueError, "no assigned preset in any configured bank"):
            state_api.switch_all_banks(state, "stereo-sub", "A", 4, "B")
        with self.assertRaisesRegex(ValueError, "A or B"):
            state_api.switch_all_banks(state, "stereo-sub", "A", 4, "C")
        self.assertEqual(state, before)

    def test_all_is_not_an_assignable_or_measurable_bank(self):
        with self.assertRaises(ValueError):
            state_api.set_bank_preset(sub_state(), "stereo-sub", "all", preset="Room")
        with self.assertRaises(ValueError):
            freeze_measurement_target(sub_state(), bank_id="all", output_key="A", channels=4,
                                      sample_rate_hz=48000, fingerprint="paired-test")

    def test_pure_stereo_keeps_global_bank(self):
        stereo = state_api.switch_mode(state_api.set_mode_routing(
            state_api.default_output_state(), "stereo", "A", ["main_l", "main_r"]), "stereo")
        with self.assertRaisesRegex(ValueError, "Global"):
            state_api.select_bank(stereo, "stereo", "A", 2, "main")
        selected = state_api.select_bank(stereo, "stereo", "A", 2, "global")
        self.assertEqual(selected["modes"]["stereo"]["selected_bank"], "global")
        legacy = dict(stereo)
        legacy["modes"]["stereo"]["selected_bank"] = "main_l"
        rerouted = state_api.set_mode_routing(legacy, "stereo", "A", ["main_l", "main_r"])
        self.assertEqual(rerouted["modes"]["stereo"]["selected_bank"], "global")


if __name__ == "__main__":
    unittest.main()
