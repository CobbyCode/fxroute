#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Position routing, separate LFE, shared subs and lossless Surround migration."""

import copy
import json
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.filter_banks import bank_catalog, bank_definitions, selected_bank
from audio.output_state import (default_output_state, referenced_presets, release_bank_preset, select_bank,
                                set_bank_preset, set_bass_management, set_crossover, set_mode_routing,
                                switch_mode, validate_output_state)
from audio.output_state_migration import upgrade_output_state
from audio.output_topology import derive_topology, roles_for_mode
from audio.output_service import OutputService, OutputServiceDeps
from dsp.native_config import layout_from_plan
from dsp.processing_plan import FOLD_GAIN, compile_processing_plan, rendered_crossover_filters
from dsp.manager import DSPManager, build_wav_bytes
from dsp.runtime import DSPRuntimeConfig
from dsp.surround_global import neutral_selection
import tempfile


ROLES = ["main_l", "main_r", "center", "side_l", "side_r", "rear_l", "rear_r", "sub1", "sub2"]
QUAD_SUBS = ["main_l", "main_r", "rear_l", "rear_r", "sub1", "sub2"]


def group_filter(frequency_hz, highpass_enabled=True, family="linkwitz-riley", slope_db_oct=24):
    return {"family": family, "slope_db_oct": slope_db_oct, "frequency_hz": frequency_hz,
            "highpass_enabled": highpass_enabled}


class SurroundTests(unittest.TestCase):
    def state(self, roles=ROLES):
        return switch_mode(set_mode_routing(default_output_state(), "surround", "A", roles), "surround")

    def plan(self, state):
        return compile_processing_plan(state, output_key="A", channels=16, sample_rate_hz=48000,
                                       preset_loader=lambda *_: {"chain": []})

    def test_surround_roles_and_only_common_mono_subs(self):
        self.assertEqual(roles_for_mode("surround"), tuple(ROLES))
        topology = derive_topology("surround", ROLES)
        topology.require_activatable()
        self.assertEqual(topology.sub_mode, "dual-mono")
        for subs in (["sub_l"], ["sub_r"], ["sub2"]):
            with self.subTest(subs=subs), self.assertRaises(ValueError):
                derive_topology("surround", ["main_l", "main_r", *subs]).require_activatable()

    def test_bank_pairs_do_not_pair_center_with_lfe(self):
        definitions = bank_definitions(ROLES)
        self.assertEqual(definitions["side"]["roles"], ["side_l", "side_r"])
        self.assertEqual(definitions["rear"]["roles"], ["rear_l", "rear_r"])
        self.assertEqual(definitions["center"]["channel_mode"], "mono")
        self.assertEqual(definitions["sub1"]["channel_mode"], "mono")

    def test_crossover_independent_and_keeps_surround_speakers_and_subs(self):
        state = set_crossover(self.state(), "surround", True)
        roles = ["left_low", "left_high", "right_low", "right_high", *ROLES[2:]]
        state = set_mode_routing(state, "surround", "A", roles)
        plan = self.plan(state)
        self.assertEqual(plan["way_count"], 2)
        outputs = {item["role"]: item for item in plan["outputs"]}
        self.assertEqual(outputs["center"]["routes"], [{"input": 2, "gain": 1.0}])
        self.assertEqual(outputs["right_high"]["routes"], [{"input": 1, "gain": 1.0}])
        self.assertEqual(state["modes"]["stereo"]["crossover_enabled"], False)

    def test_position_tagged_inputs_and_same_physical_sub_bus(self):
        plan = self.plan(self.state())
        self.assertEqual(plan["input_positions"], ["FL", "FR", "FC", "LFE", "SL", "SR", "BL", "BR"])
        outputs = {item["role"]: item for item in plan["outputs"]}
        for role, channel in (("main_l", 0), ("main_r", 1), ("center", 2),
                              ("side_l", 4), ("side_r", 5), ("rear_l", 6), ("rear_r", 7)):
            self.assertEqual(outputs[role]["routes"], [{"input": channel, "gain": 1.0}])
        self.assertEqual(outputs["sub1"]["routes"], [{"input": i, "gain": 0.5} for i in (0, 1, 2, 4, 5, 6, 7)])
        for role in ("sub1", "sub2"):
            self.assertEqual(outputs[role]["lfe_routes"], [{"input": 3, "gain": 1.0}])
        self.assertEqual(outputs["sub1"]["routes"], outputs["sub2"]["routes"])
        self.assertEqual(outputs["sub1"]["crossover"], outputs["sub2"]["crossover"])

    def test_lfe_without_subs_goes_only_to_fronts(self):
        outputs = {item["role"]: item for item in self.plan(self.state(ROLES[:7]))["outputs"]}
        self.assertEqual(outputs["main_l"]["routes"], [{"input": 0, "gain": 1.0}, {"input": 3, "gain": 0.5}])
        self.assertEqual(outputs["main_r"]["routes"], [{"input": 1, "gain": 1.0}, {"input": 3, "gain": 0.5}])
        self.assertEqual(outputs["center"]["routes"], [{"input": 2, "gain": 1.0}])

    def test_lfe_bypasses_bass_lowpass_but_shares_sub_correction(self):
        state = self.state()
        state["modes"]["surround"]["processing"]["sub1"]["highpass"] = {
            "family": "butterworth", "slope_db_oct": 12, "frequency_hz": 25}
        layout = {item["name"]: item for item in layout_from_plan(self.plan(state), resolve_ir=lambda _bank, name: {})}
        sub = layout["sub1"]
        self.assertEqual(sub["post_crossover_routes"], [{"input": 3, "gain": 1.0}])
        self.assertEqual(len(sub["crossover_sos"]), 2)
        self.assertEqual(len(sub["sos"]), 1)

    def test_v3_migration_retains_all_existing_mode_settings(self):
        old = default_output_state()
        old["version"] = 3
        old["modes"].pop("surround", None)
        old.pop("input_modes", None)
        old["revision"] = 17
        old["modes"]["stereo-sub"]["bass_management"]["frequency_hz"] = 95
        before = copy.deepcopy(old)
        upgraded = upgrade_output_state(old)
        self.assertEqual(upgraded["revision"], 17)
        self.assertEqual(upgraded["modes"]["stereo-sub"], old["modes"]["stereo-sub"])
        self.assertIn("surround", upgraded["modes"])
        self.assertEqual(upgraded["input_modes"], {})
        self.assertEqual(old, before)

    def test_surround_sub_filters_must_be_keyed_by_speaker_group(self):
        state = self.state()
        stale = {"family": "butterworth", "slope_db_oct": 12, "frequency_hz": 90.0}
        state["modes"]["stereo-sub"]["bass_management"]["sub_filters"] = {"left": dict(stale)}
        self.assertEqual(upgrade_output_state(state)["modes"]["stereo-sub"]["bass_management"]["sub_filters"],
                         {"left": dict(stale)})
        for keys in ({"left": dict(stale)}, {"rear_l": group_filter(95)}):
            with self.subTest(keys=list(keys)):
                state["modes"]["surround"]["bass_management"]["sub_filters"] = keys
                with self.assertRaisesRegex(ValueError, "keyed by front"):
                    upgrade_output_state(state)

    def test_engine_text_retains_eight_positions_and_separate_lfe_send(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = DSPManager(Path(directory) / "presets", Path(directory) / "irs")
            layout = layout_from_plan(self.plan(self.state()), resolve_ir=lambda _bank, name: {})
            text = manager.compile_engine_text(layout, preset_name="Direct")
        self.assertIn("inputs 8\nsurround 1\n", text)
        self.assertIn("postmatrix 7 3 1\n", text)
        self.assertIn("xsos 7 ", text)

    def test_surround_runtime_keeps_eight_inputs_even_with_only_front_outputs(self):
        plan = self.plan(self.state(["main_l", "main_r"]))
        layout = layout_from_plan(plan, resolve_ir=lambda _bank, name: {})
        config = DSPRuntimeConfig.from_plan(plan, layout=layout, output_key="A", sample_rate_hz=48000,
                                            hardware_ports=["playback_AUX0", "playback_AUX1"])
        self.assertEqual(config.input_count, 8)

    def routes(self, roles, state=None):
        return {item["role"]: item["routes"] for item in self.plan(state or self.state(roles))["outputs"]}

    def test_missing_center_and_sides_fold_down_instead_of_going_silent(self):
        routes = self.routes(QUAD_SUBS)
        self.assertEqual(routes["main_l"], [{"input": 0, "gain": 1.0}, {"input": 2, "gain": FOLD_GAIN}])
        self.assertEqual(routes["main_r"], [{"input": 1, "gain": 1.0}, {"input": 2, "gain": FOLD_GAIN}])
        self.assertEqual(routes["rear_l"], [{"input": 6, "gain": 1.0}, {"input": 4, "gain": 1.0}])
        self.assertEqual(routes["rear_r"], [{"input": 7, "gain": 1.0}, {"input": 5, "gain": 1.0}])
        self.assertAlmostEqual(FOLD_GAIN, 10 ** (-3.0103 / 20), places=4)

    def test_rear_program_folds_to_side_speakers(self):
        routes = self.routes(["main_l", "main_r", "center", "side_l", "side_r", "sub1"])
        self.assertEqual(routes["side_l"], [{"input": 4, "gain": 1.0}, {"input": 6, "gain": 1.0}])
        self.assertEqual(routes["side_r"], [{"input": 5, "gain": 1.0}, {"input": 7, "gain": 1.0}])
        self.assertEqual(routes["center"], [{"input": 2, "gain": 1.0}])

    def test_fronts_alone_carry_every_position_and_lfe(self):
        routes = self.routes(["main_l", "main_r"])
        self.assertEqual(routes["main_l"], [{"input": 0, "gain": 1.0}, {"input": 2, "gain": FOLD_GAIN},
                                            {"input": 4, "gain": FOLD_GAIN}, {"input": 6, "gain": FOLD_GAIN},
                                            {"input": 3, "gain": 0.5}])
        self.assertEqual(routes["main_r"], [{"input": 1, "gain": 1.0}, {"input": 2, "gain": FOLD_GAIN},
                                            {"input": 5, "gain": FOLD_GAIN}, {"input": 7, "gain": FOLD_GAIN},
                                            {"input": 3, "gain": 0.5}])

    def test_crossover_ways_reproduce_the_folded_center(self):
        state = set_crossover(self.state(QUAD_SUBS), "surround", True)
        state = set_mode_routing(state, "surround", "A",
                                 ["left_low", "left_high", "right_low", "right_high", *QUAD_SUBS[2:]])
        routes = self.routes(None, state)
        for role in ("left_low", "left_high"):
            self.assertEqual(routes[role], [{"input": 0, "gain": 1.0}, {"input": 2, "gain": FOLD_GAIN}])
        self.assertEqual(routes["right_high"], [{"input": 1, "gain": 1.0}, {"input": 2, "gain": FOLD_GAIN}])

    def test_incomplete_surround_pairs_are_not_activatable(self):
        for roles in (["main_l", "main_r", "side_l"], ["main_l", "main_r", "rear_r", "sub1"]):
            with self.subTest(roles=roles), self.assertRaises(ValueError):
                derive_topology("surround", roles).require_activatable()

    def test_linked_groups_keep_one_shared_sub_bus_and_highpass(self):
        outputs = {item["role"]: item for item in self.plan(self.state(QUAD_SUBS))["outputs"]}
        shared = {"family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 80}
        self.assertEqual(outputs["sub1"]["routes"], [{"input": i, "gain": 0.5} for i in (0, 1, 2, 4, 5, 6, 7)])
        self.assertNotIn("bass_groups", outputs["sub1"])
        self.assertEqual(outputs["sub1"]["crossover"], [{"kind": "lowpass", **shared}])
        for role in ("main_l", "rear_r"):
            self.assertEqual(outputs[role]["crossover"], [{"kind": "highpass", **shared}])

    def test_unlinked_groups_split_speaker_highpass_and_sub_lowpass(self):
        state = set_bass_management(self.state(QUAD_SUBS), "surround", sub_link=False, sub_filters={
            "front": group_filter(60, highpass_enabled=False), "rear": group_filter(120, family="butterworth",
                                                                                     slope_db_oct=12)})
        outputs = {item["role"]: item for item in self.plan(state)["outputs"]}
        self.assertEqual(outputs["main_l"]["crossover"], [])
        self.assertEqual(outputs["rear_l"]["crossover"], [{"kind": "highpass", "family": "butterworth",
                                                           "slope_db_oct": 12, "frequency_hz": 120}])
        for role in ("sub1", "sub2"):
            sub = outputs[role]
            # Folded Center plays on the fronts, so its bass follows the Front group.
            self.assertEqual(sub["routes"], [{"input": i, "gain": 0.5} for i in (0, 1, 2)])
            self.assertEqual(sub["crossover"], [{"kind": "lowpass", "family": "linkwitz-riley",
                                                 "slope_db_oct": 24, "frequency_hz": 60}])
            self.assertEqual(sub["bass_groups"], [{"crossover": {"kind": "lowpass", "family": "butterworth",
                                                                 "slope_db_oct": 12, "frequency_hz": 120},
                                                   "routes": [{"input": i, "gain": 0.5} for i in (4, 5, 6, 7)]}])
            self.assertEqual(sub["lfe_routes"], [{"input": 3, "gain": 1.0}])
        rendered = rendered_crossover_filters(state, output_key="A", channels=16)
        self.assertEqual(rendered["sub1"], outputs["sub1"]["crossover"])

    def test_groups_sharing_the_front_crossover_merge_into_one_bus(self):
        state = set_bass_management(self.state(), "surround", sub_link=False, sub_filters={
            "center": group_filter(80, highpass_enabled=False), "side": group_filter(100),
            "rear": group_filter(100)})
        outputs = {item["role"]: item for item in self.plan(state)["outputs"]}
        self.assertEqual(outputs["sub1"]["routes"], [{"input": i, "gain": 0.5} for i in (0, 1, 2)])
        self.assertEqual([group["routes"] for group in outputs["sub1"]["bass_groups"]],
                         [[{"input": i, "gain": 0.5} for i in (4, 5, 6, 7)]])
        self.assertEqual(outputs["center"]["crossover"], [])

    def test_group_overrides_are_validated_per_mode(self):
        state = self.state(QUAD_SUBS)
        for overrides in ({"left": group_filter(80)}, {"rear": {"family": "linkwitz-riley", "slope_db_oct": 24,
                                                                "frequency_hz": 80}},
                          {"rear": group_filter(30)}, {"rear": group_filter(80, highpass_enabled="yes")}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                set_bass_management(state, "surround", sub_filters=overrides)
        with self.assertRaises(ValueError):
            set_bass_management(state, "stereo-sub", sub_filters={"front": group_filter(80)})
        cleared = set_bass_management(set_bass_management(state, "surround", sub_filters={"rear": group_filter(90)}),
                                      "surround", sub_filters={"rear": None})
        self.assertEqual(cleared["modes"]["surround"]["bass_management"]["sub_filters"], {})

    def test_engine_text_low_passes_each_group_bus_separately(self):
        state = set_bass_management(self.state(QUAD_SUBS), "surround", sub_link=False,
                                    sub_filters={"rear": group_filter(120)})
        with tempfile.TemporaryDirectory() as directory:
            manager = DSPManager(Path(directory) / "presets", Path(directory) / "irs")
            layout = layout_from_plan(self.plan(state), resolve_ir=lambda _bank, name: {})
            text = manager.compile_engine_text(layout, preset_name="Direct")
        lines = text.splitlines()
        self.assertEqual([line for line in lines if line.startswith("gmatrix 4 ")],
                         [f"gmatrix 4 1 {i} 0.5" for i in (4, 5, 6, 7)])
        self.assertEqual(sum(line.startswith("gxsos 4 1 ") for line in lines), 2)
        self.assertEqual(sum(line.startswith("xsos 4 ") for line in lines), 2)
        self.assertEqual([line for line in lines if line.startswith("matrix 4 ")],
                         [f"matrix 4 {i} 0.5" for i in (0, 1, 2)])

    def test_overview_reports_the_front_group_sub_crossover(self):
        from audio.samplerate.overview import _derived_output_mode
        state = set_bass_management(self.state(QUAD_SUBS), "surround", sub_link=False, sub_filters={
            "front": group_filter(100, highpass_enabled=False), "rear": group_filter(120)})
        derived = _derived_output_mode(state, "A")
        self.assertEqual(derived["mode"], "subwoofer-2.2")
        self.assertEqual((derived["crossover_frequency_hz"], derived["main_highpass_enabled"]), (100, False))
        self.assertEqual(derived["subwoofers"]["sub2"]["crossover_frequency_hz"], 100)

    def test_surround_offers_the_global_bank_like_every_other_mode(self):
        state = self.state(QUAD_SUBS)
        config = state["modes"]["surround"]
        self.assertEqual(config["selected_bank"], "global")
        self.assertIn("global", bank_catalog(config, QUAD_SUBS))
        self.assertEqual(selected_bank(config, QUAD_SUBS, "surround"), "global")
        stored_all = {**config, "selected_bank": "all"}
        self.assertEqual(selected_bank(stored_all, QUAD_SUBS, "surround"), "all")
        gone = {**config, "selected_bank": "side"}
        self.assertEqual(selected_bank(gone, QUAD_SUBS, "surround"), "all")
        selected = select_bank(state, "surround", "A", 16, "global")
        self.assertEqual(selected["modes"]["surround"]["selected_bank"], "global")
        assigned = set_bank_preset(selected, "surround", "global", preset="Room", group="rear")
        self.assertEqual(assigned["modes"]["surround"]["banks"]["global"]["preset"],
                         {**neutral_selection(), "rear": "Room"})
        # Other banks stay untouched by a Global assignment.
        self.assertEqual(assigned["modes"]["surround"]["banks"]["main_l"]["preset"], "Neutral")
        self.assertEqual(select_bank(state, "surround", "A", 16, "main")["modes"]["surround"]["selected_bank"], "main")
        fronts = self.state(["main_l", "main_r"])
        self.assertEqual(fronts["modes"]["surround"]["selected_bank"], "global")
        self.assertEqual(select_bank(fronts, "surround", "A", 16, "all")["modes"]["surround"]["selected_bank"], "all")
        # Re-routing keeps a Global selection instead of falling back to All Banks.
        rerouted = set_mode_routing(selected, "surround", "A", ROLES)
        self.assertEqual(rerouted["modes"]["surround"]["selected_bank"], "global")

    def test_surround_global_slots_are_group_selections(self):
        state = self.state(QUAD_SUBS)
        self.assertEqual(state["modes"]["surround"]["banks"]["global"],
                         {"preset": neutral_selection(), "preset_a": neutral_selection(), "preset_b": neutral_selection()})
        self.assertEqual(state["modes"]["stereo"]["banks"]["global"]["preset"], "Neutral")
        for slot in ("Room", "Neutral", {"front": "Room"}, {**neutral_selection(), "front": "Direct"}):
            broken = copy.deepcopy(state)
            broken["modes"]["surround"]["banks"]["global"] = {"preset": slot, "preset_a": slot, "preset_b": None}
            with self.subTest(slot=slot), self.assertRaises(ValueError):
                validate_output_state(broken)
        broken = copy.deepcopy(state)
        broken["modes"]["stereo"]["banks"]["global"] = {"preset": neutral_selection(),
                                                        "preset_a": neutral_selection(), "preset_b": None}
        with self.assertRaisesRegex(ValueError, "Only Surround Global"):
            validate_output_state(broken)

    def test_version_six_surround_global_slots_become_group_selections(self):
        state = self.state(QUAD_SUBS)
        state["version"] = 6
        state["modes"]["surround"]["banks"]["global"] = {"preset": "Rear FIR", "preset_a": "Rear FIR",
                                                         "preset_b": "Neutral"}
        upgraded = upgrade_output_state(state)
        self.assertEqual(upgraded["version"], 7)
        # A named preset has no known group: both slots start without filters.
        self.assertEqual(upgraded["modes"]["surround"]["banks"]["global"],
                         {"preset": neutral_selection(), "preset_a": neutral_selection(), "preset_b": neutral_selection()})
        state["modes"]["surround"]["banks"]["global"] = {"preset": "Direct", "preset_a": "Room", "preset_b": "Direct"}
        self.assertEqual(upgrade_output_state(state)["modes"]["surround"]["banks"]["global"],
                         {"preset": "Direct", "preset_a": neutral_selection(), "preset_b": "Direct"})
        self.assertEqual(upgraded["modes"]["stereo"], state["modes"]["stereo"])

    def test_side_and_rear_banks_project_dual_peq_to_correct_side(self):
        state = self.state()
        state["modes"]["surround"]["banks"]["side_r"]["preset"] = "Side EQ"
        chain = [{"type": "equalizer", "params": {"channelMode": "dual", "leftBands": [],
                  "rightBands": [{"filterType": "gain", "gainDb": -3}]}}]
        plan = compile_processing_plan(state, output_key="A", channels=16, sample_rate_hz=48000,
                                       preset_loader=lambda _mode, _bank, name: {"chain": chain if name == "Side EQ" else []})
        layout = {item["name"]: item for item in layout_from_plan(plan, resolve_ir=lambda _bank, name: {})}
        self.assertEqual(layout["side_r"]["gain_db"], -3)


def eq_plugin(group, identifier, gain_db=-3.0, **extra):
    band = {"enabled": True, "filterType": "bell", "frequencyHz": 100.0, "gainDb": gain_db, "q": 2.0, "delayMs": 0.0}
    return {"id": identifier, "type": "equalizer", "enabled": True, "group": group,
            "params": {"channelMode": "stereo-linked", "eqMode": "IIR", "bands": [band]}, **extra}


def group_preset(group, gain_db=-3.0):
    return {"chain": [eq_plugin(group, "equalizer#0", gain_db)], "metadata": {"layout": "surround"}}


def selection(**groups):
    return {**neutral_selection(), **groups}


class SurroundGlobalPresetTests(unittest.TestCase):
    """Surround Global: each A/B slot selects one preset per speaker group."""

    ALL_GROUPS = ["front", "center", "side", "rear"]
    EVERY_GROUP = {group: f"EQ {group}" for group in ALL_GROUPS}

    def state(self, roles, slot=None):
        state = switch_mode(set_mode_routing(default_output_state(), "surround", "A", roles), "surround")
        slot = slot or selection(**self.EVERY_GROUP)
        state["modes"]["surround"]["banks"]["global"] = {"preset": slot, "preset_a": slot, "preset_b": None}
        return validate_output_state(state)

    def presets(self, **extra):
        return {**{f"EQ {group}": group_preset(group) for group in self.ALL_GROUPS}, **extra}

    def plan(self, state, presets=None):
        payloads = presets or self.presets()
        return compile_processing_plan(state, output_key="A", channels=16, sample_rate_hz=48000,
                                       preset_loader=lambda _mode, _bank, name: payloads.get(name, {"chain": [], "metadata": {}}))

    def test_plan_runs_the_selected_preset_of_every_routed_group(self):
        plan = self.plan(self.state(QUAD_SUBS))
        self.assertEqual([plugin["group"] for plugin in plan["global"]["chain"]], ["front", "rear"])
        self.assertEqual(plan["global"]["preset"], selection(**self.EVERY_GROUP))
        self.assertEqual(plan["global"]["extras"], {})
        everything = self.plan(self.state(ROLES))
        # Each group's preset numbers its plugins from zero; the chain renumbers them.
        self.assertEqual([(plugin["group"], plugin["id"]) for plugin in everything["global"]["chain"]],
                         [(group, f"equalizer#{index}") for index, group in enumerate(self.ALL_GROUPS)])
        nothing = self.plan(self.state(ROLES, neutral_selection()))
        self.assertEqual((nothing["global"]["chain"], nothing["global"]["bypass"]), ([], False))

    def test_group_assignment_replaces_only_its_group_in_the_slot(self):
        state = self.state(QUAD_SUBS, selection(front="EQ front"))
        state = set_bank_preset(state, "surround", "global", preset="EQ rear", group="rear")
        bank = state["modes"]["surround"]["banks"]["global"]
        self.assertEqual(bank["preset_a"], selection(front="EQ front", rear="EQ rear"))
        self.assertEqual(bank["preset"], bank["preset_a"])
        # A legacy unassigned B starts from A; equal selections are allowed.
        state = set_bank_preset(state, "surround", "global", preset_b="EQ rear B", group="rear")
        equal = set_bank_preset(state, "surround", "global", preset_b="EQ rear", group="rear")
        self.assertEqual(equal["modes"]["surround"]["banks"]["global"]["preset_a"],
                         equal["modes"]["surround"]["banks"]["global"]["preset_b"])
        # Editing the listened A keeps listening to it.
        state = set_bank_preset(state, "surround", "global", preset_a="Neutral", group="front")
        bank = state["modes"]["surround"]["banks"]["global"]
        self.assertEqual(bank["preset_b"], selection(front="EQ front", rear="EQ rear B"))
        self.assertEqual(bank["preset"], selection(rear="EQ rear"))
        self.assertEqual(bank["preset"], bank["preset_a"])
        with self.assertRaisesRegex(ValueError, "name the group"):
            set_bank_preset(state, "surround", "global", preset="EQ rear")
        with self.assertRaisesRegex(ValueError, "Surround Global bank only"):
            set_bank_preset(state, "surround", "main", preset="Room", group="front")
        # Direct replaces the whole selection; a group pick starts a fresh one.
        direct = set_bank_preset(state, "surround", "global", preset_b="Direct", group="rear")
        self.assertEqual(direct["modes"]["surround"]["banks"]["global"]["preset_b"], "Direct")
        picked = set_bank_preset(direct, "surround", "global", preset_b="EQ front", group="front")
        self.assertEqual(picked["modes"]["surround"]["banks"]["global"]["preset_b"], selection(front="EQ front"))

    def test_global_compare_switches_the_whole_selection_without_changing_area_banks(self):
        state = self.state(ROLES, selection(front="EQ front", rear="EQ rear"))
        state = set_bank_preset(state, "surround", "global", preset_b="EQ rear B", group="rear")
        before = copy.deepcopy(state)
        presets = self.presets(**{"EQ rear B": group_preset("rear", -6.0)})
        gains = lambda plan: [(plugin["group"], plugin["params"]["bands"][0]["gainDb"]) for plugin in plan["global"]["chain"]]
        self.assertEqual(gains(self.plan(state, presets)), [("front", -3.0), ("rear", -3.0)])
        switched = set_bank_preset(state, "surround", "global", active_side="B")
        self.assertEqual(gains(self.plan(switched, presets)), [("front", -3.0), ("rear", -6.0)])
        for role in ROLES:
            self.assertEqual(switched["modes"]["surround"]["banks"][role], before["modes"]["surround"]["banks"][role])
        self.assertEqual(switched["modes"]["stereo"], before["modes"]["stereo"])

    def test_deleting_a_preset_releases_only_its_group(self):
        state = self.state(QUAD_SUBS, selection(front="EQ front", rear="EQ rear"))
        state = set_bank_preset(state, "surround", "global", preset_b="EQ rear B", group="rear")
        state = set_bank_preset(state, "surround", "global", active_side="B")
        self.assertEqual(referenced_presets(state, "surround", "global"), {"EQ front", "EQ rear", "EQ rear B", "Neutral"})
        released, changes = release_bank_preset(state, "surround", "global", "EQ front")
        bank = released["modes"]["surround"]["banks"]["global"]
        self.assertEqual((bank["preset_a"], bank["preset_b"]), (selection(rear="EQ rear"), selection(rear="EQ rear B")))
        self.assertEqual(bank["preset"], bank["preset_b"])
        self.assertEqual(len(changes), 1)
        # The group falls back to Neutral; B keeps its other groups and stays listened.
        released, _ = release_bank_preset(released, "surround", "global", "EQ rear B")
        bank = released["modes"]["surround"]["banks"]["global"]
        self.assertEqual((bank["preset_b"], bank["preset"]), (neutral_selection(), neutral_selection()))
        # A B left equal to A stays assigned and listened.
        state = self.state(QUAD_SUBS, selection(rear="EQ rear"))
        state = set_bank_preset(state, "surround", "global", preset_b="EQ front", group="front", active_side="B")
        released, _ = release_bank_preset(state, "surround", "global", "EQ front")
        self.assertEqual(released["modes"]["surround"]["banks"]["global"],
                         {"preset": selection(rear="EQ rear"), "preset_a": selection(rear="EQ rear"),
                          "preset_b": selection(rear="EQ rear"), "active_side": "B"})

    def test_crossover_ways_count_as_the_front_group(self):
        state = set_crossover(self.state(QUAD_SUBS), "surround", True)
        state = set_mode_routing(state, "surround", "A",
                                 ["left_low", "left_high", "right_low", "right_high", *QUAD_SUBS[2:]])
        plan = self.plan(validate_output_state(state))
        self.assertEqual([plugin["group"] for plugin in plan["global"]["chain"]], ["front", "rear"])

    def test_direct_bypasses_and_changes_with_the_routed_speakers(self):
        direct = self.plan(self.state(QUAD_SUBS, "Direct"))
        self.assertEqual((direct["global"]["bypass"], direct["global"]["chain"]), (True, []))
        before = self.plan(self.state(QUAD_SUBS))
        after = self.plan(self.state(ROLES))
        self.assertNotEqual(before["global"]["chain"], after["global"]["chain"])

    def test_surround_chain_must_be_tagged_peq_or_convolver(self):
        state = self.state(QUAD_SUBS, selection(front="Bad"))
        untagged = {"chain": [{k: v for k, v in eq_plugin("front", "equalizer#0").items() if k != "group"}],
                    "metadata": {"layout": "surround"}}
        with self.assertRaisesRegex(ValueError, "needs a speaker group"):
            self.plan(state, {"Bad": untagged})
        limiter = {"chain": [{"id": "limiter#0", "type": "limiter", "enabled": True, "group": "front", "params": {}}],
                   "metadata": {"layout": "surround"}}
        with self.assertRaisesRegex(ValueError, "only PEQ and convolver"):
            self.plan(state, {"Bad": limiter})
        delayed = eq_plugin("front", "equalizer#0")
        delayed["params"]["bands"].append({"enabled": True, "filterType": "delay", "delayMs": 1.0})
        with self.assertRaisesRegex(ValueError, "delay band"):
            self.plan(state, {"Bad": {"chain": [delayed], "metadata": {"layout": "surround"}}})
        mixed = {"chain": [eq_plugin("front", "equalizer#0"), eq_plugin("rear", "equalizer#1")],
                 "metadata": {"layout": "surround"}}
        with self.assertRaisesRegex(ValueError, "mixes speaker groups"):
            self.plan(state, {"Bad": mixed})
        with self.assertRaisesRegex(ValueError, "holds rear filters, not front filters"):
            self.plan(state, {"Bad": group_preset("rear")})

    def manager(self, directory):
        return DSPManager(Path(directory) / "presets", Path(directory) / "irs")

    def test_global_slot_accepts_only_presets_of_its_own_mode_and_group(self):
        # Surround Global holds speaker-group presets, the other modes stereo
        # ones; each mode's Global bank lists and accepts only its own.
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            manager.create_peq_preset("Room", self.peq(), bank="global", mode="stereo")
            manager.create_peq_preset("Front EQ", self.peq(), bank="global", mode="surround", group="front")
            service = OutputService(OutputServiceDeps(store=None, preset_loader=manager.read_preset,
                                                      resolve_ir=lambda *_: {}, measurement_active=lambda: False))
            surround, stereo = self.state(QUAD_SUBS), validate_output_state(default_output_state())
            for name in ("Front EQ", "Neutral", "Direct"):
                service.validate_bank_preset(surround, "surround", "global", name, group="front")
            with self.assertRaisesRegex(ValueError, "holds front filters, not rear filters"):
                service.validate_bank_preset(surround, "surround", "global", "Front EQ", group="rear")
            with self.assertRaises(FileNotFoundError):
                service.validate_bank_preset(surround, "surround", "global", "Room", group="front")
            for name in ("Room", "Neutral", "Direct"):
                service.validate_bank_preset(stereo, "stereo", "global", name)
            with self.assertRaises(FileNotFoundError):
                service.validate_bank_preset(stereo, "stereo", "global", "Front EQ")
            listed = {entry["name"]: entry["group"] for entry in manager.list_presets()}
            self.assertEqual((listed["Front EQ"], listed["Room"], listed["Neutral"]), ("front", None, None))

    def peq(self):
        band = {"enabled": True, "filterType": "bell", "frequencyHz": 120.0, "gainDb": -4.0, "q": 3.0}
        return {"enabled": True, "params": {"channelMode": "stereo-linked", "eqMode": "IIR", "bands": [band]}}

    def engine_lines(self, manager, state):
        plan = compile_processing_plan(state, output_key="A", channels=16, sample_rate_hz=48000,
                                       preset_loader=manager.read_preset)
        layout = layout_from_plan(plan, resolve_ir=lambda *_: {})
        text = manager.compile_engine_text(layout, preset_name=plan["global"]["preset"], mode=plan["mode"])
        return [Path(json.loads(line[len("param path "):])).name if line.startswith("param path") else line
                for line in text.splitlines() if line.startswith(("stage_begin", "pairs", "param path"))]

    def test_engine_text_runs_each_group_preset_on_its_effect_pairs_only(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            for group in self.ALL_GROUPS:
                manager.create_peq_preset(f"EQ {group}", self.peq(), bank="global", mode="surround", group=group)
            stages = [line for line in self.engine_lines(manager, self.state(QUAD_SUBS))
                      if line.startswith(("stage_begin", "pairs"))]
        # Center and Side have no routed speaker in QUAD_SUBS, so only Front (1) and Rear (16) render.
        eq = "lv2 http://lsp-plug.in/plugins/lv2/para_equalizer_x32_lr"
        # The global helpers (default limiter) stay untagged and run on every pair.
        self.assertEqual(stages[:4], [f"stage_begin 0 equalizer#0 {eq}", "pairs 1",
                                      f"stage_begin 1 equalizer#1 {eq}", "pairs 16"])
        self.assertEqual([line for line in stages[4:] if line.startswith("pairs")], [])
        self.assertEqual(len(stages), 5)

    def ir(self, path, channels=2):
        frames = [1.0] + [0.0] * 63
        data = b"".join(struct.pack("<f", value) * channels for value in frames)
        path.write_bytes(build_wav_bytes(channels=channels, rate=48000, bits=32, format_tag=3, data=data))
        return path

    def test_front_and_rear_convolvers_run_together_and_switch_as_one_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            source = self.ir(Path(directory) / "ir.wav")
            for name, group in (("Front FIR", "front"), ("Rear FIR", "rear"), ("Rear FIR B", "rear")):
                manager.create_convolver_preset_with_upload(name, source, "ir.wav", bank="global",
                                                            mode="surround", group=group)
            state = self.state(QUAD_SUBS, selection(front="Front FIR", rear="Rear FIR"))
            state = set_bank_preset(state, "surround", "global", preset_b="Rear FIR B", group="rear")
            # Slot A: both groups' convolvers at once, each on its own effect pairs.
            self.assertEqual(self.engine_lines(manager, state)[:6], [
                "stage_begin 0 convolver#0 native convolver", "pairs 1", "Front FIR.wav",
                "stage_begin 1 convolver#1 native convolver", "pairs 16", "Rear FIR.wav"])
            # A/B switches the whole selection: B keeps Front and changes Rear.
            switched = set_bank_preset(state, "surround", "global", active_side="B")
            self.assertEqual(self.engine_lines(manager, switched)[:6], [
                "stage_begin 0 convolver#0 native convolver", "pairs 1", "Front FIR.wav",
                "stage_begin 1 convolver#1 native convolver", "pairs 16", "Rear FIR B.wav"])

    def test_group_tag_is_only_valid_on_the_surround_global_bank_and_known_groups(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            for bank, mode, group, message in (
                    ("main", None, "front", "Global bank only"),
                    ("global", "surround", "lfe", "Unknown speaker group"),
                    (None, None, "front", "fits only the Surround Global bank"),
                    ("global", "stereo-sub", "front", "fits only the Surround Global bank"),
                    ("global", "surround", None, "need a speaker group")):
                with self.subTest(bank=bank, mode=mode, group=group), self.assertRaisesRegex(ValueError, message):
                    manager.create_peq_preset("Bad", self.peq(), bank=bank, mode=mode, group=group)
            self.assertEqual([entry["name"] for entry in manager.list_presets()], ["Direct", "Neutral"])

    def test_layouts_live_in_separate_global_banks(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            manager.create_peq_preset("Stereo EQ", self.peq(), bank="global")
            manager.create_peq_preset("Front EQ", self.peq(), bank="global", mode="surround", group="front")
            manager.create_peq_preset("Front EQ 2", self.peq(), bank="global", mode="surround", group="front")
            manager.create_peq_preset("Rear EQ", self.peq(), bank="global", mode="surround", group="rear")
            # The same name may exist in both banks, each with its own layout.
            manager.create_peq_preset("Front EQ", self.peq(), bank="global")
            self.assertIsNone(manager.library("global", "stereo").read("Front EQ")["metadata"].get("layout"))
            self.assertEqual(manager.library("global", "surround").read("Front EQ")["metadata"]["layout"], "surround")
            # Combine reads its sources from the bank it writes to, within one speaker group.
            with self.assertRaises(FileNotFoundError):
                manager.combine_presets("Mixed", ["Stereo EQ", "Rear EQ"], bank="global", mode="surround")
            with self.assertRaisesRegex(ValueError, "mixes speaker groups"):
                manager.combine_presets("Both", ["Front EQ", "Rear EQ"], bank="global", mode="surround")
            combined = manager.combine_presets("Fronts", ["Front EQ", "Front EQ 2"], bank="global", mode="surround")
            chain = manager.library("global", "surround").read("Fronts")["chain"]
            self.assertEqual([plugin["group"] for plugin in chain], ["front", "front"])
            self.assertEqual(combined["plugin_count"], 2)

    def test_surround_preset_cannot_drive_a_stereo_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            manager.create_peq_preset("Front EQ", self.peq(), bank="global", mode="surround", group="front")
            stereo = [{"name": "left", "source": 0}, {"name": "right", "source": 1}]
            with self.assertRaisesRegex(ValueError, "select it per group"):
                manager.compile_engine_text(stereo, preset_name="Front EQ", mode="surround")
            with self.assertRaisesRegex(ValueError, "needs the Surround output mode"):
                manager.compile_engine_text(stereo, preset_name=selection(front="Front EQ"), mode="surround")

    def test_store_refuses_group_tags_without_the_surround_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            payload = {"schema": "fxroute.dsp.preset", "version": 1, "metadata": {},
                       "chain": [eq_plugin("front", "equalizer#0")]}
            with self.assertRaisesRegex(ValueError, "require the Surround preset layout"):
                manager.library().write("Tagged", payload)
            payload["chain"][0]["group"] = "lfe"
            with self.assertRaisesRegex(ValueError, "group must be one of"):
                manager.library().write("Tagged", payload)


if __name__ == "__main__":
    unittest.main()
