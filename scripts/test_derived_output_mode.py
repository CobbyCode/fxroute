#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Derived overview mode payload: v2 head translates to the legacy shape.

The v2 output state is the single source of truth; the overview builder
derives its legacy ``output_mode`` payload (mode label, subwoofer blocks,
crossover fields) from the committed head instead of the removed mode
files. Unusable heads (None, garbage, failing loader) fall back to the
file path so behavior degrades exactly like a missing mode file.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from audio.output_state import default_output_state, set_mode_routing, switch_mode
from audio.samplerate.overview import (
    _derived_output_mode,
    _derived_output_mode_from_head,
    configure_output_state_head,
)


def head_with(mode, key, roles, *, frequency_hz=80, highpass=True, subs=None):
    state = switch_mode(set_mode_routing(
        default_output_state(), mode, key, ["main_l", "main_r", *roles]), mode)
    spec = state["modes"][mode]
    spec["bass_management"] = {"frequency_hz": frequency_hz,
                               "main_highpass_enabled": highpass}
    for role, settings in (subs or {}).items():
        spec["processing"][role].update(settings)
    return state


class DerivedModeTests(unittest.TestCase):
    def test_stereo_head_reads_stereo(self):
        payload = _derived_output_mode(head_with("stereo", "dev", []), "dev")
        self.assertEqual(payload["mode"], "stereo")
        self.assertEqual(payload["subwoofer"]["crossover_frequency_hz"], 80)

    def test_single_sub_reads_21_with_settings(self):
        head = head_with("stereo-sub", "dev", ["sub1"], frequency_hz=150,
                         highpass=False,
                         subs={"sub1": {"level_db": -3.0, "alignment_ms": 1.2,
                                        "polarity": "invert"}})
        payload = _derived_output_mode(head, "dev")
        self.assertEqual(payload["mode"], "subwoofer-2.1")
        sub = payload["subwoofer"]
        self.assertEqual(sub["crossover_frequency_hz"], 150)
        self.assertIs(sub["main_highpass_enabled"], False)
        self.assertEqual(sub["sub_level_db"], -3.0)
        self.assertEqual(sub["sub_alignment_ms"], 1.2)
        self.assertEqual(sub["sub_polarity"], "invert")

    def test_dual_sub_reads_22_and_keeps_minus_80_park(self):
        head = head_with("stereo-sub", "dev", ["sub1", "sub2"],
                         subs={"sub1": {"level_db": 1.5, "alignment_ms": 1.0,
                                        "polarity": "invert"},
                               "sub2": {"level_db": -80.0, "alignment_ms": 3.0,
                                        "polarity": "normal"}})
        payload = _derived_output_mode(head, "dev")
        self.assertEqual(payload["mode"], "subwoofer-2.2")
        self.assertEqual(payload["crossover_frequency_hz"], 80)
        self.assertEqual(payload["slope"], "LR24")
        self.assertEqual(payload["subwoofers"]["sub1"]["level_db"], 1.5)
        self.assertEqual(payload["subwoofers"]["sub1"]["polarity"], "invert")
        self.assertEqual(payload["subwoofers"]["sub2"]["level_db"], -80.0)
        self.assertEqual(payload["subwoofers"]["sub2"]["alignment_ms"], 3.0)

    def test_stereo_subs_read_22_stereo_label(self):
        head = head_with("stereo-sub", "dev", ["sub_l", "sub_r"])
        payload = _derived_output_mode(head, "dev")
        self.assertEqual(payload["mode"], "subwoofer-2.2-stereo")

    def test_unusable_heads_fall_back(self):
        self.assertIsNone(_derived_output_mode(None, "dev"))
        self.assertIsNone(_derived_output_mode({"no": "modes"}, "dev"))
        self.assertIsNone(_derived_output_mode({"active_mode": "stereo-sub",
                                                "modes": {}}, "dev"))
        self.assertIsNone(_derived_output_mode(
            head_with("stereo-sub", "dev", ["sub1"]), "other-key"))

    def test_parity_with_legacy_file_content(self):
        """Same settings via file and via v2 head must agree on key fields."""
        legacy = {
            "mode": "subwoofer-2.1",
            "subwoofer": {"crossover_frequency_hz": 150,
                          "main_highpass_enabled": False,
                          "sub_level_db": -3.0, "sub_alignment_ms": 1.2,
                          "sub_polarity": "invert"},
        }
        head = head_with("stereo-sub", "dev", ["sub1"], frequency_hz=150,
                         highpass=False,
                         subs={"sub1": {"level_db": -3.0, "alignment_ms": 1.2,
                                        "polarity": "invert"}})
        derived = _derived_output_mode(head, "dev")["subwoofer"]
        for key, value in legacy["subwoofer"].items():
            self.assertEqual(derived[key], value, key)


class LoaderFallbackTests(unittest.TestCase):
    def tearDown(self):
        configure_output_state_head(None)

    def test_failing_loader_falls_back(self):
        configure_output_state_head(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        self.assertIsNone(
            _derived_output_mode_from_head({"selected_key": "dev"}))

    def test_missing_loader_falls_back(self):
        configure_output_state_head(None)
        self.assertIsNone(
            _derived_output_mode_from_head({"selected_key": "dev"}))

    def test_wired_loader_feeds_builder_payload(self):
        head = head_with("stereo-sub", "dev", ["sub1"])
        configure_output_state_head(lambda: head)
        payload = _derived_output_mode_from_head({"selected_key": "dev"})
        self.assertEqual(payload["mode"], "subwoofer-2.1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
