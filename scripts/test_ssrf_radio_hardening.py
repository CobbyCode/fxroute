#!/usr/bin/env python3
"""SSRF hardening tests for server-side radio/playlist URL resolution.

All network I/O is mocked: ``safe_http.requests.get`` provides fake
responses and ``safe_http.socket.getaddrinfo`` provides fake DNS answers.
No real internet or LAN targets are contacted.
"""

import asyncio
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import radio_api
import safe_http
import stations


def fake_dns_public(host, port=None, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 0))]


def fake_dns_private(host, port=None, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.5", 0))]


def fake_dns_table(host, port=None, **kwargs):
    address = {"localhost": "127.0.0.1"}.get(host, "8.8.8.8")
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 0))]


def fake_dns_unresolvable(host, port=None, **kwargs):
    import socket as socket_module
    raise socket_module.gaierror("no address")


class FakeResponse:
    def __init__(self, *, status_code=200, text="", headers=None, is_redirect=False):
        self.status_code = status_code
        self.headers = headers or {}
        self.is_redirect = is_redirect
        self.ok = 200 <= status_code < 400
        self.closed = False
        self.encoding = "utf-8"
        self._content = False
        self._content_consumed = False
        self._body = text.encode("utf-8")

    def iter_content(self, chunk_size=1):
        pos = 0
        while pos < len(self._body):
            yield self._body[pos:pos + chunk_size]
            pos += chunk_size

    def close(self):
        self.closed = True

    @property
    def text(self):
        return self._body.decode("utf-8", errors="replace")

    @property
    def content(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


class ValidatePublicUrlTests(unittest.TestCase):
    def test_public_http_and_https_accepted(self):
        with patch.object(safe_http.socket, "getaddrinfo", side_effect=fake_dns_public):
            for url in (
                "http://8.8.8.8/stream",
                "https://93.184.216.34/stream.pls",
                "http://[2606:2800:220:1:248:1893:25c8:1946]/x",
                "http://[2001:4860:4860::8888]/x",
                "https://example.com/radio.pls",
                "http://stream.example:8000/radio.m3u",
            ):
                self.assertEqual(safe_http.validate_public_url(url), url)

    def test_private_and_non_routable_literals_rejected(self):
        for url in (
            "http://127.0.0.1/x",
            "http://127.8.8.8/x",
            "http://10.0.0.1/x",
            "http://10.255.255.255/x",
            "http://172.16.0.1/x",
            "http://172.31.255.255/x",
            "http://192.168.1.1/x",
            "http://169.254.169.254/x",
            "http://0.0.0.0/x",
            "http://224.0.0.1/x",
            "http://255.255.255.255/x",
            "http://192.0.2.1/x",
            "http://198.51.100.1/x",
            "http://203.0.113.1/x",
            "http://100.64.0.1/x",
            "http://[::1]/x",
            "http://[fd00::1]/x",
            "http://[fc00::1]/x",
            "http://[fe80::1]/x",
            "http://[fe80::1%25eth0]/x",
            "http://[::]/x",
            "http://[2001:db8::1]/x",
            "http://[2001::1]/x",
            "http://[2001:2::1]/x",
            "http://[64:ff9b:1::7f00:1]/x",
            "http://[ff02::1]/x",
        ):
            with self.subTest(url=url):
                with self.assertRaises(safe_http.BlockedUrlError):
                    safe_http.validate_public_url(url)

    def test_ipv4_mapped_nat64_and_6to4_embedded_private_rejected(self):
        for url in (
            "http://[::ffff:127.0.0.1]/x",
            "http://[::ffff:10.0.0.1]/x",
            "http://[::ffff:192.168.1.1]/x",
            "http://[64:ff9b::7f00:1]/x",
            "http://[2002:7f00:1::]/x",
        ):
            with self.subTest(url=url):
                with self.assertRaises(safe_http.BlockedUrlError):
                    safe_http.validate_public_url(url)

    def test_ipv4_mapped_nat64_and_6to4_embedded_public_accepted(self):
        for url in (
            "http://[::ffff:8.8.8.8]/x",
            "http://[64:ff9b::1.1.1.1]/x",
            "http://[2002:0101:0101::]/x",
        ):
            with self.subTest(url=url):
                safe_http.validate_public_url(url)

    def test_hostname_dns_pointing_to_private_rejected(self):
        with patch.object(safe_http.socket, "getaddrinfo", side_effect=fake_dns_private):
            with self.assertRaises(safe_http.BlockedUrlError):
                safe_http.validate_public_url("https://internal.example/stream.pls")

    def test_hostname_dns_pointing_public_accepted(self):
        with patch.object(safe_http.socket, "getaddrinfo", side_effect=fake_dns_public):
            safe_http.validate_public_url("https://public.example/stream.pls")

    def test_unresolvable_hostname_rejected(self):
        with patch.object(safe_http.socket, "getaddrinfo", side_effect=fake_dns_unresolvable):
            with self.assertRaises(safe_http.BlockedUrlError):
                safe_http.validate_public_url("https://does-not-exist.example/x.pls")

    def test_non_http_schemes_rejected(self):
        for url in (
            "ftp://example.com/x.pls",
            "file:///etc/passwd",
            "data:text/plain;base64,QQ==",
            "javascript:alert(1)",
            "gopher://example.com/x",
        ):
            with self.subTest(url=url):
                with self.assertRaises(safe_http.BlockedUrlError):
                    safe_http.validate_public_url(url)

    def test_userinfo_rejected(self):
        for url in (
            "http://user:pass@example.com/x.pls",
            "http://user@example.com/x.pls",
            "https://pass@example.com/x.pls",
        ):
            with self.subTest(url=url):
                with self.assertRaises(safe_http.BlockedUrlError):
                    safe_http.validate_public_url(url)

    def test_missing_host_rejected(self):
        with self.assertRaises(safe_http.BlockedUrlError):
            safe_http.validate_public_url("http:///x.pls")


class SafeGetTests(unittest.TestCase):
    def test_redirect_to_private_literal_is_blocked_before_fetch(self):
        def side_effect(url, **kwargs):
            self.assertEqual(url, "https://public.example/a.pls")
            return FakeResponse(
                is_redirect=True,
                headers={"Location": "http://127.0.0.1:9000/internal.pls"},
            )

        with patch.object(safe_http.socket, "getaddrinfo", side_effect=fake_dns_public), \
                patch("safe_http.requests.get", side_effect=side_effect) as mock_get:
            with self.assertRaises(safe_http.BlockedUrlError):
                safe_http.safe_get("https://public.example/a.pls", timeout=5, max_bytes=1024)
        mock_get.assert_called_once()

    def test_redirect_to_private_dns_target_is_blocked(self):
        def side_effect(url, **kwargs):
            if url == "https://public.example/a.pls":
                return FakeResponse(
                    is_redirect=True,
                    headers={"Location": "https://internal.example/stream.pls"},
                )
            self.fail(f"redirect target must not be fetched: {url}")

        def dns(host, port=None, **kwargs):
            if host == "public.example":
                return fake_dns_public(host, port, **kwargs)
            return fake_dns_private(host, port, **kwargs)

        with patch.object(safe_http.socket, "getaddrinfo", side_effect=dns), \
                patch("safe_http.requests.get", side_effect=side_effect) as mock_get:
            with self.assertRaises(safe_http.BlockedUrlError):
                safe_http.safe_get("https://public.example/a.pls", timeout=5, max_bytes=1024)
        mock_get.assert_called_once()

    def test_redirect_chain_to_public_target_succeeds(self):
        calls = []

        def side_effect(url, **kwargs):
            calls.append(url)
            if url == "https://public.example/a.pls":
                return FakeResponse(
                    is_redirect=True,
                    headers={"Location": "https://cdn.example/b.pls"},
                )
            return FakeResponse(text="File1=https://ice.example/stream")

        with patch.object(safe_http.socket, "getaddrinfo", side_effect=fake_dns_public), \
                patch("safe_http.requests.get", side_effect=side_effect):
            response = safe_http.safe_get("https://public.example/a.pls", timeout=5, max_bytes=1024)
        self.assertEqual(calls, ["https://public.example/a.pls", "https://cdn.example/b.pls"])
        self.assertEqual(response.text, "File1=https://ice.example/stream")

    def test_redirect_loop_hits_redirect_limit(self):
        def side_effect(url, **kwargs):
            return FakeResponse(
                is_redirect=True,
                headers={"Location": "https://cdn.example/next.pls"},
            )

        with patch.object(safe_http.socket, "getaddrinfo", side_effect=fake_dns_public), \
                patch("safe_http.requests.get", side_effect=side_effect):
            with self.assertRaises(requests.TooManyRedirects):
                safe_http.safe_get("https://public.example/a.pls", timeout=5, max_bytes=1024)


class ResolveStreamUrlTests(unittest.TestCase):
    def test_normal_pls_resolution_still_works(self):
        with patch.object(safe_http.socket, "getaddrinfo", side_effect=fake_dns_public), \
                patch("safe_http.requests.get", return_value=FakeResponse(
                    text="[playlist]\nFile1=http://stream.example/radio\nTitle1=Test",
                )) as mock_get:
            resolved = stations.resolve_stream_url("https://example.com/radio.pls")
        self.assertEqual(resolved, "http://stream.example/radio")
        self.assertEqual(mock_get.call_count, 1)

    def test_normal_somafm_resolution_still_works(self):
        responses = [
            FakeResponse(status_code=404),
            FakeResponse(status_code=404),
            FakeResponse(text="File1=https://ice4.somafm.com/groovesalad-256-mp3"),
        ]

        def side_effect(url, **kwargs):
            return responses.pop(0)

        with patch.object(safe_http.socket, "getaddrinfo", side_effect=fake_dns_public), \
                patch("safe_http.requests.get", side_effect=side_effect):
            resolved = stations.resolve_stream_url("https://somafm.com/groovesalad130.pls")
        self.assertEqual(resolved, "https://ice4.somafm.com/groovesalad-256-mp3")

    def test_normal_m3u_resolution_still_works(self):
        with patch.object(safe_http.socket, "getaddrinfo", side_effect=fake_dns_public), \
                patch("safe_http.requests.get", return_value=FakeResponse(
                    text="#EXTM3U\nhttps://stream.example/radio",
                )):
            resolved = stations.resolve_stream_url("https://example.com/playlist.m3u8")
        self.assertEqual(resolved, "https://stream.example/radio")

    def test_private_playlist_url_rejected(self):
        with patch.object(safe_http.socket, "getaddrinfo", side_effect=fake_dns_public), \
                patch("safe_http.requests.get") as mock_get:
            with self.assertRaises(safe_http.BlockedUrlError):
                stations.resolve_stream_url("http://127.0.0.1:9000/radio.pls")
        mock_get.assert_not_called()

    def test_private_dns_playlist_url_rejected(self):
        with patch.object(safe_http.socket, "getaddrinfo", side_effect=fake_dns_private), \
                patch("safe_http.requests.get") as mock_get:
            with self.assertRaises(safe_http.BlockedUrlError):
                stations.resolve_stream_url("https://internal.example/radio.pls")
        mock_get.assert_not_called()

    def test_public_url_redirecting_to_private_rejected(self):
        def side_effect(url, **kwargs):
            return FakeResponse(
                is_redirect=True,
                headers={"Location": "http://localhost:9000/stream.pls"},
            )

        with patch.object(safe_http.socket, "getaddrinfo", side_effect=fake_dns_table), \
                patch("safe_http.requests.get", side_effect=side_effect) as mock_get:
            with self.assertRaises(safe_http.BlockedUrlError):
                stations.resolve_stream_url("https://example.com/radio.pls")
        mock_get.assert_called_once()


class StationApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_config_home = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.temp_dir.name
        path = Path(self.temp_dir.name) / "fxroute" / "stations.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[]\n", encoding="utf-8")
        stations._cached_stations = None

    def tearDown(self):
        stations._cached_stations = None
        if self.previous_config_home is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self.previous_config_home
        self.temp_dir.cleanup()

    def test_create_station_with_public_playlist_url_succeeds(self):
        with patch.object(safe_http.socket, "getaddrinfo", side_effect=fake_dns_public), \
                patch("safe_http.requests.get", return_value=FakeResponse(
                    text="File1=https://stream.example/radio",
                )):
            result = asyncio.run(radio_api.create_station(
                radio_api.StationUpsertRequest(name="Test FM", stream_url="https://example.com/radio.pls")
            ))
        self.assertEqual(result["status"], "ok")
        saved = stations.get_stations()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].stream_url, "https://stream.example/radio")

    def test_create_station_with_private_playlist_url_returns_400(self):
        with patch.object(safe_http.socket, "getaddrinfo", side_effect=fake_dns_public), \
                patch("safe_http.requests.get") as mock_get:
            with self.assertRaises(radio_api.HTTPException) as ctx:
                asyncio.run(radio_api.create_station(
                    radio_api.StationUpsertRequest(name="Evil FM", stream_url="http://127.0.0.1:9000/radio.pls")
                ))
        self.assertEqual(ctx.exception.status_code, 400)
        mock_get.assert_not_called()
        self.assertEqual(stations.get_stations(), [])

    def test_update_station_with_redirect_to_private_returns_400(self):
        with patch.object(safe_http.socket, "getaddrinfo", side_effect=fake_dns_public), \
                patch("safe_http.requests.get", return_value=FakeResponse(
                    text="File1=https://stream.example/radio",
                )):
            created = asyncio.run(radio_api.create_station(
                radio_api.StationUpsertRequest(name="Test FM", stream_url="https://example.com/radio.pls")
            ))
        station_id = created["station"]["id"]

        def side_effect(url, **kwargs):
            return FakeResponse(
                is_redirect=True,
                headers={"Location": "http://192.168.1.50:8000/stream.pls"},
            )

        with patch.object(safe_http.socket, "getaddrinfo", side_effect=fake_dns_public), \
                patch("safe_http.requests.get", side_effect=side_effect):
            with self.assertRaises(radio_api.HTTPException) as ctx:
                asyncio.run(radio_api.edit_station(
                    station_id,
                    radio_api.StationUpsertRequest(name="Test FM", stream_url="https://example.com/radio.pls"),
                ))
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
