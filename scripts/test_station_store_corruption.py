#!/usr/bin/env python3
"""Regression tests: a corrupt stations.json must never be overwritten.

Fail-closed contract for the station store:
- valid store -> mutations work normally
- syntactically broken JSON -> every mutation raises
  StationStoreCorruptedError and the file stays byte-identical (no
  fallback to defaults plus the new change)
- structurally invalid content (valid JSON, not an array) -> every
  mutation raises StationStoreCorruptedError and the file stays unchanged
- reads stay available during corruption (defaults fallback, uncached
  and never persisted back, so lazy artwork enrichment cannot repair
  the damaged file with defaults either)
- after restoring a valid store, mutations and reads work again without
  any manual cache reset
- the HTTP layer reports store corruption as 500, not as a 400
  client-validation error

All scenarios use a temporary XDG_CONFIG_HOME and non-SomaFM example
URLs, so no network access happens and no user station file is touched.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import radio.api as radio_api
import radio.stations as stations
from radio.stations import StationStoreCorruptedError

BROKEN_PAYLOADS = (
    b"{broken",
    b'[{"id": "mine", "name": "Mine", "stream_url": "https://example.com/x"',
    b"\xff\xfe not json \x00",
)

STRUCTURALLY_INVALID_PAYLOADS = (
    b'{"id": "mine", "name": "Mine"}',
    b"null",
    b'"just a string"',
    b"42",
)

USER_STATION = {
    "id": "mine",
    "name": "Mine",
    "input_url": "https://example.com/mine.mp3",
    "stream_url": "https://example.com/mine.mp3",
    "image_url": None,
    "custom_image_url": None,
}


class StationStoreCorruptionTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config_root = Path(self._tmp.name)
        os.environ["XDG_CONFIG_HOME"] = str(self.config_root)
        self.stations_file = self.config_root / "fxroute" / "stations.json"
        self.stations_file.parent.mkdir(parents=True, exist_ok=True)
        legacy_patch = patch.object(
            stations,
            "_legacy_stations_file",
            return_value=self.config_root / "legacy-stations.json",
        )
        legacy_patch.start()
        self.addCleanup(legacy_patch.stop)
        self.addCleanup(self._tmp.cleanup)
        self._reset_cache()
        radio_api._station_mutation_lock = None
        self.addCleanup(setattr, radio_api, "_station_mutation_lock", None)
        self._default_snapshot = copy.deepcopy(stations.DEFAULT_STATIONS)

    def _reset_cache(self):
        with stations._cache_lock:
            stations._cached_stations = None
            stations._cache_generation = 0

    def _write_bytes(self, payload: bytes):
        self.stations_file.write_bytes(payload)
        self._reset_cache()

    def _seed(self, payload):
        self._write_bytes((json.dumps(payload, indent=2) + "\n").encode("utf-8"))

    def _disk_bytes(self) -> bytes:
        return self.stations_file.read_bytes()

    def _mutations(self):
        return (
            lambda: stations.add_station("New", "https://example.com/new.mp3"),
            lambda: stations.update_station("mine", "Mine", "https://example.com/mine.mp3"),
            lambda: stations.delete_station("mine"),
            lambda: stations.add_catalog_station("rp-main"),
        )

    def test_valid_store_mutations_work(self):
        self._seed([dict(USER_STATION)])
        created = stations.add_station("New", "https://example.com/new.mp3")
        self.assertEqual(created.stream_url, "https://example.com/new.mp3")
        updated = stations.update_station("mine", "Mine Renamed", "https://example.com/mine.mp3")
        self.assertEqual(updated.name, "Mine Renamed")
        catalog_added = stations.add_catalog_station("rp-main")
        self.assertEqual(catalog_added.id, "rp-main")
        stations.delete_station(created.id)
        self.assertEqual(
            sorted(item["id"] for item in json.loads(self.stations_file.read_text(encoding="utf-8"))),
            ["mine", "rp-main"],
        )

    def test_broken_json_mutations_fail_and_preserve_bytes(self):
        for payload in BROKEN_PAYLOADS:
            with self.subTest(payload=payload):
                self._write_bytes(payload)
                for mutate in self._mutations():
                    with self.assertRaises(StationStoreCorruptedError):
                        mutate()
                self.assertEqual(self._disk_bytes(), payload)
        self.assertEqual(stations.DEFAULT_STATIONS, self._default_snapshot)

    def test_structurally_invalid_mutations_fail_and_preserve_bytes(self):
        for payload in STRUCTURALLY_INVALID_PAYLOADS:
            with self.subTest(payload=payload):
                self._write_bytes(payload)
                for mutate in self._mutations():
                    with self.assertRaises(StationStoreCorruptedError):
                        mutate()
                self.assertEqual(self._disk_bytes(), payload)

    def test_corruption_error_is_recognizable(self):
        self._write_bytes(b"{broken")
        with self.assertRaises(StationStoreCorruptedError) as ctx:
            stations.add_station("New", "https://example.com/new.mp3")
        self.assertIsInstance(ctx.exception, ValueError)
        self.assertIn("corrupt", str(ctx.exception))

    def test_reads_fall_back_to_defaults_without_touching_the_file(self):
        self._write_bytes(b"{broken")
        fallback = stations.get_stations()
        self.assertEqual(
            [station.id for station in fallback],
            [item["id"] for item in stations.DEFAULT_STATIONS],
        )
        enriched = stations.get_stations(enrich_missing_art=True)
        self.assertEqual(
            [station.id for station in enriched],
            [item["id"] for item in stations.DEFAULT_STATIONS],
        )
        self.assertEqual(self._disk_bytes(), b"{broken")
        self.assertEqual(stations.DEFAULT_STATIONS, self._default_snapshot)

    def test_reads_fall_back_for_structurally_invalid_store(self):
        for payload in STRUCTURALLY_INVALID_PAYLOADS:
            with self.subTest(payload=payload):
                self._write_bytes(payload)
                fallback = stations.get_stations()
                self.assertEqual(
                    [station.id for station in fallback],
                    [item["id"] for item in stations.DEFAULT_STATIONS],
                )
                self.assertEqual(self._disk_bytes(), payload)
        self.assertEqual(stations.DEFAULT_STATIONS, self._default_snapshot)

    def test_recovery_after_restore_needs_no_cache_reset(self):
        self._write_bytes(b"{broken")
        with self.assertRaises(StationStoreCorruptedError):
            stations.add_station("New", "https://example.com/new.mp3")
        self.assertEqual(
            [station.id for station in stations.get_stations()],
            [item["id"] for item in stations.DEFAULT_STATIONS],
        )
        self._seed([dict(USER_STATION)])
        created = stations.add_station("New", "https://example.com/new.mp3")
        self.assertEqual(created.stream_url, "https://example.com/new.mp3")
        self.assertEqual(
            sorted(station.id for station in stations.get_stations()),
            ["mine", created.id],
        )

    def test_api_reports_corruption_as_server_error(self):
        self._write_bytes(b"{broken")
        before = self._disk_bytes()
        with self.assertRaises(radio_api.HTTPException) as ctx:
            asyncio.run(
                radio_api.create_station(
                    SimpleNamespace(
                        name="New",
                        stream_url="https://example.com/new.mp3",
                        custom_image_url=None,
                    )
                )
            )
        self.assertEqual(ctx.exception.status_code, 500)
        with self.assertRaises(radio_api.HTTPException) as ctx:
            asyncio.run(radio_api.remove_station("mine"))
        self.assertEqual(ctx.exception.status_code, 500)
        with self.assertRaises(radio_api.HTTPException) as ctx:
            asyncio.run(radio_api.add_station_catalog_selection("rp-main"))
        self.assertEqual(ctx.exception.status_code, 500)
        with self.assertRaises(radio_api.HTTPException) as ctx:
            asyncio.run(
                radio_api.edit_station(
                    "mine",
                    SimpleNamespace(
                        name="Mine",
                        stream_url="https://example.com/mine.mp3",
                        custom_image_url=None,
                    ),
                )
            )
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertEqual(self._disk_bytes(), before)

    def test_api_reports_structural_corruption_as_server_error(self):
        self._write_bytes(b'{"id": "mine", "name": "Mine"}')
        before = self._disk_bytes()
        with self.assertRaises(radio_api.HTTPException) as ctx:
            asyncio.run(
                radio_api.create_station(
                    SimpleNamespace(
                        name="New",
                        stream_url="https://example.com/new.mp3",
                        custom_image_url=None,
                    )
                )
            )
        self.assertEqual(ctx.exception.status_code, 500)
        with self.assertRaises(radio_api.HTTPException) as ctx:
            asyncio.run(radio_api.remove_station("mine"))
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertEqual(self._disk_bytes(), before)


if __name__ == "__main__":
    unittest.main()
