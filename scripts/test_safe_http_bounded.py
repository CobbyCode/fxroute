#!/usr/bin/env python3
"""Bounded response-body tests for the server-side fetch boundary.

``safe_get`` reads response bodies with a counted, chunked read.  These
tests verify the bounded-read semantics with real ``requests.Response``
objects whose raw stream is a fake; no network access happens.  DNS is
mocked to public addresses.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
import urllib3

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import safe_http
import stations


def fake_dns_public(host, port=None, **kwargs):
    return [(2, 1, 6, "", ("8.8.8.8", 0))]


class FakeRaw:
    def __init__(self, data=b"", exc=None):
        self.data = data
        self._pos = 0
        self.exc = exc
        self.closed = False

    def read(self, n=-1):
        if self.exc is not None:
            raise self.exc
        if n is None or n < 0:
            chunk = self.data[self._pos:]
        else:
            chunk = self.data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def stream(self, n, decode_content=True):
        while True:
            chunk = self.read(n)
            if not chunk:
                break
            yield chunk

    def close(self):
        self.closed = True


def make_response(*, status_code=200, body=b"", headers=None, raw_exc=None):
    resp = requests.Response()
    resp.status_code = status_code
    resp.headers = requests.structures.CaseInsensitiveDict(headers or {})
    resp.raw = FakeRaw(body, exc=raw_exc)
    resp._content = False
    resp._content_consumed = False
    resp.encoding = "utf-8"
    return resp


class SafeGetBoundedBodyTests(unittest.TestCase):
    def _fetch(self, responses, url="https://public.example/radio.pls", max_bytes=1024):
        with patch.object(safe_http.socket, "getaddrinfo", side_effect=fake_dns_public), \
                patch("safe_http.requests.get", side_effect=responses):
            return safe_http.safe_get(url, timeout=5, max_bytes=max_bytes)

    def test_small_playlist_body_unchanged(self):
        body = b"[playlist]\nFile1=http://stream.example/radio\n"
        response = self._fetch([make_response(body=body)], max_bytes=4096)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, body)
        self.assertEqual(response.text, body.decode("utf-8"))
        self.assertTrue(response.ok)

    def test_body_exactly_at_limit_allowed(self):
        body = b"x" * 100
        response = self._fetch([make_response(body=body)], max_bytes=100)
        self.assertEqual(response.content, body)

    def test_content_length_fast_reject_before_reading(self):
        response = make_response(body=b"small", headers={"content-length": "10000"})
        with self.assertRaises(safe_http.ResponseTooLargeError) as ctx:
            self._fetch([response], max_bytes=100)
        self.assertIn("Content-Length", str(ctx.exception))
        self.assertEqual(response.raw._pos, 0, "body must not be read on fast reject")
        self.assertTrue(response.raw.closed, "response must be closed")

    def test_lying_content_length_does_not_bypass_counted_read(self):
        response = make_response(
            body=b"y" * 200_000,
            headers={"content-length": "50"},
        )
        with self.assertRaises(safe_http.ResponseTooLargeError) as ctx:
            self._fetch([response], max_bytes=100)
        self.assertIn("exceeded", str(ctx.exception))
        self.assertLess(response.raw._pos, len(response.raw.data), "read aborted mid-body")
        self.assertTrue(response.raw.closed)

    def test_missing_and_garbage_content_length_decided_by_counted_read(self):
        for headers in ({}, {"content-length": "not-a-number"}):
            with self.subTest(headers=headers):
                response = make_response(body=b"z" * 5000, headers=headers)
                with self.assertRaises(safe_http.ResponseTooLargeError):
                    self._fetch([response], max_bytes=100)
                self.assertTrue(response.raw.closed)

    def test_redirect_to_legit_target_reads_final_body(self):
        first = make_response(
            status_code=302,
            headers={"Location": "https://cdn.example/b.pls"},
        )
        final = make_response(body=b"File1=http://stream.example/radio")
        response = self._fetch([first, final], max_bytes=4096)
        self.assertEqual(response.text, "File1=http://stream.example/radio")
        self.assertTrue(first.raw.closed, "redirect response must be closed without body read")
        self.assertEqual(first.raw._pos, 0)

    def test_redirect_to_private_ip_still_blocked_by_ssrf(self):
        first = make_response(
            status_code=302,
            headers={"Location": "http://127.0.0.1:9000/internal.pls"},
        )
        with patch.object(safe_http.socket, "getaddrinfo", side_effect=fake_dns_public), \
                patch("safe_http.requests.get", side_effect=[first]) as mock_get:
            with self.assertRaises(safe_http.BlockedUrlError):
                safe_http.safe_get("https://public.example/a.pls", timeout=5, max_bytes=1024)
        mock_get.assert_called_once()
        self.assertTrue(first.raw.closed)

    def test_read_timeout_closes_response_and_stays_requests_semantics(self):
        # Body-read timeouts surface as a requests exception (ReadTimeout on
        # the pinned requests 2.31, ConnectionError on newer requests); both
        # are RequestException, and the response must be closed.
        response = make_response(
            body=b"x" * 100,
            raw_exc=urllib3.exceptions.ReadTimeoutError("pool", "host", "read timed out"),
        )
        with self.assertRaises(requests.exceptions.RequestException):
            self._fetch([response], max_bytes=1024)
        self.assertTrue(response.raw.closed)

    def test_oversized_artwork_rejected_and_response_closed(self):
        response = make_response(
            body=b"PNG" * 10000,
            headers={"content-type": "image/png"},
        )
        with self.assertRaises(safe_http.ResponseTooLargeError):
            self._fetch([response], max_bytes=100)
        self.assertTrue(response.raw.closed)


class StationResolutionLimitTests(unittest.TestCase):
    def test_oversized_playlist_is_controlled_resolution_error(self):
        response = make_response(body=b"x" * 5000)
        with patch.object(safe_http.socket, "getaddrinfo", side_effect=fake_dns_public), \
                patch.object(stations, "RADIO_PLAYLIST_FETCH_MAX_BYTES", 1024), \
                patch("safe_http.requests.get", return_value=response):
            with self.assertRaises(safe_http.ResponseTooLargeError) as ctx:
                stations.resolve_stream_url("https://example.com/radio.pls")
        self.assertIn("exceeded", str(ctx.exception))
        self.assertTrue(response.raw.closed)

    def test_timeout_during_playlist_body_is_controlled_error(self):
        response = make_response(raw_exc=urllib3.exceptions.ReadTimeoutError("p", "h", "t"))
        with patch.object(safe_http.socket, "getaddrinfo", side_effect=fake_dns_public), \
                patch("safe_http.requests.get", return_value=response):
            with self.assertRaises(ValueError) as ctx:
                stations.resolve_stream_url("https://example.com/radio.pls")
        self.assertIn("Could not fetch", str(ctx.exception))
        self.assertTrue(response.raw.closed)

    def test_small_playlist_resolution_unchanged(self):
        response = make_response(body=b"[playlist]\nFile1=http://stream.example/radio\n")
        with patch.object(safe_http.socket, "getaddrinfo", side_effect=fake_dns_public), \
                patch("safe_http.requests.get", return_value=response):
            resolved = stations.resolve_stream_url("https://example.com/radio.pls")
        self.assertEqual(resolved, "http://stream.example/radio")


class SomafmArtworkLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.art_dir = Path(self.temp_dir.name) / "station-art"
        self.art_dir.mkdir()
        self.original_art_dir = stations.STATION_ART_DIR
        stations.STATION_ART_DIR = self.art_dir

    def tearDown(self):
        stations.STATION_ART_DIR = self.original_art_dir
        self.temp_dir.cleanup()

    def _page_response(self, image_url):
        html = f'<meta property="og:image" content="{image_url}">'
        return make_response(body=html.encode("utf-8"))

    def test_oversized_artwork_leaves_no_partial_state(self):
        page = self._page_response("https://somafm.com/logos/testslug.png")
        image = make_response(
            body=b"PNG" * 5000,
            headers={"content-type": "image/png"},
        )
        with patch.object(safe_http.socket, "getaddrinfo", side_effect=fake_dns_public), \
                patch.object(stations, "SOMAFM_ARTWORK_FETCH_MAX_BYTES", 1024), \
                patch("safe_http.requests.get", side_effect=[page, image]):
            result = stations._download_somafm_art("testslug")
        self.assertIsNone(result)
        self.assertEqual(list(self.art_dir.iterdir()), [], "no partial artwork may remain")
        self.assertTrue(image.raw.closed)

    def test_small_artwork_still_saved(self):
        page = self._page_response("https://somafm.com/logos/testslug.png")
        image = make_response(
            body=b"\x89PNG-test",
            headers={"content-type": "image/png"},
        )
        with patch.object(safe_http.socket, "getaddrinfo", side_effect=fake_dns_public), \
                patch("safe_http.requests.get", side_effect=[page, image]):
            result = stations._download_somafm_art("testslug")
        self.assertIsNotNone(result)
        saved = self.art_dir / "testslug.png"
        self.assertTrue(saved.is_file())
        self.assertEqual(saved.read_bytes(), b"\x89PNG-test")


if __name__ == "__main__":
    unittest.main()
