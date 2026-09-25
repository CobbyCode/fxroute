#!/usr/bin/env python3
"""Station import requests are bounded before station mutation begins."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
import main
import radio.api as radio_api


class StationImportLimitTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def test_five_entries_are_accepted(self):
        # Missing URLs produce skipped results without touching the store/network.
        response = self.client.post("/api/stations/import", json=[{"name": "No URL"}] * 5)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([entry["status"] for entry in response.json()["results"]], ["skipped"] * 5)

    def test_six_entries_rejected_before_acquiring_mutation_lock(self):
        with mock.patch.object(radio_api, "_run_locked_station_worker", side_effect=AssertionError("worker called")):
            response = self.client.post("/api/stations/import", json=[{"name": "No URL"}] * 6)
        self.assertEqual(response.status_code, 413, response.text)
        self.assertIn("5", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
