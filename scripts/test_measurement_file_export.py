#!/usr/bin/env python3
"""Focused tests for managed calibration and house-curve file exports."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from measurement import MeasurementStore


class MeasurementFileExportTests(unittest.TestCase):
    def make_store(self, root):
        return patch.dict(
            "os.environ",
            {
                "XDG_CONFIG_HOME": str(Path(root) / "config"),
                "XDG_STATE_HOME": str(Path(root) / "state"),
            },
        )

    def test_calibration_export_uses_managed_file_and_original_name_and_bytes(self):
        with tempfile.TemporaryDirectory() as root, self.make_store(root):
            store = MeasurementStore(home=Path(root))
            payload = b"# calibration header\r\n20,0.25\r\n1000,-1.5\r\n"
            meta = store._store_calibration_file("UMIK-1 calibration.csv", payload)
            path, filename = store.get_calibration_file_for_export(meta["id"])
            self.assertEqual(filename, "UMIK-1-calibration.csv")
            self.assertEqual(path.read_bytes(), payload)

    def test_house_curve_export_supports_imported_and_custom_files(self):
        with tempfile.TemporaryDirectory() as root, self.make_store(root):
            store = MeasurementStore(home=Path(root))
            imported = b"20\t1.0\n1000\t-0.5\n20000\t-4.0\n"
            created = store.upload_house_curve_file("Custom House Curve 1.txt", imported)
            path, filename = store.get_house_curve_file_for_export(created["uploaded_house_curve_id"])
            self.assertEqual(filename, "Custom-House-Curve-1.txt")
            self.assertEqual(path.read_bytes(), imported)

    def test_empty_builtin_and_arbitrary_paths_are_not_exportable(self):
        with tempfile.TemporaryDirectory() as root, self.make_store(root):
            store = MeasurementStore(home=Path(root))
            for ref in ("", "neutral", "../outside.txt", str(Path(root) / "outside.txt")):
                with self.subTest(ref=ref):
                    with self.assertRaises((ValueError, KeyError)):
                        store.get_house_curve_file_for_export(ref)
                    with self.assertRaises((ValueError, KeyError)):
                        store.get_calibration_file_for_export(ref)


if __name__ == "__main__":
    unittest.main()
