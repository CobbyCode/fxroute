#!/usr/bin/env python3
"""Regression tests for EasyEffects preset filesystem ownership."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from easyeffects import EasyEffectsManager
from easyeffects_persistence import EasyEffectsPresetStore


class EasyEffectsPresetStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.output_dir = root / "output"
        self.irs_dir = root / "irs"
        self.store = EasyEffectsPresetStore(self.output_dir, self.irs_dir)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_strict_and_tolerant_read_contracts_are_distinct(self):
        with self.assertRaisesRegex(ValueError, "Invalid preset name"):
            self.store.read_preset("")
        with self.assertRaisesRegex(FileNotFoundError, "Preset not found: Missing"):
            self.store.read_preset("Missing")

        self.output_dir.mkdir()
        (self.output_dir / "Broken.json").write_text("{")
        with self.assertRaisesRegex(RuntimeError, "Failed to read preset 'Broken'"):
            self.store.read_preset("Broken")
        self.assertIsNone(self.store.try_read_preset("Broken", context="test scan"))

        (self.output_dir / "List.json").write_text("[]")
        with self.assertRaisesRegex(RuntimeError, "not a valid JSON object"):
            self.store.read_preset("List")
        self.assertIsNone(self.store.try_read_preset("List", context="test scan"))

    def test_write_and_list_preserve_contract(self):
        path = self.store.write_preset(
            "Combined.json",
            {"output": {}, "fxroute": {"source_presets": ["A.json", "B"]}},
        )
        self.assertEqual(path.name, "Combined.json")
        self.assertTrue(path.read_text().endswith("\n"))
        self.assertEqual(json.loads(path.read_text())["output"], {})
        self.assertEqual(
            self.store.list_presets(pinned_names=["Direct", "Neutral"])[0]["source_presets"],
            ["A", "B"],
        )

    def test_incomplete_reference_scan_is_reported(self):
        self.store.write_preset(
            "Target", {"output": {"convolver#0": {"kernel-name": "shared"}}}
        )
        self.store.write_preset(
            "Other", {"output": {"convolver#0": {"kernel-name": "other"}}}
        )
        (self.output_dir / "Broken.json").write_text("{")

        referenced, complete = self.store.referenced_kernels_except("Target")
        self.assertEqual(referenced, {"other"})
        self.assertFalse(complete)

    def test_samplerate_probe_tolerates_stale_active_preset(self):
        manager = EasyEffectsManager.__new__(EasyEffectsManager)
        manager.preset_store = self.store
        manager.get_active_preset = lambda: "Missing"
        self.assertFalse(manager.active_preset_requires_samplerate_reload())

        self.store.write_preset(
            "Convolver",
            {"output": {"convolver#0": {"bypass": False, "kernel-name": "room"}}},
        )
        manager.get_active_preset = lambda: "Convolver"
        self.assertTrue(manager.active_preset_requires_samplerate_reload())


if __name__ == "__main__":
    unittest.main(verbosity=2)
