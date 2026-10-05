#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Level Calibration slots: schema, cut-only validation, v4 upgrade and compiled gain."""

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.output_state import (VERSION, default_output_state, set_mode_routing, set_output_processing,
                                switch_mode, validate_output_state)
from audio.output_state_migration import upgrade_output_state
from dsp.processing_plan import compile_processing_plan


def stereo_state():
    return switch_mode(set_mode_routing(default_output_state(), "stereo", "A", ["main_l", "main_r"]), "stereo")


def v4_document(state):
    """The same state in the pre-slot v4 shape."""
    document = copy.deepcopy(state)
    document["version"] = 4
    for config in document["modes"].values():
        for settings in config["processing"].values():
            del settings["level_cal_db"]
    return document


class LevelCalibrationSchemaTests(unittest.TestCase):
    def test_default_processing_has_neutral_slots(self):
        for config in default_output_state()["modes"].values():
            for settings in config["processing"].values():
                self.assertEqual(settings["level_cal_db"], {"global": 0.0, "bank": 0.0})

    def test_slots_are_cut_only(self):
        state = stereo_state()
        state["modes"]["stereo"]["processing"]["main_l"]["level_cal_db"]["bank"] = 0.1
        with self.assertRaises(ValueError):
            validate_output_state(state)

    def test_slots_reject_unknown_keys_and_out_of_range(self):
        for bad in ({"global": 0.0}, {"global": 0.0, "bank": 0.0, "extra": 0.0}, {"global": -81.0, "bank": 0.0},
                    {"global": float("nan"), "bank": 0.0}):
            state = stereo_state()
            state["modes"]["stereo"]["processing"]["main_l"]["level_cal_db"] = bad
            with self.assertRaises(ValueError, msg=str(bad)):
                validate_output_state(state)

    def test_set_output_processing_merges_slots(self):
        state = set_output_processing(stereo_state(), "stereo", "main_l", level_cal_db={"global": -2.5})
        state = set_output_processing(state, "stereo", "main_l", level_cal_db={"bank": -1.0})
        self.assertEqual(state["modes"]["stereo"]["processing"]["main_l"]["level_cal_db"],
                         {"global": -2.5, "bank": -1.0})
        self.assertEqual(state["modes"]["stereo"]["processing"]["main_r"]["level_cal_db"],
                         {"global": 0.0, "bank": 0.0})
        self.assertEqual(state["modes"]["stereo"]["processing"]["main_l"]["level_db"], 0.0)

    def test_set_output_processing_rejects_unknown_slot_and_positive_gain(self):
        state = stereo_state()
        with self.assertRaises(ValueError):
            set_output_processing(state, "stereo", "main_l", level_cal_db={"front": -1.0})
        with self.assertRaises(ValueError):
            set_output_processing(state, "stereo", "main_l", level_cal_db={"global": 1.0})


class LevelCalibrationUpgradeTests(unittest.TestCase):
    def test_v4_document_gains_neutral_slots(self):
        state = set_output_processing(stereo_state(), "stereo", "main_l", level_db=-3.0)
        upgraded = upgrade_output_state(v4_document(state))
        self.assertEqual(upgraded["version"], VERSION)
        self.assertEqual(upgraded, validate_output_state(state))

    def test_existing_slots_are_kept_by_the_upgrade(self):
        state = set_output_processing(stereo_state(), "stereo", "main_r", level_cal_db={"global": -4.0})
        document = v4_document(state)
        # A mode that already carries slots (the Surround block a v3 upgrade creates) keeps them.
        document["modes"]["stereo"]["processing"]["main_r"]["level_cal_db"] = {"global": -4.0, "bank": 0.0}
        self.assertEqual(upgrade_output_state(document), state)

    def test_invalid_existing_slots_still_fail_closed(self):
        document = v4_document(stereo_state())
        document["modes"]["stereo"]["processing"]["main_l"]["level_cal_db"] = {"global": 3.0, "bank": 0.0}
        with self.assertRaises(ValueError):
            upgrade_output_state(document)


class LevelCalibrationPlanTests(unittest.TestCase):
    def outputs(self, state):
        plan = compile_processing_plan(state, output_key="A", channels=2, sample_rate_hz=48000,
                                       preset_loader=lambda name: {"chain": []})
        return {output["role"]: output for output in plan["outputs"]}

    def test_slots_add_to_the_manual_level(self):
        state = set_output_processing(stereo_state(), "stereo", "main_l", level_db=-2.0,
                                      level_cal_db={"global": -1.5, "bank": -0.5})
        outputs = self.outputs(state)
        self.assertAlmostEqual(outputs["main_l"]["gain_db"], -4.0)
        self.assertAlmostEqual(outputs["main_r"]["gain_db"], 0.0)

    def test_neutral_slots_leave_the_gain_unchanged(self):
        state = set_output_processing(stereo_state(), "stereo", "main_r", level_db=-6.0)
        self.assertAlmostEqual(self.outputs(state)["main_r"]["gain_db"], -6.0)


if __name__ == "__main__":
    unittest.main()
