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
    bass_crossover_for_side, default_output_state, referenced_presets,
    routing_for_device, select_bank, set_bass_management, set_crossover,
    set_mode_routing, shared_bass_crossover, switch_mode, validate_output_state,
)
from audio.output_state_store import OutputStateStore, StateConflictError

STEREO_SUB = ["main_l", "main_r", "sub1", "off"]
WAYS = ["left_low", "left_high", "right_low", "right_high"]
USB = "alsa_output.usb-Test-00.multichannel-output"
PRO = "alsa_output.usb-Test-00.pro-output-0"


def crossover_state(state, device, assignments):
    state = set_crossover(state, "stereo-sub", True)
    return set_mode_routing(state, "stereo-sub", device, assignments)


class BassCrossoverTests(unittest.TestCase):
    """The sub crossover resolves per side only for an unlinked Stereo pair."""

    def setUp(self):
        state = set_mode_routing(default_output_state(), "stereo-sub", USB,
                                 ["main_l", "main_r", "sub_l", "sub_r"])
        self.bass = state["modes"]["stereo-sub"]["bass_management"]

    def test_shared_crossover_drives_both_sides_while_linked(self):
        bass = set_bass_management(default_output_state(), "stereo-sub",
                                   frequency_hz=70, family="butterworth", slope_db_oct=36)
        bass = bass["modes"]["stereo-sub"]["bass_management"]
        for side in ("left", "right"):
            self.assertEqual(bass_crossover_for_side(bass, side),
                             {"family": "butterworth", "slope_db_oct": 36, "frequency_hz": 70})
        self.assertEqual(shared_bass_crossover(bass), bass_crossover_for_side(bass, "left"))

    def test_unlinked_pair_uses_its_own_side_filters(self):
        left = {"family": "bessel", "slope_db_oct": 18, "frequency_hz": 60}
        right = {"family": "butterworth", "slope_db_oct": 12, "frequency_hz": 120}
        state = set_bass_management(default_output_state(), "stereo-sub", sub_link=False,
                                    sub_filters={"left": left, "right": right})
        bass = state["modes"]["stereo-sub"]["bass_management"]
        self.assertEqual(bass_crossover_for_side(bass, "left"), left)
        self.assertEqual(bass_crossover_for_side(bass, "right"), right)
        # A missing side override falls back to the shared values.
        self.assertEqual(bass_crossover_for_side({**bass, "sub_filters": {"left": left}}, "right"),
                         shared_bass_crossover(bass))
        # Re-linking hides the overrides without discarding them.
        linked = set_bass_management(state, "stereo-sub", sub_link=True)
        self.assertEqual(linked["modes"]["stereo-sub"]["bass_management"]["sub_filters"],
                         {"left": left, "right": right})

    def test_clearing_an_override_and_partial_shape_edits(self):
        state = set_bass_management(default_output_state(), "stereo-sub", sub_link=False,
                                    sub_filters={"left": {"family": "bessel", "slope_db_oct": 12,
                                                           "frequency_hz": 50}})
        cleared = set_bass_management(state, "stereo-sub", sub_filters={"left": None})
        self.assertEqual(cleared["modes"]["stereo-sub"]["bass_management"]["sub_filters"], {})
        for kwargs in ({"family": "bessel"}, {"slope_db_oct": 12},
                       {"sub_filters": {"center": None}}, {"sub_filters": []}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                set_bass_management(state, "stereo-sub", **kwargs)


class OutputStateTests(unittest.TestCase):
    def test_mode_roundtrip_preserves_complete_independent_settings(self):
        state = switch_mode(set_mode_routing(default_output_state(), "stereo-sub", USB, STEREO_SUB),
                            "stereo-sub")
        stereo_sub = state["modes"]["stereo-sub"]
        stereo_sub["banks"]["global"] = {"preset": "Room B", "preset_a": "Room A", "preset_b": "Room B"}
        stereo_sub["extras"] = {"headroom": {"enabled": True, "params": {"gainDb": -6}}}
        stereo_sub["processing"]["sub1"]["alignment_ms"] = -4
        state = select_bank(state, "stereo-sub", USB, 4, "sub1")
        saved_sub = copy.deepcopy(state["modes"]["stereo-sub"])
        state = crossover_state(state, USB, WAYS)
        state["modes"]["stereo-sub"]["banks"]["left_low"]["preset"] = "Low IR"
        state = select_bank(state, "stereo-sub", USB, 4, "left_low")
        state = set_crossover(state, "stereo-sub", False)
        self.assertEqual(routing_for_device(state, "stereo-sub", USB),
                         ["main_l", "off", "main_r", "off"])
        state = set_mode_routing(state, "stereo-sub", USB, STEREO_SUB)
        state["modes"]["stereo-sub"]["banks"]["global"] = saved_sub["banks"]["global"]
        state["modes"]["stereo-sub"]["extras"] = saved_sub["extras"]
        state["modes"]["stereo-sub"]["processing"]["sub1"] = saved_sub["processing"]["sub1"]
        state = select_bank(state, "stereo-sub", USB, 4, "sub1")
        self.assertEqual(state["modes"]["stereo-sub"]["banks"]["global"], saved_sub["banks"]["global"])
        # Stereo stays an independent full-band configuration without subs.
        plain = set_mode_routing(default_output_state(), "stereo", PRO, ["main_l", "main_r"])
        self.assertEqual(routing_for_device(plain, "stereo", PRO), ["main_l", "main_r"])
        self.assertEqual(state["modes"]["stereo-sub"]["selected_bank"], "sub1")

    def test_routing_edits_retain_dormant_banks_and_smaller_tier_assignments(self):
        state = switch_mode(set_mode_routing(default_output_state(), "stereo-sub", "A",
                                             STEREO_SUB + ["sub2", "off"]), "stereo-sub")
        state["modes"]["stereo-sub"]["banks"]["sub2"]["preset"] = "Dormant IR"
        state = select_bank(state, "stereo-sub", "A", 6, "sub2")
        state = set_mode_routing(state, "stereo-sub", "A", ["main_r", "main_l", "off", "off"])
        self.assertEqual(routing_for_device(state, "stereo-sub", "A"),
                         ["main_r", "main_l", "off", "off", "sub2", "off"])
        self.assertEqual(state["modes"]["stereo-sub"]["selected_bank"], "global")
        self.assertEqual(state["modes"]["stereo-sub"]["banks"]["sub2"]["preset"], "Dormant IR")
        self.assertIn("Dormant IR", referenced_presets(state))
        with self.assertRaises(ValueError):
            select_bank(state, "stereo-sub", "A", 4, "sub2")

    def test_default_stereo_is_not_an_implicit_sub_assignment(self):
        state = default_output_state()
        self.assertEqual(routing_for_device(state, "stereo", "new"), ["main_l", "main_r"])
        self.assertEqual(routing_for_device(state, "stereo-sub", "new"), ["main_l", "main_r"])

    def test_crossover_toggle_keeps_one_routing_and_dormant_banks(self):
        state = switch_mode(set_mode_routing(default_output_state(), "stereo-sub", "A", STEREO_SUB),
                            "stereo-sub")
        state = set_crossover(state, "stereo-sub", True)
        self.assertEqual(routing_for_device(state, "stereo-sub", "A"),
                         ["left_low", "right_low", "sub1", "off"])
        state = set_mode_routing(state, "stereo-sub", "A", WAYS + ["sub1", "off"])
        state["modes"]["stereo-sub"]["processing"]["left_high"]["level_db"] = -8
        state = set_crossover(state, "stereo-sub", False)
        self.assertEqual(routing_for_device(state, "stereo-sub", "A"),
                         ["main_l", "off", "main_r", "off", "sub1", "off"])
        self.assertEqual(state["modes"]["stereo-sub"]["processing"]["left_high"]["level_db"], -8)

    def test_mutations_and_validation_return_detached_values(self):
        original = default_output_state()
        changed = crossover_state(original, "A", WAYS)
        changed["modes"]["stereo-sub"]["banks"]["global"]["preset"] = "Other"
        self.assertEqual(original["modes"]["stereo-sub"]["banks"]["global"]["preset"], "Neutral")
        validated = validate_output_state(changed)
        validated["modes"]["stereo-sub"]["routing"]["A"][0] = "off"
        self.assertEqual(changed["modes"]["stereo-sub"]["routing"]["A"][0], "left_low")

    def test_filter_combinations_and_hidden_mode_data_are_validated(self):
        state = crossover_state(default_output_state(), "A", WAYS)
        for family, slopes in (("linkwitz-riley", range(12, 73, 12)),
                               ("butterworth", range(6, 73, 6)), ("bessel", range(6, 73, 6))):
            for slope in slopes:
                state["modes"]["stereo-sub"]["processing"]["left_low"]["lowpass"] = {
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
                state["modes"]["stereo-sub"]["processing"]["left_low"]["lowpass"] = definition
                validate_output_state(state)

    def test_bass_crossover_family_slope_and_side_overrides_are_validated(self):
        state = crossover_state(default_output_state(), "A", WAYS)
        bass = state["modes"]["stereo-sub"]["bass_management"]
        for family, slopes in (("linkwitz-riley", range(12, 73, 12)),
                               ("butterworth", range(6, 73, 6)), ("bessel", range(6, 73, 6))):
            for slope in slopes:
                bass["family"], bass["slope_db_oct"] = family, slope
                validate_output_state(state)
        invalid = [
            {"family": "LR24"},
            {"family": "linkwitz-riley", "slope_db_oct": 18},
            {"family": "bessel", "slope_db_oct": 78},
            {"family": "butterworth", "slope_db_oct": True},
        ]
        for patch in invalid:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                validate_output_state({**state, "modes": {
                    **state["modes"],
                    "stereo-sub": {**state["modes"]["stereo-sub"],
                                   "bass_management": {**bass, **patch}}}})
        for patch in ({"sub_link": 1}, {"sub_filters": {"center": {
                "family": "bessel", "slope_db_oct": 12, "frequency_hz": 80}}},
                {"sub_filters": {"left": {"family": "bessel", "slope_db_oct": 12,
                                          "frequency_hz": 20}}},
                {"frequency_hz": 20}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                validate_output_state({**state, "modes": {
                    **state["modes"],
                    "stereo-sub": {**state["modes"]["stereo-sub"],
                                   "bass_management": {**bass, **patch}}}})

    def test_malformed_state_never_silently_discards_settings(self):
        base = crossover_state(default_output_state(), "A", WAYS)
        for path, value in (
            (("version",), 4), (("revision",), True), (("active_mode",), "surround"),
            (("modes", "stereo", "selected_bank"), "missing"),
            (("modes", "stereo", "banks", "global", "typo"), 1),
            (("modes", "stereo-sub", "processing", "left_low", "level_db"), float("inf")),
            (("modes", "stereo-sub", "processing", "left_low", "alignment_ms"), 41),
            (("modes", "stereo-sub", "processing", "left_low", "polarity"), "bad"),
            (("modes", "stereo", "extras", "bad"), float("nan")),
        ):
            state = copy.deepcopy(base)
            owner = state
            for key in path[:-1]:
                owner = owner[key]
            owner[path[-1]] = value
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_output_state(state)
        del base["modes"]["stereo-sub"]["banks"]["left_high"]
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
        candidate = set_mode_routing(initial, "stereo-sub", "A", STEREO_SUB)
        result = self.store.commit(candidate, expected_revision=0)
        self.assertEqual(result["revision"], 1)
        self.assertEqual(OutputStateStore(self.path).load(), result)
        committed = self.path.read_bytes()
        with self.assertRaises(StateConflictError):
            OutputStateStore(self.path).commit(stale, expected_revision=0)
        self.assertEqual(self.path.read_bytes(), committed)
        result = switch_mode(result, "stereo-sub")
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
        committed = switch_mode(committed, "stereo-sub")
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
