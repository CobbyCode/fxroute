#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Mode isolation, durable revisions, dormant banks, and strict DSP state."""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.output_state import (
    default_output_state, referenced_presets, routing_for_device, select_bank,
    set_mode_routing, switch_mode, validate_output_state,
)
from audio.output_state_store import OutputStateStore, StateConflictError

STEREO = ["main_l", "main_r", "sub1", "off"]
CROSSOVER = ["left_low", "left_high", "right_low", "right_high"]
USB = "alsa_output.usb-Test-00.multichannel-output"
PRO = "alsa_output.usb-Test-00.pro-output-0"


class OutputStateTests(unittest.TestCase):
    def test_mode_roundtrip_preserves_complete_independent_settings(self):
        state = set_mode_routing(default_output_state(), "stereo", USB, STEREO)
        stereo = state["modes"]["stereo"]
        stereo["banks"]["global"] = {"preset": "Room B", "preset_a": "Room A", "preset_b": "Room B"}
        stereo["extras"] = {"headroom": {"enabled": True, "params": {"gainDb": -6}}}
        stereo["processing"]["sub1"]["alignment_ms"] = -4
        state = select_bank(state, "stereo", USB, 4, "sub1")
        saved_stereo = copy.deepcopy(state["modes"]["stereo"])
        state = set_mode_routing(state, "crossover", USB, CROSSOVER)
        state = switch_mode(state, "crossover")
        state["modes"]["crossover"]["banks"]["left_low"]["preset"] = "Low IR"
        state = select_bank(state, "crossover", USB, 4, "left_low")
        state = switch_mode(state, "stereo")
        self.assertEqual(state["modes"]["stereo"], saved_stereo)
        self.assertEqual(routing_for_device(state, "stereo", PRO), STEREO)
        self.assertEqual(routing_for_device(state, "crossover", PRO), CROSSOVER)
        self.assertEqual(state["modes"]["crossover"]["selected_bank"], "left_low")
        self.assertEqual(state["modes"]["crossover"]["banks"]["global"]["preset"], "Neutral")

    def test_routing_edits_retain_dormant_banks_and_smaller_tier_assignments(self):
        state = set_mode_routing(default_output_state(), "stereo", "A", STEREO + ["sub2", "off"])
        state["modes"]["stereo"]["banks"]["sub2"]["preset"] = "Dormant IR"
        state = select_bank(state, "stereo", "A", 6, "sub2")
        state = set_mode_routing(state, "stereo", "A", ["main_r", "main_l", "off", "off"])
        self.assertEqual(routing_for_device(state, "stereo", "A"), ["main_r", "main_l", "off", "off", "sub2", "off"])
        self.assertEqual(state["modes"]["stereo"]["selected_bank"], "global")
        self.assertEqual(state["modes"]["stereo"]["banks"]["sub2"]["preset"], "Dormant IR")
        self.assertIn("Dormant IR", referenced_presets(state))
        with self.assertRaises(ValueError):
            select_bank(state, "stereo", "A", 4, "sub2")

    def test_default_stereo_is_not_an_implicit_sub_assignment(self):
        state = default_output_state()
        self.assertEqual(routing_for_device(state, "stereo", "new"), ["main_l", "main_r"])
        self.assertEqual(routing_for_device(state, "crossover", "new"), [])

    def test_mutations_and_validation_return_detached_values(self):
        original = default_output_state()
        changed = set_mode_routing(original, "crossover", "A", CROSSOVER)
        changed["modes"]["crossover"]["banks"]["global"]["preset"] = "Other"
        self.assertEqual(original["modes"]["crossover"]["banks"]["global"]["preset"], "Neutral")
        validated = validate_output_state(changed)
        validated["modes"]["crossover"]["routing"]["A"][0] = "off"
        self.assertEqual(changed["modes"]["crossover"]["routing"]["A"][0], "left_low")

    def test_filter_combinations_and_hidden_mode_data_are_validated(self):
        state = set_mode_routing(default_output_state(), "crossover", "A", CROSSOVER)
        for family, slopes in (("linkwitz-riley", range(12, 73, 12)),
                               ("butterworth", range(6, 73, 6)), ("bessel", range(6, 73, 6))):
            for slope in slopes:
                state["modes"]["crossover"]["processing"]["left_low"]["lowpass"] = {
                    "family": family, "slope_db_oct": slope, "frequency_hz": 2000}
                validate_output_state(state)
        invalid_filters = [
            {"family": "LR24", "slope_db_oct": 24, "frequency_hz": 2000},
            {"family": "linkwitz-riley", "slope_db_oct": 18, "frequency_hz": 2000},
            {"family": "bessel", "slope_db_oct": 78, "frequency_hz": 2000},
            {"family": "butterworth", "slope_db_oct": True, "frequency_hz": 2000},
            {"family": "bessel", "slope_db_oct": 6, "frequency_hz": float("nan")},
        ]
        for definition in invalid_filters:
            with self.subTest(definition=definition), self.assertRaises(ValueError):
                state["modes"]["crossover"]["processing"]["left_low"]["lowpass"] = definition
                validate_output_state(state)

    def test_malformed_state_never_silently_discards_settings(self):
        base = set_mode_routing(default_output_state(), "crossover", "A", CROSSOVER)
        for path, value in (
            (("version",), 2), (("revision",), True), (("active_mode",), "surround"),
            (("modes", "stereo", "selected_bank"), "missing"),
            (("modes", "stereo", "banks", "global", "typo"), 1),
            (("modes", "crossover", "processing", "left_low", "level_db"), float("inf")),
            (("modes", "crossover", "processing", "left_low", "alignment_ms"), 41),
            (("modes", "crossover", "processing", "left_low", "polarity"), "bad"),
            (("modes", "stereo", "extras", "bad"), float("nan")),
        ):
            state = copy.deepcopy(base)
            owner = state
            for key in path[:-1]:
                owner = owner[key]
            owner[path[-1]] = value
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_output_state(state)
        del base["modes"]["crossover"]["banks"]["left_high"]
        with self.assertRaises(ValueError):
            validate_output_state(base)


class OutputStateStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "output-state.json"
        self.store = OutputStateStore(self.path)

    def test_restart_and_revision_conflicts_preserve_committed_bytes(self):
        initial = self.store.load()
        self.assertFalse(self.path.exists())
        stale = OutputStateStore(self.path).load()
        candidate = set_mode_routing(initial, "stereo", "A", STEREO)
        result = self.store.commit(candidate, expected_revision=0)
        self.assertEqual(result["revision"], 1)
        self.assertEqual(OutputStateStore(self.path).load(), result)
        committed = self.path.read_bytes()
        with self.assertRaises(StateConflictError):
            OutputStateStore(self.path).commit(stale, expected_revision=0)
        self.assertEqual(self.path.read_bytes(), committed)
        result["active_mode"] = "crossover"
        self.assertEqual(self.store.commit(result, expected_revision=1)["revision"], 2)

    def test_corrupt_or_future_documents_are_not_overwritten(self):
        for content in ("{broken", '[]', '{"schema":"future","version":2}'):
            self.path.write_text(content)
            with self.subTest(content=content), self.assertRaises(ValueError):
                self.store.load()
            with self.assertRaises(ValueError):
                self.store.commit(default_output_state(), expected_revision=0)
            self.assertEqual(self.path.read_text(), content)

    def test_failed_atomic_replace_keeps_previous_document(self):
        committed = self.store.commit(default_output_state(), expected_revision=0)
        before = self.path.read_bytes()
        committed["active_mode"] = "crossover"
        with patch("common.atomic_write.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                self.store.commit(committed, expected_revision=1)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_candidate_cannot_forge_its_base_revision(self):
        candidate = self.store.commit(default_output_state(), expected_revision=0)
        candidate["revision"] = 0
        before = self.path.read_bytes()
        with self.assertRaises(StateConflictError):
            self.store.commit(candidate, expected_revision=1)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
