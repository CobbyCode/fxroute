#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
#
# EasyEffects-era state migration: SPL/Loudness calibration profiles and
# global AutoGain/Bass settings must survive the move to the native DSP
# state, exactly once, without overwriting newer state.

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dsp.manager import DSPManager

LEGACY_EXTRAS = {
    "limiter": {"enabled": True, "params": {
        "thresholdDb": -1.0, "attackMs": 5.0, "releaseMs": 50.0,
        "lookaheadMs": 5.0, "stereoLinkPercent": 100.0}},
    "headroom": {"enabled": False, "params": {"gainDb": -3.0}},
    "delay": {"enabled": False, "params": {"leftMs": 0.0, "rightMs": 0.0}},
    "bass_enhancer": {"enabled": True, "params": {
        "amount": -6.0, "harmonics": 10.0, "scope": 90.0, "blend": -15.0}},
    "autogain": {"enabled": True, "params": {
        "targetDb": -18.0, "reference": "Geometric Mean (MSI)",
        "silenceThresholdDb": -65.0, "maximumHistorySeconds": 15}},
    "loudness": {"enabled": True, "params": {
        "fftSize": 4096, "strength": 7, "volumeDb": -22.5,
        "calibration": {"outputProfileId": "out-1", "requiredAdjustmentDb": 1.5},
        "calibrationProfiles": {
            "out-1": {"outputProfileId": "out-1", "requiredAdjustmentDb": 0.5},
            "out-2": {"outputProfileId": "out-2", "requiredAdjustmentDb": -1.5},
        }}},
    "tone_effect": {"enabled": True, "mode": "crystalizer"},
}


class DSPMigrationTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="fxroute-migration-"))
        self.legacy = self.home / ".var/app/com.github.wwmm.easyeffects/config/easyeffects/agent-output-extras.json"

    def write_legacy(self, payload):
        self.legacy.parent.mkdir(parents=True)
        self.legacy.write_text(json.dumps(payload))

    def test_flatpak_legacy_extras_are_migrated_once(self):
        self.write_legacy(LEGACY_EXTRAS)
        manager = DSPManager(home=self.home)
        extras = manager.load_global_extras()
        self.assertTrue(extras["loudness"]["enabled"])
        self.assertEqual(extras["loudness"]["params"]["volumeDb"], -22.5)
        self.assertEqual(extras["loudness"]["params"]["strength"], 7)
        self.assertEqual(extras["loudness"]["params"]["calibrationProfiles"]["out-1"]["requiredAdjustmentDb"], 0.5)
        self.assertTrue(extras["loudness"]["params"]["calibrationProfiles"]["out-1"]["calibrated"])
        self.assertFalse(extras["loudness"]["params"]["calibrationProfiles"]["out-2"]["calibrated"])
        self.assertTrue(extras["autogain"]["enabled"])
        self.assertEqual(extras["autogain"]["params"]["targetDb"], -18.0)
        self.assertEqual(extras["bass_enhancer"]["params"]["amount"], -6.0)
        self.assertEqual(extras["tone_effect"]["mode"], "crystalizer")
        migration = manager.state_store.read("migration.json", {})
        self.assertTrue(migration.get("extras_migrated"))
        self.assertEqual(migration.get("source"), str(self.legacy))

    def test_native_legacy_extras_path_is_used_when_flatpak_is_absent(self):
        native = self.home / ".config/easyeffects/agent-output-extras.json"
        native.parent.mkdir(parents=True)
        native.write_text(json.dumps(LEGACY_EXTRAS))
        manager = DSPManager(home=self.home)
        migration = manager.state_store.read("migration.json", {})
        self.assertEqual(migration.get("source"), str(native))
        self.assertTrue(manager.load_global_extras()["loudness"]["enabled"])

    def test_migration_runs_exactly_once_and_never_overwrites_newer_state(self):
        self.write_legacy(LEGACY_EXTRAS)
        manager = DSPManager(home=self.home)
        self.legacy.write_text(json.dumps({**LEGACY_EXTRAS, "loudness": {
            "enabled": True, "params": {"fftSize": 4096, "strength": 1, "volumeDb": 0.0}}}))
        DSPManager(home=self.home)
        self.assertEqual(DSPManager(home=self.home).load_global_extras()["loudness"]["params"]["strength"], 7)
        manager.save_global_extras(manager.normalize_effects_extras({
            "loudness": {"enabled": True, "params": {"volumeDb": -40.0}}}))
        DSPManager(home=self.home)
        self.assertEqual(DSPManager(home=self.home).load_global_extras()["loudness"]["params"]["volumeDb"], -40.0)

    def test_invalid_legacy_json_is_skipped_without_state(self):
        self.legacy.parent.mkdir(parents=True)
        self.legacy.write_text("{not json")
        manager = DSPManager(home=self.home)
        self.assertFalse(manager.global_extras_file.exists())
        self.assertFalse(manager.state_store.read("migration.json", {}).get("extras_migrated"))

    def test_invalid_legacy_section_does_not_block_calibration_migration(self):
        legacy = dict(LEGACY_EXTRAS)
        legacy["autogain"] = {"enabled": True, "params": {"maximumHistorySeconds": 3}}
        self.write_legacy(legacy)
        manager = DSPManager(home=self.home)
        extras = manager.load_global_extras()
        self.assertEqual(extras["loudness"]["params"]["calibrationProfiles"]["out-1"]["requiredAdjustmentDb"], 0.5)
        self.assertEqual(extras["autogain"]["params"]["maximumHistorySeconds"], 15)
        self.assertFalse(extras["autogain"]["enabled"])

    def test_existing_extras_json_is_never_overwritten(self):
        manager = DSPManager(home=self.home)
        manager.save_global_extras({"loudness": {"enabled": True, "params": {"volumeDb": -33.0}}})
        self.write_legacy(LEGACY_EXTRAS)
        reloaded = DSPManager(home=self.home)
        self.assertEqual(reloaded.load_global_extras()["loudness"]["params"]["volumeDb"], -33.0)
        self.assertFalse(reloaded.state_store.read("migration.json", {}).get("extras_migrated"))

    def test_calibration_flows_into_confirmed_work_point_after_migration(self):
        self.write_legacy(LEGACY_EXTRAS)
        manager = DSPManager(home=self.home)
        extras = manager.load_global_extras()
        payload = manager._loudness_plugin_payload(
            extras["loudness"], extras["autogain"])
        expected = manager._loudness_plugin_payload(
            manager.normalize_effects_extras(LEGACY_EXTRAS)["loudness"],
            manager.normalize_effects_extras(LEGACY_EXTRAS)["autogain"])
        self.assertTrue(math_isclose(float(payload["volume"]), float(expected["volume"])))
        # The legacy calibration profile is part of the persisted extras, so
        # the confirmed work point reflects it without any runtime mirror.
        uncalibrated = copy.deepcopy(extras)
        uncalibrated["loudness"]["params"]["calibration"] = {}
        uncalibrated["loudness"]["params"]["calibrationProfiles"] = {}
        uncalibrated_payload = manager._loudness_plugin_payload(
            manager.normalize_effects_extras(uncalibrated)["loudness"],
            manager.normalize_effects_extras(uncalibrated)["autogain"])
        self.assertFalse(math_isclose(float(payload["volume"]), float(uncalibrated_payload["volume"])))


def math_isclose(a, b):
    return abs(a - b) < 1e-9


if __name__ == "__main__":
    unittest.main()
