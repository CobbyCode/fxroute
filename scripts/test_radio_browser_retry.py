#!/usr/bin/env python3
"""Tests for the radio-browser retry/mirror-fallback logic in radio_api.py."""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import radio_api


class _FakeResponse:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, str):
            return json.loads(self._payload)
        return self._payload


class RadioBrowserRequestTests(unittest.TestCase):
    def test_first_mirror_success(self):
        with mock.patch.object(
            radio_api.requests, "get", return_value=_FakeResponse(200, [{"name": "x"}])
        ) as get:
            result = radio_api._radio_browser_request("/json/stations/search", {"name": "fip"})
        self.assertEqual(result, [{"name": "x"}])
        self.assertEqual(get.call_count, 1)

    def test_fallback_to_next_mirror_on_503(self):
        calls = {"n": 0}

        def fake_get(url, *a, **kw):
            calls["n"] += 1
            if "de1.api" in url:
                return _FakeResponse(503)
            return _FakeResponse(200, [{"name": "y"}])

        with mock.patch.object(radio_api.requests, "get", side_effect=fake_get):
            result = radio_api._radio_browser_request("/json/stations/search", {"name": "fip"})
        self.assertEqual(result, [{"name": "y"}])

    def test_retry_round_after_all_mirrors_fail(self):
        calls = {"n": 0}

        def fake_get(url, *a, **kw):
            calls["n"] += 1
            if calls["n"] <= 3:  # first round: all mirrors 503
                return _FakeResponse(503)
            return _FakeResponse(200, [{"name": "z"}])  # retry round: de1 ok

        with mock.patch.object(radio_api.requests, "get", side_effect=fake_get):
            result = radio_api._radio_browser_request("/json/stations/search", {"name": "fip"})
        self.assertEqual(result, [{"name": "z"}])

    def test_total_outage_raises_502(self):
        with mock.patch.object(radio_api.requests, "get", return_value=_FakeResponse(503)):
            with self.assertRaises(radio_api.HTTPException) as ctx:
                radio_api._radio_browser_request("/json/stations/search", {"name": "fip"})
        self.assertEqual(ctx.exception.status_code, 502)

    def test_timeout_raises_504(self):
        def fake_get(url, *a, **kw):
            raise radio_api.requests.Timeout("timed out")

        with mock.patch.object(radio_api.requests, "get", side_effect=fake_get):
            with self.assertRaises(radio_api.HTTPException) as ctx:
                radio_api._radio_browser_request("/json/stations/search", {"name": "fip"})
        self.assertEqual(ctx.exception.status_code, 504)

    def test_non_retryable_http_fails_fast(self):
        with mock.patch.object(
            radio_api.requests, "get", return_value=_FakeResponse(400)
        ) as get:
            with self.assertRaises(radio_api.HTTPException) as ctx:
                radio_api._radio_browser_request("/json/stations/search", {"name": "fip"})
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
