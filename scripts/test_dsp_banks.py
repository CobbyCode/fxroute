#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Bank-local A/B selection must not change another bank or the source value."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dsp.banks import BankState


class BankStateTests(unittest.TestCase):
    def test_switching_ab_is_local_and_keeps_both_choices(self):
        bank = BankState("Room", "Room", "Room IR -1.5dB")
        other = BankState()
        selected = bank.select("B")
        self.assertEqual(selected.preset, "Room IR -1.5dB")
        self.assertEqual(selected.active_side, "B")
        self.assertEqual(selected.select("A"), bank)
        self.assertEqual(bank.active_side, "A")
        self.assertEqual(other.preset, "Neutral")

    def test_active_preset_can_be_outside_compare_slots(self):
        bank = BankState.from_dict({"preset": "Other", "preset_a": "A", "preset_b": "B"})
        self.assertIsNone(bank.active_side)
        self.assertEqual(BankState.from_dict(bank.to_dict()), bank)

    def test_unassigned_side_and_malformed_names_fail(self):
        with self.assertRaises(ValueError):
            BankState().select("B")
        with self.assertRaises(ValueError):
            BankState().select("C")
        for value in ("", "../Room", "a/b", "a\\b", None, True, "Room\ncontrol"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                BankState(value)
        with self.assertRaises(ValueError):
            BankState("A", "A", "A")
        with self.assertRaises(ValueError):
            BankState.from_dict({"preset": "A", "preset_a": "A", "preset_b": None, "typo": 1})


if __name__ == "__main__":
    unittest.main()
