#!/usr/bin/env python3
import asyncio
import io
import json
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radio.metadata import (
    RadioMetadataService, parse_fip, parse_kexp, parse_radio_paradise, parse_somafm,
)
import safe_http
import radio.stations as stations


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
    async def test_default_fetch_rejects_private_provider_dns_without_requesting_it(self):
        response = requests.Response()
        response.status_code = 200
        response._content = b'[{"event": "1", "title": "Track"}]'
        session = MagicMock()
        with patch.object(safe_http.socket, "getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443)),
        ]), patch.object(safe_http, "_build_public_session", return_value=session), \
                patch("requests.api.request", return_value=response):
            result = await RadioMetadataService(clock=lambda: NOW).get("rp-main")
        self.assertTrue(result is None)
        session.get.assert_not_called()

    async def test_default_fetch_bounds_provider_json_and_preserves_soma_slug(self):
        body = json.dumps({"songs": [{"title": "Now", "album": "X" * (5 * 1024 * 1024)}]}).encode()

        def response():
            result = requests.Response()
            result.status_code = 200
            result.raw = io.BytesIO(body)
            return result

        session = MagicMock()
        session.get.side_effect = lambda *args, **kwargs: response()
        with patch.object(safe_http.socket, "getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443)),
        ]), patch.object(safe_http, "_build_public_session", return_value=session), \
                patch("requests.api.request", side_effect=lambda *args, **kwargs: response()):
            result = await RadioMetadataService(clock=lambda: NOW).get(
                "custom", "https://ice5.somafm.com/lush-128-aac"
            )
        self.assertTrue(result is None)
        self.assertTrue(session.get.called)
        self.assertEqual(session.get.call_args.args[0], "https://somafm.com/songs/lush.json")

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

    def test_somafm_domain_and_subdomain_not_lookalike(self):
        for url in ("https://somafm.com/lush130.pls", "https://Ice5.SomaFM.Com:443/lush-128-aac"):
            with self.subTest(url=url):
                self.assertEqual(stations._extract_somafm_slug("", url), "lush")
                self.assertEqual(RadioMetadataService.provider_for("custom", url), ("soma", "lush"))
        for url in (
            "https://somafm.com.evil.example/lush130.pls",
            "https://evil-somafm.com/lush130.pls",
            "https://somafm.com@evil.example/lush130.pls",
        ):
            with self.subTest(url=url):
                self.assertIsNone(stations._extract_somafm_slug("", url))
                self.assertIsNone(stations._resolve_somafm_url(url))
                self.assertIsNone(RadioMetadataService.provider_for("custom", url))
                self.assertIsNone(stations._extract_somafm_slug("Groove Salad", url))
                self.assertIsNone(RadioMetadataService.provider_for("groovesalad", url))

    def test_somafm_playlist_resolution_accepts_real_host(self):
        response = MagicMock(ok=True, text="[playlist]\nFile1=https://ice5.somafm.com/lush-128-aac")
        with patch.object(stations, "safe_get", return_value=response):
            self.assertEqual(
                stations._resolve_somafm_url("https://api.somafm.com/lush130.pls"),
                "https://ice5.somafm.com/lush-128-aac",
            )


if __name__ == "__main__":
    unittest.main()
