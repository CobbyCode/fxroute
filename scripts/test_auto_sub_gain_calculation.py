#!/usr/bin/env python3
import copy
import math
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.modules.setdefault("multipart", types.SimpleNamespace(__version__="0", multipart=types.SimpleNamespace(parse_options_header=lambda value: (value, {}))))

import main


def curve(offset=0.0, dip=False):
    points = []
    for index in range(80):
        frequency = 20.0 * (10.0 ** (index / 79.0 * 2.0))
        value = float(offset)
        if dip and 77.0 <= frequency <= 83.0:
            value -= 30.0
        points.append([frequency, value])
    return points


class GainCalculationTests(unittest.TestCase):
    def setUp(self):
        self.target = {"points": curve(0.0), "key": "flat", "label": "Flat"}
        self.anchor = {"status": "ready", "target_vertical_offset_db": -20.0}

    def calculate(self, mode=main.OUTPUT_MODE_SUBWOOFER_21, left=-20.0, right=-20.0, **kwargs):
        curves = kwargs.pop("winner_curves", {"left": curve(left), "right": curve(right)})
        return main._calculate_auto_sub_gain(
            mode=mode, target_curve=kwargs.pop("target", self.target),
            anchor=kwargs.pop("anchor", self.anchor), winner_curves=curves, crossover_hz=80,
        )

    def test_zero_positive_and_negative_sign(self):
        self.assertEqual(self.calculate()["recommendation"]["delta_db"], 0.0)
        self.assertEqual(self.calculate(left=-23, right=-23)["recommendation"]["delta_db"], 3.0)
        self.assertEqual(self.calculate(left=-17, right=-17)["recommendation"]["delta_db"], -3.0)

    def test_narrow_deep_dip_does_not_dominate(self):
        result = self.calculate(winner_curves={"left": curve(-20, True), "right": curve(-20, True)})
        self.assertAlmostEqual(result["recommendation"]["delta_db"], 0.0, places=2)

    def test_common_modes_and_stereo_channel_rule(self):
        mono = self.calculate(mode=main.OUTPUT_MODE_SUBWOOFER_22, left=-22, right=-18)
        self.assertEqual(mono["recommendation"]["type"], "common")
        self.assertEqual(mono["recommendation"]["delta_db"], 0.0)
        self.assertTrue(mono["recommendation"]["preserves_relative_sub_gain"])
        stereo = self.calculate(mode=main.OUTPUT_MODE_SUBWOOFER_22_STEREO, left=-22, right=-18)
        self.assertEqual(stereo["recommendation"]["left_delta_db"], 2.0)
        self.assertEqual(stereo["recommendation"]["right_delta_db"], -2.0)

    def test_safety_bounds_preserve_raw_recommendation(self):
        result = self.calculate(left=-32, right=-32)
        self.assertEqual(result["recommendation"]["raw_delta_db"], 12.0)
        self.assertEqual(result["recommendation"]["delta_db"], 6.0)
        self.assertTrue(result["recommendation"]["clamped"])

    def test_invalid_support_is_explicitly_unavailable(self):
        result = self.calculate(anchor={"status": "unavailable"})
        self.assertFalse(result["gain_calculated"])
        self.assertIn("anchor", result["reason"].lower())
        result = self.calculate(winner_curves={"left": [[80, -20]], "right": [[80, -20]]})
        self.assertFalse(result["available"])

    def test_display_offset_and_inputs_do_not_affect_or_mutate_result(self):
        target, anchor = copy.deepcopy(self.target), copy.deepcopy(self.anchor)
        before = self.calculate(target=target, anchor=anchor)
        main._auto_sub_shared_bass_offset({"points": [[20, -200], [80, 200]]})
        after = self.calculate(target=target, anchor=anchor)
        self.assertEqual(before, after)
        self.assertEqual(target, self.target)
        self.assertEqual(anchor, self.anchor)

    def test_diagnostics_include_band_coverage_confidence_and_no_application(self):
        result = self.calculate()
        self.assertTrue(result["gain_calculated"])
        self.assertFalse(result["applied"])
        self.assertEqual(result["smoothing_octaves"], 1.0)
        for channel in ("left", "right"):
            diagnostics = result["channels"][channel]
            self.assertGreaterEqual(diagnostics["point_count"], 8)
            self.assertIn("frequency_range_hz", diagnostics)
            self.assertIn("target_delta_db", diagnostics)
            self.assertIn(diagnostics["confidence"], ("low", "medium", "high"))
            self.assertTrue(diagnostics["reason"])


if __name__ == "__main__":
    unittest.main()
