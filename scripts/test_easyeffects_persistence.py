#!/usr/bin/env python3
"""Regression tests for EasyEffects preset filesystem ownership."""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import easyeffects_persistence
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

    def _fd_count(self) -> int:
        return len(os.listdir("/proc/self/fd"))

    def test_atomic_write_never_touches_process_umask(self):
        path = self.store.write_preset("Umask", {"output": {}})
        with mock.patch.object(
            easyeffects_persistence.os,
            "umask",
            side_effect=AssertionError("os.umask must not be used"),
        ):
            self.store.write_preset("Umask", {"output": {"v": 2}})
        self.assertEqual(json.loads(path.read_text())["output"]["v"], 2)

    def test_atomic_write_preserves_existing_file_mode(self):
        self.store.write_preset("Mode", {"output": {"v": 1}})
        path = self.output_dir / "Mode.json"
        os.chmod(path, 0o640)
        self.store.write_preset("Mode", {"output": {"v": 2}})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        self.assertEqual(json.loads(path.read_text())["output"]["v"], 2)

    def test_atomic_write_new_file_gets_no_permissive_mode(self):
        path = self.store.write_preset("New", {"output": {}})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o077, 0)

    def test_atomic_write_fchmod_failure_leaves_no_temp_or_open_fd(self):
        self.store.write_preset("Fail", {"output": {"v": 1}})
        before = self._fd_count()
        with mock.patch.object(
            easyeffects_persistence.os,
            "fchmod",
            side_effect=OSError("simulated fchmod failure"),
        ):
            with self.assertRaises(OSError):
                self.store.write_preset("Fail", {"output": {"v": 2}})
        # The old target survives untouched; no temp file and no fd remains.
        self.assertEqual(
            [path.name for path in self.output_dir.iterdir()], ["Fail.json"]
        )
        self.assertEqual(self._fd_count(), before)

    def test_atomic_write_replace_failure_removes_temp_file(self):
        with mock.patch.object(
            easyeffects_persistence.os,
            "replace",
            side_effect=OSError("simulated replace failure"),
        ):
            with self.assertRaises(OSError):
                self.store.write_preset("Fail", {"output": {"v": 2}})
        self.assertEqual(list(self.output_dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
