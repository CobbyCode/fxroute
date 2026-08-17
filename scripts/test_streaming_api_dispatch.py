# SPDX-License-Identifier: AGPL-3.0-only

"""HTTP-level tests for the generic streaming provider API.

The provider registry's catalog/auth methods are patched with a fake provider
so the route registration, the ``type`` dispatch on favorites, the album/playlist
track endpoints, the auth endpoints and the capability-gated transport action
are exercised end-to-end through FastAPI without any network or tidalapi
dependency.
"""

from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import main as main_module
from fastapi.testclient import TestClient


class _FakeProvider:
    """Fake provider exposing every generic catalog/auth method the API uses."""

    async def search(self, query, types=None, limit=25):
        return {"tracks": [{"id": "1", "title": "T"}]}

    async def favorites(self, limit=50):
        return [{"id": "1", "title": "Track"}]

    async def favorites_albums(self, limit=50):
        return [{"id": "a1", "title": "Album"}]

    async def favorites_artists(self, limit=50):
        return [{"id": "ar1", "name": "Artist"}]

    async def playlists(self):
        return [{"id": "p1", "name": "Playlist"}]

    async def playlist_tracks(self, playlist_id):
        return [{"id": "2", "title": "PT"}]

    async def get_album_tracks(self, album_id):
        return [{"id": "3", "title": "AT"}]

    async def start_device_login(self):
        return {"user_code": "ABC", "verification_uri_complete": "https://link.tidal.com/ABC"}

    async def finish_device_login(self):
        return {"authenticated": True}

    async def pkce_login_url(self):
        return "https://login.tidal.com/authorize?..."

    async def finish_pkce_login(self, redirect_url):
        return {"authenticated": True, "is_pkce": True}

    async def logout(self):
        return None


class _FailingProvider(_FakeProvider):
    """Raises a provider-neutral error for the error-mapping test."""

    def __init__(self, exc):
        self._exc = exc

    async def search(self, query, types=None, limit=25):
        raise self._exc


class StreamingApiDispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main_module.app)

    def _patch(self, provider):
        return mock.patch.object(main_module.streaming, "get_provider", return_value=provider)

    def test_search_dispatches_to_provider(self):
        with self._patch(_FakeProvider()):
            resp = self.client.get("/api/streaming/tidal/search?q=daft&types=tracks")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["tracks"][0]["id"], "1")

    def test_favorites_defaults_to_tracks(self):
        with self._patch(_FakeProvider()):
            resp = self.client.get("/api/streaming/tidal/favorites")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()[0]["id"], "1")

    def test_favorites_albums_dispatch(self):
        with self._patch(_FakeProvider()):
            resp = self.client.get("/api/streaming/tidal/favorites?type=albums")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()[0]["id"], "a1")

    def test_favorites_artists_dispatch(self):
        with self._patch(_FakeProvider()):
            resp = self.client.get("/api/streaming/tidal/favorites?type=artists")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()[0]["name"], "Artist")

    def test_favorites_unknown_type_is_400(self):
        with self._patch(_FakeProvider()):
            resp = self.client.get("/api/streaming/tidal/favorites?type=unknown")
        self.assertEqual(resp.status_code, 400)

    def test_playlists_dispatch(self):
        with self._patch(_FakeProvider()):
            resp = self.client.get("/api/streaming/tidal/playlists")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()[0]["name"], "Playlist")

    def test_playlist_tracks_dispatch(self):
        with self._patch(_FakeProvider()):
            resp = self.client.get("/api/streaming/tidal/playlists/p1/tracks")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()[0]["title"], "PT")

    def test_album_tracks_dispatch(self):
        with self._patch(_FakeProvider()):
            resp = self.client.get("/api/streaming/tidal/albums/a1/tracks")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()[0]["title"], "AT")

    def test_auth_pkce_url(self):
        with self._patch(_FakeProvider()):
            resp = self.client.post("/api/streaming/tidal/auth/pkce")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["url"].startswith("https://login.tidal.com"))

    def test_auth_pkce_finish_requires_redirect_url(self):
        with self._patch(_FakeProvider()):
            resp = self.client.post("/api/streaming/tidal/auth/pkce/finish", json={})
        self.assertEqual(resp.status_code, 400)

    def test_auth_pkce_finish(self):
        with self._patch(_FakeProvider()):
            resp = self.client.post(
                "/api/streaming/tidal/auth/pkce/finish",
                json={"redirect_url": "https://tidal.com/android/login/auth?code=x"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["is_pkce"])

    def test_provider_auth_error_maps_to_401(self):
        import streaming.tidal.auth as tidal_auth

        failing = _FailingProvider(tidal_auth.TidalAuthError("not authenticated"))
        with self._patch(failing):
            resp = self.client.get("/api/streaming/tidal/search?q=x")
        self.assertEqual(resp.status_code, 401)


class GenericTransportActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main_module.app)

    async def _status(self):
        return {"status": "Paused"}

    def test_transport_action_dispatches_by_capability(self):
        provider = _FakeProvider()
        provider.toggle = lambda: self._status()
        with mock.patch.object(main_module.streaming, "get_provider", return_value=provider):
            resp = self.client.post("/api/streaming/qobuz/toggle")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "Paused")

    def test_unknown_action_is_404(self):
        provider = _FakeProvider()
        with mock.patch.object(main_module.streaming, "get_provider", return_value=provider):
            resp = self.client.post("/api/streaming/qobuz/bogus")
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
