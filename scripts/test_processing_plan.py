#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Global/area separation and role-addressed processing plans using real presets."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.output_state import default_output_state, select_bank, set_mode_routing, switch_mode
from dsp.persistence import DSPPresetStore
from dsp.processing_plan import compile_processing_plan


def crossover_filter(frequency):
    return {"family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": frequency}


class ProcessingPlanTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.presets = DSPPresetStore(Path(directory.name) / "presets", Path(directory.name) / "irs")
        for name, chain in (
            ("Neutral", []), ("Direct", []),
            ("Global EQ", [{"id": "eq", "type": "equalizer", "params": {"bands": [{"filterType": "bell", "frequencyHz": 1000, "gainDb": -3, "q": 1}]}}]),
            ("Mid IR", [{"id": "conv", "type": "convolver", "params": {"kernel": "mid -1.5dB"}}]),
            ("Output loudness", [{"id": "loudness", "type": "loudness", "params": {}}]),
        ):
            self.presets.write(name, {"schema": "fxroute.dsp.preset", "version": 1, "chain": chain})

    def compile(self, state, channels=8, rate=48000):
        return compile_processing_plan(state, output_key="A", channels=channels,
                                       sample_rate_hz=rate, preset_loader=self.presets.read)

    def crossover_state(self):
        assignments = [f"{side}_{way}" for side in ("left", "right") for way in ("low", "mid", "high")]
        state = switch_mode(set_mode_routing(default_output_state(), "crossover", "A", assignments), "crossover")
        for role, settings in state["modes"]["crossover"]["processing"].items():
            if not role.endswith("low"):
                settings["highpass"] = crossover_filter(300 if role.endswith("mid") else 2500)
            if not role.endswith("high"):
                settings["lowpass"] = crossover_filter(300 if role.endswith("low") else 2500)
        return state

    def test_global_precedes_independent_area_banks_and_selection_is_not_solo(self):
        state = self.crossover_state()
        mode = state["modes"]["crossover"]
        mode["banks"]["global"]["preset"] = "Global EQ"
        mode["banks"]["left_mid"]["preset"] = "Mid IR"
        mode["extras"] = {"headroom": {"enabled": True, "params": {"gainDb": -3}}}
        plan = self.compile(state)
        outputs = {row["role"]: row for row in plan["outputs"]}
        self.assertEqual(plan["global"]["chain"][0]["type"], "equalizer")
        self.assertEqual(outputs["left_mid"]["bank"]["chain"][0]["params"]["kernel"], "mid -1.5dB")
        self.assertEqual(outputs["right_mid"]["bank"]["chain"], [])
        self.assertNotIn("extras", outputs["left_mid"]["bank"])
        self.assertEqual(plan["global"]["extras"], mode["extras"])
        self.assertLess(plan["order"].index("global"), plan["order"].index("area-bank"))
        selected = select_bank(state, "crossover", "A", 8, "left_mid")
        self.assertEqual(self.compile(selected), plan)
        plan["outputs"][1]["bank"]["chain"][0]["params"]["kernel"] = "changed"
        self.assertEqual(self.presets.read("Mid IR")["chain"][0]["params"]["kernel"], "mid -1.5dB")

    def test_physical_fanout_compiles_one_logical_bank(self):
        state = set_mode_routing(default_output_state(), "stereo", "A", ["main_r", "off", "sub1", "main_l", "sub1"])
        plan = self.compile(state, channels=5)
        self.assertEqual([row["role"] for row in plan["outputs"]], ["main_l", "main_r", "sub1"])
        self.assertEqual(plan["physical_routes"], [
            {"output": 1, "channel": 0}, {"output": 2, "channel": 2},
            {"output": 0, "channel": 3}, {"output": 2, "channel": 4}])
        self.assertEqual(plan["outputs"][2]["routes"], [{"input": 0, "gain": 0.5}, {"input": 1, "gain": 0.5}])

    def test_stereo_bass_requires_the_complete_pair(self):
        for subs, expected in (
            (["sub_l", "sub_r"], [[{"input": 0, "gain": 1.0}], [{"input": 1, "gain": 1.0}]]),
            (["sub1", "sub_r"], [[{"input": 0, "gain": 0.5}, {"input": 1, "gain": 0.5}]] * 2),
            (["sub_r"], [[{"input": 0, "gain": 0.5}, {"input": 1, "gain": 0.5}]]),
        ):
            with self.subTest(subs=subs):
                state = set_mode_routing(default_output_state(), "stereo", "A", ["main_l", "main_r", *subs])
                self.assertEqual([row["routes"] for row in self.compile(state)["outputs"][2:]], expected)

    def test_subs_and_speaker_alignment_coexist_without_negative_delays(self):
        state = self.crossover_state()
        routing = state["modes"]["crossover"]["routing"]["A"] + ["sub1", "sub2"]
        state = set_mode_routing(state, "crossover", "A", routing)
        mode = state["modes"]["crossover"]
        mode["processing"]["left_mid"]["alignment_ms"] = -2
        mode["processing"]["sub1"]["alignment_ms"] = -5
        mode["processing"]["sub2"]["alignment_ms"] = 3
        mode["processing"]["sub2"]["polarity"] = "invert"
        mode["processing"]["sub2"]["level_db"] = -4
        plan = self.compile(state)
        outputs = {row["role"]: row for row in plan["outputs"]}
        self.assertEqual(outputs["left_low"]["delay_ms"], 5)
        self.assertEqual(outputs["left_mid"]["delay_ms"], 3)
        self.assertEqual(outputs["sub1"]["delay_ms"], 0)
        self.assertEqual(outputs["sub2"]["delay_ms"], 8)
        self.assertTrue(outputs["sub2"]["invert"])
        self.assertEqual(outputs["sub2"]["gain_db"], -4)
        self.assertIn({"kind": "highpass", **crossover_filter(80)}, outputs["left_low"]["crossover"])
        self.assertEqual(outputs["sub1"]["crossover"], [{"kind": "lowpass", **crossover_filter(80)}])

    def test_direct_does_not_remove_speaker_crossover(self):
        state = self.crossover_state()
        for bank in state["modes"]["crossover"]["banks"].values():
            bank["preset"] = "Direct"
        plan = self.compile(state)
        self.assertTrue(plan["global"]["bypass"])
        self.assertEqual(plan["global"]["extras"], {})
        self.assertTrue(plan["outputs"][0]["bank"]["bypass"])
        self.assertEqual(plan["outputs"][0]["crossover"], [{"kind": "lowpass", **crossover_filter(300)}])

    def test_incomplete_unprotected_and_above_nyquist_plans_are_rejected(self):
        state = self.crossover_state()
        with self.assertRaises(ValueError):
            self.compile(state, channels=4)
        missing = copy.deepcopy(state)
        missing["modes"]["crossover"]["processing"]["left_high"]["highpass"] = None
        with self.assertRaises(ValueError):
            self.compile(missing)
        with self.assertRaises(ValueError):
            self.compile(state, rate=4000)
        with self.assertRaises(ValueError):
            self.compile(state, rate=True)

    def test_bad_missing_and_global_only_area_presets_fail_before_a_plan_is_returned(self):
        for name, error in (("missing", FileNotFoundError), ("broken", ValueError), ("Output loudness", ValueError)):
            state = self.crossover_state()
            state["modes"]["crossover"]["banks"]["left_low"]["preset"] = name
            self.presets.path("broken").write_text('{"schema":"fxroute.dsp.preset","version":1,"chain":[{}]}')
            with self.subTest(name=name), self.assertRaises(error):
                self.compile(state)


if __name__ == "__main__":
    unittest.main()
