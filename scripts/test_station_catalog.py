#!/usr/bin/env python3
"""Focused regression checks for the built-in radio station catalog."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import stations
from radio_api import add_station_catalog_selection, list_station_catalog


class StationCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_config_home = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.temp_dir.name
        stations._cached_stations = None

    def tearDown(self):
        stations._cached_stations = None
        if self.previous_config_home is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self.previous_config_home
        self.temp_dir.cleanup()

    def station_file(self) -> Path:
        return Path(self.temp_dir.name) / "fxroute" / "stations.json"

    def write_stations(self, items):
        self.station_file().parent.mkdir(parents=True, exist_ok=True)
        self.station_file().write_text(json.dumps(items, indent=2) + "\n", encoding="utf-8")
        stations._cached_stations = None

    def test_catalog_has_expected_groups_and_unique_entries(self):
        catalog = stations.get_station_catalog()
        providers = list(dict.fromkeys(station.provider for station in catalog))

        self.assertEqual(
            providers,
            ["Radio Paradise", "SomaFM", "FIP", "Other Stations"],
        )
        self.assertEqual(len({station.id for station in catalog}), len(catalog))
        self.assertEqual(len({station.input_url for station in catalog}), len(catalog))
        self.assertEqual(len({station.stream_url for station in catalog}), len(catalog))
        self.assertNotIn("live", {station.id for station in catalog})
        self.assertFalse({"nts-1", "nts-2"} & {station.id for station in catalog})
        self.assertTrue(
            {"rp-main", "groovesalad2", "spacestation", "fip-main", "kexp-main"}
            <= {station.id for station in catalog}
        )

    def test_catalog_is_independent_from_personal_defaults(self):
        self.assertEqual(len(stations.DEFAULT_STATIONS), 7)
        self.assertIn("live", {item["id"] for item in stations.DEFAULT_STATIONS})
        self.assertGreater(len(stations.get_station_catalog()), len(stations.DEFAULT_STATIONS))

    def test_catalog_art_uses_full_size_sources_and_includes_kcrw(self):
        catalog = {station.id: station for station in stations.get_station_catalog()}

        self.assertTrue(all(station.image_url for station in catalog.values()))
        self.assertTrue(all("/pikapi/images/" in catalog[station_id].image_url for station_id in catalog if station_id.startswith("fip-")))
        self.assertTrue(catalog["wfmu-main"].image_url.endswith(".svg"))
        self.assertTrue(catalog["kexp-main"].image_url.endswith(".svg"))
        self.assertIn("pressroom.kcrw.com", catalog["kcrw-eclectic24"].image_url)

    def test_add_is_idempotent_and_does_not_change_existing_entry(self):
        existing = dict(stations.DEFAULT_STATIONS[0])
        existing["id"] = "my-groove-salad"
        self.write_stations([existing])
        before = self.station_file().read_bytes()

        first = stations.add_catalog_station("groovesalad")
        second = stations.add_catalog_station("groovesalad")

        self.assertEqual(first.id, "my-groove-salad")
        self.assertEqual(second.id, "my-groove-salad")
        self.assertEqual(self.station_file().read_bytes(), before)

    def test_add_and_remove_change_only_personal_selection(self):
        self.write_stations([])

        added = stations.add_catalog_station("thetrip")
        stations.delete_station(added.id)

        self.assertEqual(stations.get_stations(), [])
        self.assertIn("thetrip", [station.id for station in stations.get_station_catalog()])

    def test_catalog_api_reports_saved_mapping(self):
        self.write_stations([])
        response = asyncio.run(add_station_catalog_selection("poptron"))
        catalog = asyncio.run(list_station_catalog())
        poptron = next(item for item in catalog if item["id"] == "poptron")

        self.assertEqual(response["station"]["id"], "poptron")
        self.assertEqual(poptron["provider"], "SomaFM")
        self.assertTrue(poptron["is_saved"])
        self.assertEqual(poptron["saved_station_id"], "poptron")

    def test_remove_reappears_and_readd_works_without_persisting_provider(self):
        personal = {
            "id": "personal",
            "name": "Personal",
            "input_url": "https://example.test/personal",
            "stream_url": "https://example.test/personal",
            "image_url": None,
            "custom_image_url": None,
        }
        self.write_stations([personal])

        first = stations.add_catalog_station("rp-main")
        saved_catalog = asyncio.run(list_station_catalog())
        self.assertTrue(next(item for item in saved_catalog if item["id"] == "rp-main")["is_saved"])

        raw = json.loads(self.station_file().read_text(encoding="utf-8"))
        self.assertNotIn("provider", next(item for item in raw if item["id"] == first.id))
        self.assertEqual(next(item for item in raw if item["id"] == "personal"), personal)

        stations.delete_station(first.id)
        removed_catalog = asyncio.run(list_station_catalog())
        self.assertFalse(next(item for item in removed_catalog if item["id"] == "rp-main")["is_saved"])
        self.assertEqual(json.loads(self.station_file().read_text(encoding="utf-8")), [personal])

        second = stations.add_catalog_station("rp-main")
        self.assertEqual(second.id, "rp-main")
        self.assertEqual(len(stations.get_stations()), 2)


if __name__ == "__main__":
    unittest.main()
