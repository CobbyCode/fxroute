#!/usr/bin/env python3
"""Focused backend checks for the Radio Browser integration."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import radio_api
import stations


def browser_item(index=1):
    return {
        "stationuuid": f"uuid-{index}",
        "name": f"Station {index}",
        "url": f"https://example.test/input-{index}",
        "url_resolved": f"https://example.test/stream-{index}",
        "favicon": f"https://example.test/icon-{index}.png",
        "country": "Germany",
        "countrycode": "DE",
        "language": "german",
        "tags": "ambient,electronic",
        "codec": "MP3",
        "bitrate": 192,
        "lastcheckok": 1,
        "clickcount": 100 - index,
    }


class FakeResponse:
    def __init__(self, data, status=200):
        self.data = data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self.data


class RadioBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_config_home = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.temp_dir.name
        path = Path(self.temp_dir.name) / "fxroute" / "stations.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[]\n", encoding="utf-8")
        stations._cached_stations = None
        radio_api._station_mutation_lock = None

    def tearDown(self):
        stations._cached_stations = None
        radio_api._station_mutation_lock = None
        if self.previous_config_home is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self.previous_config_home
        self.temp_dir.cleanup()

    @patch("radio_api.requests.get")
    def test_search_contract_uses_four_independent_or_queries(self, mock_get):
        def response_for(*_args, **kwargs):
            field = next(key for key in ("name", "country", "language", "tag") if key in kwargs["params"])
            item = browser_item(("name", "country", "language", "tag").index(field) + 1)
            item["name"] = {"name": "WDR", "country": "Deutschland Radio", "language": "Deutsch FM", "tag": "Jazz FM"}[field]
            return FakeResponse([item])

        mock_get.side_effect = response_for
        results = asyncio.run(radio_api.search_station_browser(" search term "))

        self.assertEqual(len(results), 4)
        self.assertEqual(mock_get.call_count, 4)
        expected_user_agent = (
            f"FXRoute/{(radio_api.BASE_DIR / 'VERSION').read_text(encoding='utf-8').strip()}"
        )
        field_sets = []
        for call in mock_get.call_args_list:
            params = call.kwargs["params"]
            fields = [field for field in ("name", "country", "language", "tag") if field in params]
            field_sets.append(fields)
            self.assertEqual(len(fields), 1)
            self.assertEqual(params[fields[0]], "search term")
            self.assertEqual(params["hidebroken"], "true")
            self.assertEqual(params["order"], "clickcount")
            self.assertEqual(params["reverse"], "true")
            self.assertEqual(params["limit"], radio_api.RADIO_BROWSER_QUERY_LIMIT)
            self.assertEqual(call.kwargs["timeout"], (2, 5))
            self.assertEqual(call.kwargs["headers"]["User-Agent"], expected_user_agent)
        self.assertCountEqual(field_sets, [["name"], ["country"], ["language"], ["tag"]])

    @patch("radio_api.requests.get")
    def test_search_merges_deduplicates_ranks_and_limits(self, mock_get):
        items_by_field = {}
        for field_index, field in enumerate(("name", "country", "language", "tag")):
            items = [browser_item(field_index * 20 + i) for i in range(1, 13)]
            items_by_field[field] = items
        items_by_field["name"][0]["name"] = "Jazz Name Match"
        items_by_field["tag"][0] = dict(items_by_field["name"][0])
        items_by_field["language"][1]["url"] = items_by_field["country"][1]["url"]
        items_by_field["language"][1]["url_resolved"] = items_by_field["country"][1]["url_resolved"]

        def response_for(*_args, **kwargs):
            field = next(key for key in items_by_field if key in kwargs["params"])
            return FakeResponse(items_by_field[field])

        mock_get.side_effect = response_for
        results = asyncio.run(radio_api.search_station_browser("Jazz"))

        self.assertEqual(len(results), 30)
        self.assertEqual(results[0]["title"], "Jazz Name Match")
        self.assertEqual(len({item["stationuuid"] for item in results}), 30)
        result_urls = [{item["url"], item["url_resolved"]} for item in results]
        for index, urls in enumerate(result_urls):
            self.assertFalse(any(urls & other for other in result_urls[index + 1:]))

    @patch("radio_api.requests.get")
    def test_search_filters_codec_specific_low_bitrate_results(self, mock_get):
        low_aac = browser_item(101)
        low_aac.update(codec="AAC", bitrate=95)
        accepted_aac = browser_item(102)
        accepted_aac.update(codec="AAC+", bitrate=96)
        low_mp3 = browser_item(103)
        low_mp3.update(codec="MP3", bitrate=127)
        accepted_mp3 = browser_item(104)
        accepted_mp3.update(codec="MP3", bitrate=128)
        low_ogg = browser_item(105)
        low_ogg.update(codec="OGG", bitrate=95)
        accepted_ogg_vorbis = browser_item(106)
        accepted_ogg_vorbis.update(codec="OGG Vorbis", bitrate=96)
        low_vorbis = browser_item(107)
        low_vorbis.update(codec="Vorbis", bitrate=95)
        accepted_vorbis = browser_item(108)
        accepted_vorbis.update(codec="VORBIS", bitrate=96)
        other_codec = browser_item(109)
        other_codec.update(codec="OPUS", bitrate=64)
        mock_get.return_value = FakeResponse([
            low_aac,
            accepted_aac,
            low_mp3,
            accepted_mp3,
            low_ogg,
            accepted_ogg_vorbis,
            low_vorbis,
            accepted_vorbis,
            other_codec,
        ])

        results = asyncio.run(radio_api.search_station_browser("Station"))

        self.assertEqual(
            {item["stationuuid"] for item in results},
            {"uuid-102", "uuid-104", "uuid-106", "uuid-108", "uuid-109"},
        )

    @patch("radio_api.requests.get", side_effect=requests.Timeout())
    def test_timeout_is_reported_cleanly(self, _mock_get):
        with self.assertRaises(radio_api.HTTPException) as context:
            asyncio.run(radio_api.search_station_browser("ambient"))
        self.assertEqual(context.exception.status_code, 504)

    @patch("radio_api.requests.get")
    def test_invalid_provider_response_is_rejected(self, mock_get):
        mock_get.return_value = FakeResponse({"unexpected": True})
        with self.assertRaises(radio_api.HTTPException) as context:
            asyncio.run(radio_api.search_station_browser("ambient"))
        self.assertEqual(context.exception.status_code, 502)

    @patch("radio_api.requests.get")
    def test_add_by_uuid_uses_input_url_and_is_idempotent(self, mock_get):
        item = browser_item(7)
        item["url"] = "https://example.test/direct-input"
        item["url_resolved"] = "https://example.test/direct-resolved"
        mock_get.return_value = FakeResponse([item])

        first = asyncio.run(radio_api.add_station_browser_selection("uuid-7"))
        second = asyncio.run(radio_api.add_station_browser_selection("uuid-7"))
        saved = stations.get_stations()

        self.assertEqual(first["station"]["id"], second["station"]["id"])
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].input_url, item["url"])


if __name__ == "__main__":
    unittest.main()
