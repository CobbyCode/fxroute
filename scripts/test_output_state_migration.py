#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Upgrade of released output-state documents (version three) to the current schema."""

import copy
import json
import tempfile
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.output_state import (default_output_state, set_crossover, set_mode_routing,
                                validate_output_state, VERSION)
from audio.output_state_migration import upgrade_output_state
from audio.output_state_store import OutputStateStore


def released(version, state=None):
    """A (default) current document as the given schema version stored it."""
    state = copy.deepcopy(state or default_output_state())
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
    return state


class ReleasedUpgradeTests(unittest.TestCase):
    def test_v3_through_v6_upgrade_neutral_slots_and_preserve_existing_calibration(self):
        for version in range(3, VERSION + 1):
            with self.subTest(version=version):
                state = released(version)
                before = copy.deepcopy(state)
                result = upgrade_output_state(state)
                self.assertEqual(state, before)
                self.assertEqual(result["version"], VERSION)
                for mode in before["modes"]:
                    for role in before["modes"][mode]["processing"]:
                        self.assertEqual(result["modes"][mode]["processing"][role]["level_cal_db"],
                                         {"global": 0.0, "bank": 0.0} if version < 5 else
                                         {"global": -2.0, "bank": -1.0})
                self.assertEqual(upgrade_output_state(result), result)

    def test_store_load_upgrades_a_released_document_without_writing(self):
        state = set_mode_routing(default_output_state(), "stereo-sub", "A", ["main_l", "main_r", "sub1"])
        state["modes"]["stereo-sub"]["banks"]["global"] = {
            "preset": "Room B", "preset_a": "Room A", "preset_b": "Room B"}
        state["revision"] = 12
        state = released(3, state)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output-state.json"
            path.write_text(json.dumps(state))
            before = path.read_bytes()
            store = OutputStateStore(path)
            result = store.load()
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(result["revision"], 12)
            self.assertEqual(result["input_modes"], {})
            self.assertEqual(result["modes"]["surround"], default_output_state()["modes"]["surround"])
            self.assertEqual(result["modes"]["stereo-sub"]["routing"], state["modes"]["stereo-sub"]["routing"])
            self.assertEqual(result["modes"]["stereo-sub"]["banks"]["global"]["preset_a"], "Room A")
            committed = store.commit(result, expected_revision=12)
            self.assertEqual(store.load(), committed)

    def test_unreleased_and_malformed_documents_fail_closed(self):
        for version in (1, 2, VERSION + 1):
            with self.subTest(version=version):
                state = default_output_state()
                state["version"] = version
                with self.assertRaises(ValueError):
                    upgrade_output_state(state)
        state = released(3)
        with self.assertRaises(ValueError):
            upgrade_output_state({**state, "modes": {"stereo": state["modes"]["stereo"]}})
        state = released(3)
        state["modes"]["stereo"]["bass_management"]["typo"] = 1
        with self.assertRaises(ValueError):
            upgrade_output_state(state)

    def test_crossover_state_survives_the_upgrade(self):
        state = released(3, set_crossover(default_output_state(), "stereo-sub", True))
        result = upgrade_output_state(state)
        self.assertTrue(result["modes"]["stereo-sub"]["crossover_enabled"])

    def test_current_version_roundtrips_unchanged(self):
        state = default_output_state()
        self.assertEqual(upgrade_output_state(state), validate_output_state(state))


if __name__ == "__main__":
    unittest.main()
