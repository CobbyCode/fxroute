#!/usr/bin/env python3
"""B4: measurement results must persist atomically and corrupt files surface.

Covers:
* save_measurement/save_measurements/_persist_job use common.atomic_write_text
  (no partial files visible to readers);
* list_measurements reports corrupt files instead of silently dropping them;
* load_measurement raises a recognizable ValueError for corrupt content.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common.atomic_write import atomic_write_text
from measurement.persistence import MeasurementPersistence


def _store(tmp):
    measurements = tmp / "measurements"
    jobs = tmp / "jobs"
    diagnostics = tmp / "diagnostics"
    for directory in (measurements, jobs, diagnostics):
        directory.mkdir(parents=True, exist_ok=True)
    store = MagicMock()
    store.measurements_dir = measurements
    store.jobs_dir = jobs
    store.job_records_dir = jobs
    store.diagnostics_dir = diagnostics
    store._utc_now = lambda: "2026-01-01T00:00:00Z"
    store._list_calibration_files = lambda: []
    store.get_active_calibration_file_id = lambda: ""
    store._list_house_curve_files = lambda: []
    return store


def _payload(measurement_id="m1"):
    return {
        "id": measurement_id,
        "name": measurement_id,
        "traces": [
            {
                "kind": "measured",
                "label": "T",
                "color": "#fff",
                "points": [[100, 1.0], [200, 2.0]],
            }
        ],
    }


class MeasurementAtomicPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = _store(self.tmp)
        self.persistence = MeasurementPersistence(self.store)

    def test_save_uses_atomic_write(self):
        with patch(
            "measurement.persistence.atomic_write_text",
            side_effect=lambda path, text: atomic_write_text(path, text),
        ) as atomic:
            self.persistence.save_measurement(_payload("m1"))
            self.assertTrue(atomic.called)
        target = self.store.measurements_dir / "m1.json"
        self.assertTrue(target.read_text(encoding="utf-8").endswith("\n"))
        leftovers = [entry.name for entry in self.store.measurements_dir.iterdir() if entry.suffix == ".tmp"]
        self.assertEqual(leftovers, [])

    def test_save_measurements_uses_atomic_write(self):
        with patch(
            "measurement.persistence.atomic_write_text",
            side_effect=lambda path, text: atomic_write_text(path, text),
        ) as atomic:
            self.persistence.save_measurements([_payload("a"), _payload("b")])
            self.assertEqual(atomic.call_count, 2)

    def test_persist_job_uses_atomic_write(self):
        with patch(
            "measurement.persistence.atomic_write_text",
            side_effect=lambda path, text: atomic_write_text(path, text),
        ) as atomic:
            self.persistence._persist_job({"id": "job1", "status": "done"})
            self.assertTrue(atomic.called)
        self.assertTrue((self.store.job_records_dir / "job1.json").exists())

    def test_corrupt_file_is_listed_not_silent(self):
        self.persistence.save_measurement(_payload("good"))
        (self.store.measurements_dir / "bad.json").write_text("{corrupt", encoding="utf-8")
        with self.assertLogs("measurement.persistence", level="WARNING"):
            listed = self.persistence.list_measurements()
        ids = [item["id"] for item in listed["measurements"]]
        self.assertIn("good", ids)
        self.assertNotIn("bad", ids)
        corrupt = listed.get("corrupt_files") or []
        self.assertEqual(len(corrupt), 1)
        self.assertEqual(corrupt[0]["filename"], "bad.json")

    def test_load_corrupt_raises_recognizable_error(self):
        (self.store.measurements_dir / "broken.json").write_text("[1,2", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "corrupt.*broken"):
            self.persistence.load_measurement("broken")


if __name__ == "__main__":
    unittest.main()
