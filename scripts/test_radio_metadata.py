#!/usr/bin/env python3
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radio_metadata import (
    RadioMetadataService, parse_fip, parse_kexp, parse_radio_paradise, parse_somafm,
)


NOW = 1_785_650_000.0


class ParserTests(unittest.TestCase):
    def test_radio_paradise_full_and_timing(self):
        value, _ = parse_radio_paradise("rp-main", 0, [{
            "event": "42", "artist": "Artist", "title": "Title", "album": "Album",
            "cover": "https://cover", "sched_time": NOW - 20, "duration": 200000,
        }], NOW)
        self.assertEqual(value["track_id"], "rp:0:42")
        self.assertEqual(value["progress_seconds"], 20)
        self.assertEqual(value["duration_seconds"], 200)

    def test_fip_partial_and_separator(self):
        value, _ = parse_fip("fip-hiphop", 95, {"now": {
            "secondLine": "Artist • Title", "secondLineSongUuid": "uuid", "cover": "cover",
            "startTime": NOW - 5, "endTime": NOW + 95,
        }, "delayToRefresh": 95000}, NOW)
        self.assertEqual((value["artist"], value["title"]), ("Artist", "Title"))
        self.assertEqual(value["duration_seconds"], 100)
        no_artist, _ = parse_fip("fip-main", 7, {"now": {"secondLine": "Only title"}}, NOW)
        self.assertIsNone(no_artist["artist"])
        self.assertEqual(no_artist["title"], "Only title")

    def test_soma_history_and_kexp_track_filter(self):
        soma, _ = parse_somafm("groovesalad", {"songs": [
            {"title": "Now", "artist": "A", "date": str(int(NOW))},
            {"title": "Before", "artist": "B", "date": str(int(NOW - 10))},
        ]}, NOW)
        self.assertEqual(soma["history"][0]["title"], "Before")
        kexp, _ = parse_kexp("kexp-main", {"results": [
            {"id": 1, "play_type": "airbreak", "song": "Ignore"},
            {"id": 2, "play_type": "trackplay", "song": "Song", "artist": "Artist", "airdate": "2026-08-02T03:00:00Z"},
        ]}, NOW)
        self.assertEqual(kexp["track_id"], "kexp:2")

    def test_malformed_rejected(self):
        for parser, args in (
            (parse_radio_paradise, ("rp-main", 0, {}, NOW)),
            (parse_fip, ("fip-main", 7, {"now": {}}, NOW)),
            (parse_somafm, ("groovesalad", {"songs": []}, NOW)),
            (parse_kexp, ("kexp-main", {"results": []}, NOW)),
        ):
            with self.assertRaises(ValueError):
                parser(*args)


class FakeResponse:
    def __init__(self, payload): self.payload = payload
    def raise_for_status(self): return None
    def json(self): return self.payload


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_singleflight_and_failure_stale_expiry(self):
        calls = []
        now = [NOW]
        payload = [{"event": "1", "title": "Track", "sched_time": NOW, "duration": 100000}]
        def get(*args, **kwargs):
            calls.append(args[0])
            return FakeResponse(payload)
        service = RadioMetadataService(http_get=get, clock=lambda: now[0])
        a, b = await asyncio.gather(service.get("rp-main"), service.get("rp-main"))
        self.assertEqual(a["track_id"], b["track_id"])
        self.assertEqual(len(calls), 1)
        now[0] += 5
        cached = await service.get("rp-main")
        self.assertEqual(cached["progress_seconds"], 5)
        self.assertEqual(len(calls), 1)
        now[0] += 26
        service._http_get = lambda *a, **k: (_ for _ in ()).throw(TimeoutError())
        stale = await service.get("rp-main")
        self.assertTrue(stale["stale"])
        now[0] += 91
        self.assertIsNone(await service.get("rp-main"))

    async def test_station_switch_results_are_separate(self):
        def get(url, **kwargs):
            channel = "0" if "chan=0" in url else "1"
            return FakeResponse([{"event": channel, "title": f"Track {channel}", "sched_time": NOW, "duration": 100000}])
        service = RadioMetadataService(http_get=get, clock=lambda: NOW)
        main, mellow = await asyncio.gather(service.get("rp-main"), service.get("rp-mellow"))
        self.assertEqual(main["station_id"], "rp-main")
        self.assertEqual(mellow["station_id"], "rp-mellow")
        self.assertNotEqual(main["track_id"], mellow["track_id"])

    def test_provider_mapping_does_not_use_display_name(self):
        self.assertIsNone(RadioMetadataService.provider_for("my-radio-paradise-copy", "https://example.test/radio"))
        self.assertEqual(RadioMetadataService.provider_for("custom", "https://ice5.somafm.com/lush-128-aac")[0], "soma")


if __name__ == "__main__":
    unittest.main()
