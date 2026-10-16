#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Position routing, separate LFE, shared subs and lossless Surround migration."""

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.filter_banks import bank_catalog, bank_definitions, selected_bank
from audio.output_state import (default_output_state, select_bank, set_bank_preset, set_bass_management,
                                set_crossover, set_mode_routing, switch_mode, validate_output_state)
from audio.output_state_migration import upgrade_output_state
from audio.output_topology import derive_topology, roles_for_mode
from audio.output_service import OutputService, OutputServiceDeps
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
        assigned = set_bank_preset(selected, "surround", "global", preset="Room")
        self.assertEqual(assigned["modes"]["surround"]["banks"]["global"]["preset"], "Room")
        # Other banks stay untouched by a Global assignment.
        self.assertEqual(assigned["modes"]["surround"]["banks"]["main_l"]["preset"], "Neutral")
        self.assertEqual(select_bank(state, "surround", "A", 16, "main")["modes"]["surround"]["selected_bank"], "main")
        fronts = self.state(["main_l", "main_r"])
        self.assertEqual(fronts["modes"]["surround"]["selected_bank"], "global")
        self.assertEqual(select_bank(fronts, "surround", "A", 16, "all")["modes"]["surround"]["selected_bank"], "all")
        # Re-routing keeps a Global selection instead of falling back to All Banks.
        rerouted = set_mode_routing(selected, "surround", "A", ROLES)
        self.assertEqual(rerouted["modes"]["surround"]["selected_bank"], "global")

    def test_surround_global_without_surround_layout_renders_neutral(self):
        state = self.state(QUAD_SUBS)
        state["modes"]["surround"]["banks"]["global"] = {"preset": "Room", "preset_a": "Room", "preset_b": None}
        state = validate_output_state(state)
        loaded = []

        def loader(_mode, _bank, name):
            loaded.append(name)
            return {"chain": [{"type": "equalizer", "params": {}}] if name == "Room" else [], "metadata": {}}

        with self.assertLogs("dsp.processing_plan", level="WARNING"):
            plan = compile_processing_plan(state, output_key="A", channels=16, sample_rate_hz=48000, preset_loader=loader)
        self.assertEqual((plan["global"]["preset"], plan["global"]["chain"]), ("Neutral", []))
        self.assertFalse(plan["global"]["bypass"])

    def test_side_and_rear_banks_project_dual_peq_to_correct_side(self):
        state = self.state()
        state["modes"]["surround"]["banks"]["side_r"]["preset"] = "Side EQ"
        chain = [{"type": "equalizer", "params": {"channelMode": "dual", "leftBands": [],
                  "rightBands": [{"filterType": "gain", "gainDb": -3}]}}]
        plan = compile_processing_plan(state, output_key="A", channels=16, sample_rate_hz=48000,
                                       preset_loader=lambda _mode, _bank, name: {"chain": chain if name == "Side EQ" else []})
        layout = {item["name"]: item for item in layout_from_plan(plan, resolve_ir=lambda _bank, name: {})}
        self.assertEqual(layout["side_r"]["gain_db"], -3)


def eq_plugin(group, identifier, **extra):
    band = {"enabled": True, "filterType": "bell", "frequencyHz": 100.0, "gainDb": -3.0, "q": 2.0, "delayMs": 0.0}
    return {"id": identifier, "type": "equalizer", "enabled": True, "group": group,
            "params": {"channelMode": "stereo-linked", "eqMode": "IIR", "bands": [band]}, **extra}


class SurroundGlobalPresetTests(unittest.TestCase):
    """Surround Global: one preset, plugins scoped to speaker groups."""

    ALL_GROUPS = ["front", "center", "side", "rear"]

    def state(self, roles, preset="Room"):
        state = switch_mode(set_mode_routing(default_output_state(), "surround", "A", roles), "surround")
        state["modes"]["surround"]["banks"]["global"] = {"preset": preset, "preset_a": preset, "preset_b": None}
        return validate_output_state(state)

    def preset(self, groups=None):
        chain = [eq_plugin(group, f"equalizer#{index}") for index, group in enumerate(groups or self.ALL_GROUPS)]
        return {"chain": chain, "metadata": {"layout": "surround", "bank": "global"}}

    def plan(self, state, preset=None):
        payload = preset or self.preset()
        return compile_processing_plan(state, output_key="A", channels=16, sample_rate_hz=48000,
                                       preset_loader=lambda _mode, _bank, name: payload if name == "Room" else {"chain": [], "metadata": {}})

    def test_plan_keeps_only_groups_with_a_routed_speaker(self):
        plan = self.plan(self.state(QUAD_SUBS))
        self.assertEqual([plugin["group"] for plugin in plan["global"]["chain"]], ["front", "rear"])
        self.assertEqual(plan["global"]["preset"], "Room")
        self.assertEqual(plan["global"]["extras"], {})
        everything = self.plan(self.state(ROLES))
        self.assertEqual([plugin["group"] for plugin in everything["global"]["chain"]], self.ALL_GROUPS)

    def test_global_compare_switches_every_group_without_changing_area_banks(self):
        state = set_bank_preset(self.state(ROLES), "surround", "global", preset_b="Room B")
        before = copy.deepcopy(state)
        switched = set_bank_preset(state, "surround", "global", active_side="B")
        payload_b = self.preset()
        for plugin in payload_b["chain"]:
            plugin["params"]["bands"][0]["gainDb"] = -6.0
        plan = compile_processing_plan(switched, output_key="A", channels=16, sample_rate_hz=48000,
                                       preset_loader=lambda _mode, _bank, name: payload_b if name == "Room B" else {"chain": []})
        self.assertEqual(plan["global"]["preset"], "Room B")
        self.assertEqual([plugin["group"] for plugin in plan["global"]["chain"]], ["front", "center", "side", "rear"])
        self.assertTrue(all(plugin["params"]["bands"][0]["gainDb"] == -6.0 for plugin in plan["global"]["chain"]))
        self.assertEqual([output["role"] for output in plan["outputs"]], ROLES)
        for role in ROLES:
            self.assertEqual(switched["modes"]["surround"]["banks"][role], before["modes"]["surround"]["banks"][role])
        self.assertEqual(switched["modes"]["stereo"], before["modes"]["stereo"])

    def test_crossover_ways_count_as_the_front_group(self):
        state = set_crossover(self.state(QUAD_SUBS), "surround", True)
        state = set_mode_routing(state, "surround", "A",
                                 ["left_low", "left_high", "right_low", "right_high", *QUAD_SUBS[2:]])
        state["modes"]["surround"]["banks"]["global"] = {"preset": "Room", "preset_a": "Room", "preset_b": None}
        plan = self.plan(validate_output_state(state))
        self.assertEqual([plugin["group"] for plugin in plan["global"]["chain"]], ["front", "rear"])

    def test_direct_bypasses_and_changes_with_the_routed_speakers(self):
        direct = self.plan(self.state(QUAD_SUBS, preset="Direct"))
        self.assertEqual((direct["global"]["bypass"], direct["global"]["chain"]), (True, []))
        before = self.plan(self.state(QUAD_SUBS))
        after = self.plan(self.state(ROLES))
        self.assertNotEqual(before["global"]["chain"], after["global"]["chain"])

    def test_surround_chain_must_be_tagged_peq_or_convolver(self):
        state = self.state(QUAD_SUBS)
        untagged = {"chain": [{k: v for k, v in eq_plugin("front", "equalizer#0").items() if k != "group"}],
                    "metadata": {"layout": "surround"}}
        with self.assertRaisesRegex(ValueError, "needs a speaker group"):
            self.plan(state, untagged)
        limiter = {"chain": [{"id": "limiter#0", "type": "limiter", "enabled": True, "group": "front", "params": {}}],
                   "metadata": {"layout": "surround"}}
        with self.assertRaisesRegex(ValueError, "only PEQ and convolver"):
            self.plan(state, limiter)
        delayed = eq_plugin("front", "equalizer#0")
        delayed["params"]["bands"].append({"enabled": True, "filterType": "delay", "delayMs": 1.0})
        with self.assertRaisesRegex(ValueError, "delay band"):
            self.plan(state, {"chain": [delayed], "metadata": {"layout": "surround"}})

    def manager(self, directory):
        return DSPManager(Path(directory) / "presets", Path(directory) / "irs")

    def test_global_slot_accepts_only_presets_of_its_own_mode(self):
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
                service.validate_bank_preset(surround, "surround", "global", name)
            with self.assertRaises(FileNotFoundError):
                service.validate_bank_preset(surround, "surround", "global", "Room")
            for name in ("Room", "Neutral", "Direct"):
                service.validate_bank_preset(stereo, "stereo", "global", name)
            with self.assertRaises(FileNotFoundError):
                service.validate_bank_preset(stereo, "stereo", "global", "Front EQ")

    def peq(self):
        band = {"enabled": True, "filterType": "bell", "frequencyHz": 120.0, "gainDb": -4.0, "q": 3.0}
        return {"enabled": True, "params": {"channelMode": "stereo-linked", "eqMode": "IIR", "bands": [band]}}

    def test_engine_text_runs_tagged_plugins_on_their_effect_pairs_only(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            for group in ("front", "center", "rear", "side"):
                manager.create_peq_preset(f"EQ {group}", self.peq(), bank="global", mode="surround", group=group)
            manager.combine_presets("Room", ["EQ front", "EQ center", "EQ side", "EQ rear"],
                                    bank="global", mode="surround")
            self.assertEqual(manager.library("global", "surround").read("Room")["metadata"]["layout"], "surround")
            state = self.state(QUAD_SUBS)
            plan = compile_processing_plan(state, output_key="A", channels=16, sample_rate_hz=48000,
                                           preset_loader=manager.read_preset)
            layout = layout_from_plan(plan, resolve_ir=lambda *_: {})
            text = manager.compile_engine_text(layout, preset_name=plan["global"]["preset"], mode=plan["mode"])
        stages = [line for line in text.splitlines() if line.startswith(("stage_begin", "pairs"))]
        # Center and Side have no routed speaker in QUAD_SUBS, so only Front (1) and Rear (16) render.
        eq = "lv2 http://lsp-plug.in/plugins/lv2/para_equalizer_x32_lr"
        # The global helpers (default limiter) stay untagged and run on every pair.
        self.assertEqual(stages[:4], [f"stage_begin 0 equalizer#0 {eq}", "pairs 1",
                                      f"stage_begin 1 equalizer#3 {eq}", "pairs 16"])
        self.assertEqual([line for line in stages[4:] if line.startswith("pairs")], [])
        self.assertEqual(len(stages), 5)

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
            manager.create_peq_preset("Rear EQ", self.peq(), bank="global", mode="surround", group="rear")
            # The same name may exist in both banks, each with its own layout.
            manager.create_peq_preset("Front EQ", self.peq(), bank="global")
            self.assertIsNone(manager.library("global", "stereo").read("Front EQ")["metadata"].get("layout"))
            self.assertEqual(manager.library("global", "surround").read("Front EQ")["metadata"]["layout"], "surround")
            # Combine reads its sources from the bank it writes to.
            with self.assertRaises(FileNotFoundError):
                manager.combine_presets("Mixed", ["Stereo EQ", "Rear EQ"], bank="global", mode="surround")
            combined = manager.combine_presets("Both", ["Front EQ", "Rear EQ"], bank="global", mode="surround")
            chain = manager.library("global", "surround").read("Both")["chain"]
            self.assertEqual([plugin["group"] for plugin in chain], ["front", "rear"])
            self.assertEqual(combined["plugin_count"], 2)

    def test_group_filter_completes_the_listened_set(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            store = manager.library("global", "surround")
            manager.create_peq_preset("Front EQ", self.peq(), bank="global", mode="surround", group="front",
                                      base_preset="Neutral")
            rear = self.peq()
            rear["params"]["bands"][0]["gainDb"] = -2.0
            manager.create_peq_preset("Set 1", rear, bank="global", mode="surround", group="rear",
                                      base_preset="Front EQ")
            chain = store.read("Set 1")["chain"]
            self.assertEqual([(plugin["group"], plugin["id"]) for plugin in chain],
                             [("front", "equalizer#0"), ("rear", "equalizer#1")])
            self.assertEqual(chain[0]["params"], store.read("Front EQ")["chain"][0]["params"])
            # A new filter for a group replaces that group's filters only.
            front = self.peq()
            front["params"]["bands"][0]["gainDb"] = -6.0
            manager.create_peq_preset("Set 2", front, bank="global", mode="surround", group="front",
                                      base_preset="Set 1")
            chain = store.read("Set 2")["chain"]
            self.assertEqual([(plugin["group"], plugin["params"]["bands"][0]["gainDb"]) for plugin in chain],
                             [("front", -6.0), ("rear", -2.0)])
            # The base set is a snapshot: it keeps its own filters.
            self.assertEqual(len(store.read("Set 1")["chain"]), 2)
            listed = {entry["name"]: entry for entry in manager.list_presets()}
            self.assertEqual(listed["Set 2"]["group_filters"], [
                {"group": "front", "type": "equalizer", "enabled": True, "bands": 1},
                {"group": "rear", "type": "equalizer", "enabled": True, "bands": 1}])
            with self.assertRaisesRegex(ValueError, "listened Global preset \"Gone\" is missing"):
                manager.create_peq_preset("Set 3", front, bank="global", mode="surround", group="rear",
                                          base_preset="Gone")
            self.assertFalse(store.path("Set 3").exists())

    def ir(self, path, channels=2):
        from dsp.manager import build_wav_bytes
        import struct
        frames = [1.0] + [0.0] * 63
        data = b"".join(struct.pack("<f", value) * channels for value in frames)
        path.write_bytes(build_wav_bytes(channels=channels, rate=48000, bits=32, format_tag=3, data=data))
        return path

    def test_front_and_rear_convolvers_run_together_and_switch_as_one_set(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            source = self.ir(Path(directory) / "ir.wav")
            manager.create_convolver_preset_with_upload("Front FIR", source, "ir.wav", bank="global",
                                                        mode="surround", group="front", base_preset="Neutral")
            manager.create_convolver_preset_with_upload("Front+Rear FIR", source, "ir.wav", bank="global",
                                                        mode="surround", group="rear", base_preset="Front FIR")
            # An uploaded IR is named after its preset; it must not replace a kept group's IR.
            with self.assertRaisesRegex(ValueError, "IR of another speaker group"):
                manager.create_convolver_preset_with_upload("Front FIR", source, "ir.wav", bank="global",
                                                            mode="surround", group="rear",
                                                            base_preset="Front+Rear FIR")
            store = manager.library("global", "surround")
            self.assertEqual([(plugin["group"], plugin["params"]["kernel"]) for plugin in store.read("Front+Rear FIR")["chain"]],
                             [("front", "Front FIR"), ("rear", "Front+Rear FIR")])
            state = set_bank_preset(self.state(QUAD_SUBS, preset="Front+Rear FIR"), "surround", "global",
                                    preset_b="Front FIR")

            def engine_text(state):
                plan = compile_processing_plan(state, output_key="A", channels=16, sample_rate_hz=48000,
                                               preset_loader=manager.read_preset)
                layout = layout_from_plan(plan, resolve_ir=lambda *_: {})
                text = manager.compile_engine_text(layout, preset_name=plan["global"]["preset"], mode=plan["mode"])
                return [Path(json.loads(line[len("param path "):])).name if line.startswith("param path") else line
                        for line in text.splitlines() if line.startswith(("stage_begin", "pairs", "param path"))]

            # Slot A: both groups' convolvers at once, each on its own effect pairs.
            self.assertEqual(engine_text(state)[:6], [
                "stage_begin 0 convolver#0 native convolver", "pairs 1", "Front FIR.wav",
                "stage_begin 1 convolver#1 native convolver", "pairs 16", "Front+Rear FIR.wav"])
            # A/B switches the whole set: slot B holds the Front filter only.
            switched = set_bank_preset(state, "surround", "global", active_side="B")
            self.assertEqual(engine_text(switched)[:3], [
                "stage_begin 0 convolver#0 native convolver", "pairs 1", "Front FIR.wav"])
            self.assertNotIn("pairs 16", engine_text(switched))

    def test_surround_preset_cannot_drive_a_stereo_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            manager.create_peq_preset("Front EQ", self.peq(), bank="global", mode="surround", group="front")
            with self.assertRaisesRegex(ValueError, "needs the Surround output mode"):
                manager.compile_engine_text([{"name": "left", "source": 0}, {"name": "right", "source": 1}],
                                            preset_name="Front EQ", mode="surround")

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
