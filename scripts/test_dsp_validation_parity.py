#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
#
# Manager/engine validation parity: values the native engine rejects must
# fail in normalize_effects_extras at the API boundary, never during engine
# startup, and the tone_effect "off" normalization must match the old
# EasyEffects-era contract.

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dsp_manager import DSPManager


class DSPValidationParityTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="fxroute-validation-"))
        self.manager = DSPManager(home=self.home)

    def extras(self, **overrides):
        payload = self.manager.normalize_effects_extras(None)
        for section, value in overrides.items():
            payload[section] = value
        return payload

    def normalize(self, section, value):
        return self.manager.normalize_effects_extras({section: value})[section]

    def test_autogain_history_below_engine_minimum_is_rejected(self):
        for history in (1, 5, 5.5, 3601, 10000):
            with self.assertRaises(ValueError, msg=f"history={history}"):
                self.normalize("autogain", {
                    "enabled": True, "params": {"maximumHistorySeconds": history}})
        for history in (6, 15, 3600):
            normalized = self.normalize("autogain", {
                "enabled": True, "params": {"maximumHistorySeconds": history}})
            self.assertEqual(normalized["params"]["maximumHistorySeconds"], history)

    def test_autogain_silence_threshold_outside_engine_range_is_rejected(self):
        for value in (-101.0, 0.5, 10.0):
            with self.assertRaises(ValueError, msg=f"silence={value}"):
                self.normalize("autogain", {
                    "enabled": True, "params": {"silenceThresholdDb": value}})
        for value in (-100.0, -70.0, 0.0):
            normalized = self.normalize("autogain", {
                "enabled": True, "params": {"silenceThresholdDb": value}})
            self.assertEqual(normalized["params"]["silenceThresholdDb"], value)

    def test_autogain_reference_is_validated_against_engine_names(self):
        for reference in ("Momentary", "Shortterm", "Integrated",
                          "Geometric Mean (MSI)", "Geometric Mean (MS)",
                          "Geometric Mean (MI)", "Geometric Mean (SI)"):
            normalized = self.normalize("autogain", {
                "enabled": True, "params": {"reference": reference}})
            self.assertEqual(normalized["params"]["reference"], reference)
        with self.assertRaises(ValueError):
            self.normalize("autogain", {
                "enabled": True, "params": {"reference": "Short-term"}})

    def test_tone_effect_off_maps_to_disabled_crystalizer(self):
        normalized = self.normalize("tone_effect", {"enabled": True, "mode": "off"})
        self.assertEqual(normalized, {"enabled": False, "mode": "crystalizer"})
        normalized = self.normalize("tone_effect", {"mode": "off"})
        self.assertEqual(normalized, {"enabled": False, "mode": "crystalizer"})
        normalized = self.normalize("tone_effect", "maximizer")
        self.assertEqual(normalized, {"enabled": True, "mode": "maximizer"})
        with self.assertRaises(ValueError):
            self.normalize("tone_effect", {"mode": "bass_boost"})

    def test_tone_effect_off_never_enters_the_engine_chain(self):
        extras_payload = self.extras(tone_effect={"enabled": True, "mode": "off"})
        chain = self.manager._extras_chain(extras_payload)
        self.assertTrue(all(item["type"] not in {"crystalizer", "maximizer"} for item in chain))

    def test_limiter_contract_ranges_are_restored(self):
        with self.assertRaises(ValueError):
            self.normalize("limiter", {"params": {"thresholdDb": -25.0}})
        with self.assertRaises(ValueError):
            self.normalize("limiter", {"params": {"attackMs": 0.05}})
        with self.assertRaises(ValueError):
            self.normalize("limiter", {"params": {"attackMs": 150.0}})
        with self.assertRaises(ValueError):
            self.normalize("limiter", {"params": {"releaseMs": 0.5}})
        with self.assertRaises(ValueError):
            self.normalize("limiter", {"params": {"releaseMs": 2000.0}})
        with self.assertRaises(ValueError):
            self.normalize("limiter", {"params": {"lookaheadMs": 25.0}})
        with self.assertRaises(ValueError):
            self.normalize("limiter", {"params": {"stereoLinkPercent": 120.0}})
        normalized = self.normalize("limiter", {
            "params": {"thresholdDb": -6.0, "attackMs": 10.0, "releaseMs": 50.0,
                       "lookaheadMs": 5.0, "stereoLinkPercent": 80.0}})
        self.assertEqual(normalized["params"]["releaseMs"], 50.0)

    def test_delay_is_bounded_to_old_contract_of_500_ms(self):
        with self.assertRaisesRegex(ValueError, "between 0 and 500"):
            self.normalize("delay", {"enabled": True, "params": {"leftMs": 501, "rightMs": 0}})
        normalized = self.normalize("delay", {"enabled": True, "params": {"leftMs": 500, "rightMs": 0.5}})
        self.assertEqual(normalized["params"]["leftMs"], 500.0)

    def test_bass_enhancer_contract_ranges_are_restored(self):
        for params in ({"amount": 25.0}, {"amount": -25.0}, {"harmonics": 25.0},
                       {"harmonics": 0.5}, {"scope": 600.0}, {"scope": 10.0},
                       {"blend": 150.0}, {"blend": -150.0}):
            with self.assertRaises(ValueError, msg=str(params)):
                self.normalize("bass_enhancer", {"enabled": True, "params": params})
        normalized = self.normalize("bass_enhancer", {
            "enabled": True,
            "params": {"amount": -12.0, "harmonics": 8.5, "scope": 100.0, "blend": -20.0}})
        self.assertEqual(normalized["params"]["amount"], -12.0)

    def test_calibration_calibrated_flag_is_renormalized(self):
        normalized = self.normalize("loudness", {"params": {
            "calibration": {"requiredAdjustmentDb": 4.25},
            "calibrationProfiles": {"p1": {"requiredAdjustmentDb": 0.5},
                                    "p2": {"requiredAdjustmentDb": 1.5},
                                    "p3": {"name": "no adjustment"}}}})
        self.assertFalse(normalized["params"]["calibration"]["calibrated"])
        self.assertTrue(normalized["params"]["calibrationProfiles"]["p1"]["calibrated"])
        self.assertFalse(normalized["params"]["calibrationProfiles"]["p2"]["calibrated"])
        self.assertNotIn("calibrated", normalized["params"]["calibrationProfiles"]["p3"])


if __name__ == "__main__":
    unittest.main()
