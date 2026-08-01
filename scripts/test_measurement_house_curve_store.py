#!/usr/bin/env python3
"""Focused compatibility test for the existing house-curve store."""

import tempfile
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from measurement import MeasurementStore


class MeasurementHouseCurveStoreTests(unittest.TestCase):
    def test_upload_lists_parses_and_deletes_existing_format(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(Path(root) / "config"), "XDG_STATE_HOME": str(Path(root) / "state")}):
                store = MeasurementStore(home=Path(root))
            payload = b"20\t1.0\n50\t-0.5\n1000\t-2.0\n20000\t-4.0\n"
            result = store.upload_house_curve_file("Custom House Curve 1.txt", payload)
            curve_id = result["uploaded_house_curve_id"]
            self.assertEqual(result["points"], [[20.0, 1.0], [50.0, -0.5], [1000.0, -2.0], [20000.0, -4.0]])
            self.assertEqual(result["house_curves"][0]["filename"], "Custom-House-Curve-1")
            self.assertEqual(store.list_measurements()["house_curves"][0]["id"], curve_id)
            self.assertEqual((store.house_curves_dir / curve_id).read_bytes(), payload)
            self.assertEqual(store.delete_house_curve_file(curve_id)["house_curves"], [])


if __name__ == "__main__":
    unittest.main()
