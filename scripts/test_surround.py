#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Position routing, separate LFE, shared subs and lossless Surround migration."""

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.filter_banks import bank_definitions
from audio.output_state import default_output_state, set_crossover, set_mode_routing, switch_mode
from audio.output_state_migration import upgrade_output_state
from audio.output_topology import derive_topology, roles_for_mode
from dsp.native_config import layout_from_plan
from dsp.processing_plan import compile_processing_plan
from dsp.manager import DSPManager
from dsp.runtime import DSPRuntimeConfig
import tempfile


ROLES = ["main_l", "main_r", "center", "side_l", "side_r", "rear_l", "rear_r", "sub1", "sub2"]


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
