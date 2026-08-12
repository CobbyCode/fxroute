#!/usr/bin/env python3
"""Behavior tests for the REFACTOR-005 extraction:

- sink_inputs.brief_sink_inputs
- sink_inputs.active_unmuted_sink_inputs

plus wrapper parity against main._brief_sink_inputs and
main._active_unmuted_sink_inputs.
"""
import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main
from sink_inputs import active_unmuted_sink_inputs, brief_sink_inputs


class BriefSinkInputsTests(unittest.TestCase):
    def test_fields_and_order(self):
        entry = {
            "id": 42,
            "sink": "sink-1",
            "sample_rate": 44100,
            "volume_percent": 77,
            "properties": {
                "node.name": "node-x",
                "application.name": "AppName",
                "application.id": "app.id",
                "media.name": "MediaName",
            },
        }
        result = brief_sink_inputs([entry])[0]
        self.assertEqual(
            list(result.keys()),
            ["id", "sink", "node", "app", "media", "rate", "corked", "muted", "volume_percent"],
        )
        self.assertEqual(result["id"], 42)
        self.assertEqual(result["sink"], "sink-1")
        self.assertEqual(result["node"], "node-x")
        self.assertEqual(result["app"], "AppName")  # application.name schlägt application.id
        self.assertEqual(result["media"], "MediaName")
        self.assertEqual(result["rate"], 44100)
        self.assertEqual(result["corked"], None)
        self.assertEqual(result["muted"], None)
        self.assertEqual(result["volume_percent"], 77)

    def test_app_name_priority_falls_back_to_id(self):
        result = brief_sink_inputs([{"properties": {"application.id": "only.id"}}])[0]
        self.assertEqual(result["app"], "only.id")

    def test_missing_properties_and_fields(self):
        result = brief_sink_inputs([{}])[0]
        self.assertEqual(
            result,
            {
                "id": None,
                "sink": None,
                "node": None,
                "app": None,
                "media": None,
                "rate": None,
                "corked": None,
                "muted": None,
                "volume_percent": None,
            },
        )

    def test_entry_order_preserved(self):
        entries = [{"id": 3}, {"id": 1}, {"id": 2}]
        self.assertEqual([e["id"] for e in brief_sink_inputs(entries)], [3, 1, 2])

    def test_input_entries_not_mutated(self):
        entries = [{"id": 1, "properties": {"node.name": "n"}}]
        before = copy.deepcopy(entries)
        brief_sink_inputs(entries)
        self.assertEqual(entries, before)


class ActiveUnmutedSinkInputsTests(unittest.TestCase):
    def test_corked_and_muted_excluded(self):
        entries = [
            {"id": 1, "volume_percent": 100},
            {"id": 2, "corked": True, "volume_percent": 100},
            {"id": 3, "muted": True, "volume_percent": 100},
            {"id": 4, "corked": 1, "muted": 1, "volume_percent": 100},
        ]
        self.assertEqual([e["id"] for e in active_unmuted_sink_inputs(entries)], [1])

    def test_missing_and_none_use_fallback_100_numeric_zero_is_muted(self):
        # missing / None -> fallback 100 -> active; numeric 0 -> muted
        for entry in ({}, {"volume_percent": None}):
            self.assertEqual(active_unmuted_sink_inputs([entry]), [entry])
        self.assertEqual(active_unmuted_sink_inputs([{"volume_percent": 0}]), [])

    def test_string_zero_and_negative_inactive(self):
        for entry in ({"volume_percent": "0"}, {"volume_percent": -5}, {"volume_percent": "-5"}):
            self.assertEqual(active_unmuted_sink_inputs([entry]), [])

    def test_invalid_truthy_volume_raises(self):
        # "abc" is truthy -> int("abc") raises ValueError (previous behavior)
        with self.assertRaises(ValueError):
            active_unmuted_sink_inputs([{"volume_percent": "abc"}])

    def test_input_entries_not_mutated(self):
        entries = [{"id": 1, "volume_percent": 100}, {"id": 2, "muted": True}]
        before = copy.deepcopy(entries)
        active_unmuted_sink_inputs(entries)
        self.assertEqual(entries, before)


class WrapperParityTests(unittest.TestCase):
    def test_brief_wrapper_matches_module_function(self):
        entries = [
            {"id": 42, "sink": "s1", "sample_rate": 48000, "volume_percent": 77,
             "properties": {"node.name": "n", "application.name": "A", "application.id": "a", "media.name": "m"}},
            {"id": 43},
            {"properties": {"application.id": "only"}},
            {},
        ]
        self.assertEqual(main._brief_sink_inputs(entries), brief_sink_inputs(entries))

    def test_active_wrapper_matches_module_function(self):
        entries = [
            {"id": 1, "volume_percent": 100},
            {"id": 2, "corked": True, "volume_percent": 100},
            {"id": 3, "muted": True, "volume_percent": 100},
            {"id": 4},
            {"id": 5, "volume_percent": 0},
            {"id": 6, "volume_percent": "0"},
            {"id": 7, "volume_percent": -1},
        ]
        self.assertEqual(main._active_unmuted_sink_inputs(entries), active_unmuted_sink_inputs(entries))

    def test_active_wrapper_raises_like_module(self):
        with self.assertRaises(ValueError):
            main._active_unmuted_sink_inputs([{"volume_percent": "abc"}])


if __name__ == "__main__":
    unittest.main()
