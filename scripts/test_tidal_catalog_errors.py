#!/usr/bin/env python3
"""Catalog -> worker -> HTTP error contracts without TIDAL/network access."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import streaming
from streaming import api
from streaming.tidal import auth, catalog
from streaming.tidal.cache import TidalLibraryCache
from streaming.tidal.provider import TidalProvider


# Same public exception contracts as tidalapi.exceptions; no optional install
# is needed to test the provider's classification boundary.
class AuthenticationError(Exception):
    pass


class ObjectNotFound(Exception):
    pass


class TooManyRequests(Exception):
    pass


def http_error(status):
    response = requests.Response()
    response.status_code = status
    response.url = "https://tidal.invalid/catalog"
    return requests.HTTPError(f"upstream status {status}", response=response)


class TidalCatalogErrorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = TidalLibraryCache(Path(temporary.name) / "cache.sqlite")
        self.failure = ConnectionError("network unavailable")
        self.calls = []

        def fail(*args, **kwargs):
            self.calls.append((args, kwargs))
            raise self.failure

        self.favorites = SimpleNamespace(**{
            name: fail for name in (
                "tracks", "albums", "artists", "playlists", "add_track", "remove_track",
                "add_album", "remove_album", "add_artist", "remove_artist",
                "add_playlist", "remove_playlist",
            )
        })
        self.session = SimpleNamespace(
            search=fail, track=fail, album=fail, artist=fail, playlist=fail,
            user=SimpleNamespace(id=42, favorites=self.favorites, create_playlist=fail),
            access_token="retained-access", refresh_token="retained-refresh",
        )
        self.manager = auth.TidalSession()
        self.manager._session = self.session
        self.manager._last_user = {"id": "42", "name": "Known user"}
        module = SimpleNamespace(
            Track=object, Album=object, Artist=object, Playlist=object,
            exceptions=SimpleNamespace(AuthenticationError=AuthenticationError,
                                       ObjectNotFound=ObjectNotFound, TooManyRequests=TooManyRequests),
        )
        for patch in (
            mock.patch.object(auth, "tidalapi", module),
            mock.patch.dict(sys.modules, {"tidalapi": module}),
            mock.patch.object(auth, "manager", self.manager),
            mock.patch.object(catalog, "library_cache", self.store),
            mock.patch.object(streaming, "get_provider", return_value=TidalProvider()),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        app = FastAPI()
        app.include_router(api.router)
        # No main import, lifespan, provider probes, audio engine or services.
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_search_transport_and_server_errors_are_502_not_401(self):
        for error in (ConnectionError("offline"), TimeoutError("timeout"),
                      requests.ConnectionError("dns"), requests.Timeout("timeout"),
                      http_error(500), http_error(503),
                      json.JSONDecodeError("invalid JSON", "?", 0)):
            with self.subTest(error=repr(error)):
                self.failure = error
                response = self.client.get("/api/streaming/tidal/search?q=x")
                self.assertEqual(response.status_code, 502, response.text)

    def test_upstream_client_statuses_are_not_login_errors(self):
        for status in (400, 403, 404, 422, 429):
            with self.subTest(status=status):
                self.failure = http_error(status)
                response = self.client.get("/api/streaming/tidal/search?q=x")
                self.assertEqual(response.status_code, status, response.text)

    def test_explicit_auth_rejection_remains_401(self):
        for error in (AuthenticationError("invalid_grant"), http_error(401),
                      auth.TidalAuthError("not authenticated")):
            with self.subTest(error=repr(error)):
                self.failure = error
                response = self.client.get("/api/streaming/tidal/search?q=x")
                self.assertEqual(response.status_code, 401, response.text)

    def test_tidalapi_not_found_and_rate_limit_remain_non_auth_errors(self):
        for error, status in ((ObjectNotFound("missing"), 404), (TooManyRequests("slow down"), 429)):
            with self.subTest(status=status):
                self.failure = error
                response = self.client.get("/api/streaming/tidal/search?q=x")
                self.assertEqual(response.status_code, status, response.text)

    def test_missing_session_remains_401(self):
        with mock.patch.object(self.manager, "session", return_value=None):
            response = self.client.get("/api/streaming/tidal/search?q=x")
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(self.calls, [])

    def test_negative_or_zero_search_limit_is_400_without_upstream_io(self):
        for limit in (-5, 0):
            with self.subTest(limit=limit):
                response = self.client.get(f"/api/streaming/tidal/search?q=x&limit={limit}")
                self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.calls, [])

    def test_non_search_catalog_failures_are_502(self):
        routes = (
            ("GET", "/favorites", None), ("GET", "/favorites?type=albums", None),
            ("GET", "/favorites?type=artists", None), ("GET", "/favorites/ids", None),
            ("GET", "/albums/1", None), ("GET", "/albums/1/tracks", None),
            ("GET", "/artists/1", None), ("GET", "/playlists", None),
            ("GET", "/playlists/p1", None), ("GET", "/playlists/p1/tracks", None),
            ("POST", "/tracks/1/favorite", {"favorite": True}),
            ("POST", "/albums/1/favorite", {"favorite": True}),
            ("POST", "/artists/1/favorite", {"favorite": True}),
            ("POST", "/playlists/p1/favorite", {"favorite": True}),
            ("POST", "/playlists/create", {"name": "Mix"}),
            ("POST", "/playlists/p1/tracks", {"track_ids": ["1"]}),
        )
        for method, route, body in routes:
            with self.subTest(route=route):
                response = self.client.request(method, "/api/streaming/tidal" + route, json=body)
                self.assertEqual(response.status_code, 502, response.text)

    def test_track_lookup_uses_same_non_auth_error_mapping(self):
        try:
            catalog.get_track("1")
        except Exception as error:
            self.assertEqual(api._tidal_http_error(error).status_code, 502)
            self.assertIs(error.__cause__, self.failure)
        else:
            self.fail("failed lookup must raise")

    def test_unsuccessful_favorite_write_is_not_an_auth_rejection(self):
        for kind in ("track", "album", "artist", "playlist"):
            with self.subTest(kind=kind):
                setattr(self.favorites, "add_" + kind, lambda *args: False)
                plural = {"track": "tracks", "album": "albums", "artist": "artists",
                          "playlist": "playlists"}[kind]
                response = self.client.post(f"/api/streaming/tidal/{plural}/1/favorite",
                                            json={"favorite": True})
                self.assertEqual(response.status_code, 502, response.text)

    def test_empty_playlist_add_is_input_error(self):
        try:
            catalog.add_playlist_tracks("p1", [])
        except Exception as error:
            self.assertEqual(api._tidal_http_error(error).status_code, 400)
        else:
            self.fail("empty add must fail")
        self.assertEqual(self.calls, [])

    def test_cached_favorites_and_ids_survive_upstream_failures(self):
        fixtures = (
            ("tracks", "/favorites", [{"id": "old-track"}]),
            ("albums", "/favorites?type=albums", [{"id": "old-album"}]),
            ("artists", "/favorites?type=artists", [{"id": "old-artist"}]),
            ("playlists", "/playlists", [{"id": "old-playlist"}]),
            ("ids", "/favorites/ids", {"tracks": ["old-track"], "albums": [],
                                       "artists": [], "playlists": []}),
        )
        for kind, route, payload in fixtures:
            self.store.put("42", kind, payload)
            with self.subTest(kind=kind):
                response = self.client.get("/api/streaming/tidal" + route)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), payload)
                self.assertEqual(self.store.get("42", kind), payload)

    def test_catalog_failure_does_not_invalidate_session_or_tokens(self):
        self.client.get("/api/streaming/tidal/search?q=x")
        self.assertIs(self.manager.session(), self.session)
        self.assertEqual(self.session.access_token, "retained-access")
        self.assertEqual(self.session.refresh_token, "retained-refresh")
        self.assertEqual(self.manager._last_user, {"id": "42", "name": "Known user"})

    def test_nonpositive_favorites_limit_still_means_full_collection(self):
        self.favorites.tracks_paginated = lambda: []
        for limit in (0, -5):
            with self.subTest(limit=limit):
                response = self.client.get(f"/api/streaming/tidal/favorites?limit={limit}")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), [])


if __name__ == "__main__":
    unittest.main()
