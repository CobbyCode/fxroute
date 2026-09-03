#!/usr/bin/env python3
"""Measurement delete must refuse ids that escape the measurements directory.

merge_measurements validates that each id is a plain file name; delete lacked
the same guard and could unlink files outside the measurements directory when
handed a crafted id such as ``../dsp/presets/Neutral``.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from measurement.store import MeasurementStore


class MeasurementDeleteGuardTests(unittest.TestCase):
    def _store(self, tempdir: str) -> MeasurementStore:
        return MeasurementStore(home=Path(tempdir))

    def test_delete_refuses_traversal_id_and_leaves_target_intact(self):
        with tempfile.TemporaryDirectory() as tempdir, mock.patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            # Mirrors the real layout: DSP presets live one level below the
            # measurements directory inside the same config root.
            outside = store.measurements_dir.parent / "dsp" / "presets"
            outside.mkdir(parents=True, exist_ok=True)
            sentinel = outside / "Neutral.json"
            sentinel.write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "Invalid measurement id"):
                store.delete_measurement("../dsp/presets/Neutral")
            self.assertTrue(sentinel.exists())

    def test_delete_refuses_traversal_and_empty_id(self):
        with tempfile.TemporaryDirectory() as tempdir, mock.patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            for bad_id in ("", "  ", "a/b", "sub/x", "../dsp/presets/Neutral"):
                with self.assertRaisesRegex(ValueError, "Invalid measurement id"):
                    store.delete_measurement(bad_id)

    def test_delete_of_saved_measurement_still_works(self):
        with tempfile.TemporaryDirectory() as tempdir, mock.patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            saved = store.save_measurement({
                "id": "single-check",
                "name": "Single check",
                "channel": "left",
                "traces": [{
                    "kind": "sweep-response",
                    "label": "single",
                    "role": "trusted",
                    "points": [[20.0, 0.0], [20000.0, 0.0]],
                }],
            })
            self.assertEqual(len(store.list_measurements()["measurements"]), 1)

            store.delete_measurement(saved["id"])
            self.assertEqual(store.list_measurements()["measurements"], [])
            with self.assertRaises(KeyError):
                store.delete_measurement(saved["id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
