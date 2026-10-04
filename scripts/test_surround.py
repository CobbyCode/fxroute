#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Position routing, separate LFE, shared subs and lossless Surround migration."""

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.filter_banks import bank_catalog, bank_definitions, selected_bank
from audio.output_state import (default_output_state, select_bank, set_bank_preset, set_bass_management,
                                set_crossover, set_mode_routing, switch_mode, validate_output_state)
from audio.output_state_migration import upgrade_output_state
from audio.output_topology import derive_topology, roles_for_mode
from dsp.native_config import layout_from_plan
from dsp.processing_plan import FOLD_GAIN, compile_processing_plan, rendered_crossover_filters
from dsp.manager import DSPManager
from dsp.runtime import DSPRuntimeConfig
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
                                       preset_loader=lambda name: {"chain": []})

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
        layout = {item["name"]: item for item in layout_from_plan(self.plan(state), resolve_ir=lambda name: {})}
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

    def test_migration_drops_stale_surround_sub_filter_keys(self):
        # Before the speaker-group scheme Surround overrides were keyed
        # left/right; those were never read and must not brick the load.
        state = self.state()
        stale = {"family": "butterworth", "slope_db_oct": 12, "frequency_hz": 90.0}
        state["modes"]["surround"]["bass_management"]["sub_filters"] = {
            "left": dict(stale), "right": dict(stale), "front": group_filter(95)}
        state["modes"]["stereo-sub"]["bass_management"]["sub_filters"] = {
            "left": dict(stale)}
        upgraded = upgrade_output_state(state)
        self.assertEqual(upgraded["modes"]["surround"]["bass_management"]["sub_filters"],
                         {"front": group_filter(95)})
        self.assertEqual(upgraded["modes"]["stereo-sub"]["bass_management"]["sub_filters"],
                         {"left": dict(stale)})
        state["modes"]["surround"]["bass_management"]["sub_filters"] = {"rear_l": group_filter(95)}
        with self.assertRaisesRegex(ValueError, "keyed by front"):
            upgrade_output_state(state)

    def test_engine_text_retains_eight_positions_and_separate_lfe_send(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = DSPManager(Path(directory) / "presets", Path(directory) / "irs")
            layout = layout_from_plan(self.plan(self.state()), resolve_ir=lambda name: {})
            text = manager.compile_engine_text(layout, preset_name="Direct")
        self.assertIn("inputs 8\nsurround 1\n", text)
        self.assertIn("postmatrix 7 3 1\n", text)
        self.assertIn("xsos 7 ", text)

    def test_surround_runtime_keeps_eight_inputs_even_with_only_front_outputs(self):
        plan = self.plan(self.state(["main_l", "main_r"]))
        layout = layout_from_plan(plan, resolve_ir=lambda name: {})
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
            layout = layout_from_plan(self.plan(state), resolve_ir=lambda name: {})
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

    def test_surround_offers_no_global_bank(self):
        state = self.state(QUAD_SUBS)
        config = state["modes"]["surround"]
        self.assertEqual(config["selected_bank"], "all")
        self.assertNotIn("global", bank_catalog(config, QUAD_SUBS, "surround"))
        self.assertIn("global", bank_catalog(state["modes"]["stereo-sub"], ["main_l", "main_r"], "stereo-sub"))
        legacy = {**config, "selected_bank": "global"}
        self.assertEqual(selected_bank(legacy, QUAD_SUBS, "surround"), "all")
        with self.assertRaises(ValueError):
            select_bank(state, "surround", "A", 16, "global")
        with self.assertRaises(ValueError):
            set_bank_preset(state, "surround", "global", preset="Neutral")
        self.assertEqual(select_bank(state, "surround", "A", 16, "main")["modes"]["surround"]["selected_bank"], "main")
        fronts = self.state(["main_l", "main_r"])
        self.assertEqual(fronts["modes"]["surround"]["selected_bank"], "all")
        self.assertEqual(select_bank(fronts, "surround", "A", 16, "main")["modes"]["surround"]["selected_bank"], "main")

    def test_surround_whole_system_chain_stays_neutral(self):
        state = self.state(QUAD_SUBS)
        state["modes"]["surround"]["banks"]["global"] = {"preset": "Room", "preset_a": "Room", "preset_b": None}
        state = validate_output_state(state)
        loaded = []

        def loader(name):
            loaded.append(name)
            return {"chain": [{"type": "equalizer", "params": {}}] if name == "Room" else []}

        plan = compile_processing_plan(state, output_key="A", channels=16, sample_rate_hz=48000, preset_loader=loader)
        self.assertEqual((plan["global"]["preset"], plan["global"]["chain"]), ("Neutral", []))
        self.assertNotIn("Room", loaded)

    def test_side_and_rear_banks_project_dual_peq_to_correct_side(self):
        state = self.state()
        state["modes"]["surround"]["banks"]["side_r"]["preset"] = "Side EQ"
        chain = [{"type": "equalizer", "params": {"channelMode": "dual", "leftBands": [],
                  "rightBands": [{"filterType": "gain", "gainDb": -3}]}}]
        plan = compile_processing_plan(state, output_key="A", channels=16, sample_rate_hz=48000,
                                       preset_loader=lambda name: {"chain": chain if name == "Side EQ" else []})
        layout = {item["name"]: item for item in layout_from_plan(plan, resolve_ir=lambda name: {})}
        self.assertEqual(layout["side_r"]["gain_db"], -3)


if __name__ == "__main__":
    unittest.main()
