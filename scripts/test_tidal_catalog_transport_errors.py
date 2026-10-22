#!/usr/bin/env python3
"""Real tidalapi request/refresh contracts; only the HTTP adapter is replaced."""

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

from streaming import api
import streaming
from streaming.tidal import auth, catalog
from streaming.tidal.cache import TidalLibraryCache
from streaming.tidal.provider import TidalProvider


@unittest.skipUnless(auth.tidalapi_available(), "optional tidalapi dependency not installed")
class TidalCatalogTransportErrorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.session = auth._new_session()
        self.addCleanup(self.session.request_session.close)
        self.session.access_token = "retained-access"
        self.session.refresh_token = "retained-refresh"
        self.session.token_type = "Bearer"
        self.session.country_code = "US"
        self.session.session_id = "local-session"
        self.session.user = SimpleNamespace(
            id=42, favorites=auth.tidalapi.Favorites(self.session, 42),
        )
        self.manager = auth.TidalSession()
        self.manager._session = self.session
        self.manager._last_user = {"id": "42", "name": "Known user"}
        self.store = TidalLibraryCache(Path(temporary.name) / "cache.sqlite")
        self.upstream = (401, {"userMessage": "The token has expired."})
        self.refresh = (503, {"error": "server_error", "error_description": "Unavailable"})
        self.requests = []
        for patch in (
            mock.patch.object(auth, "manager", self.manager),
            mock.patch.object(catalog, "library_cache", self.store),
            mock.patch.object(streaming, "get_provider", return_value=TidalProvider()),
            # Real Session.request/send, response hooks, Requests.request and
            # Session.token_refresh all run. No request can reach a real socket.
            mock.patch.object(requests.adapters.HTTPAdapter, "send", side_effect=self._transport),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        app = FastAPI()
        app.include_router(api.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def _transport(self, request, **kwargs):
        is_refresh = request.url == self.session.config.api_oauth2_token
        self.requests.append("refresh" if is_refresh else "catalog")
        status, payload = self.refresh if is_refresh else self.upstream
        response = requests.Response()
        response.status_code = status
        response.request = request
        response.url = request.url
        response._content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return response

    def _request(self, expected, route="/search?q=x"):
        response = self.client.get("/api/streaming/tidal" + route)
        self.assertEqual(response.status_code, expected, response.text)
        self.assertIs(self.manager.session(), self.session)
        self.assertEqual(self.session.access_token, "retained-access")
        self.assertEqual(self.session.refresh_token, "retained-refresh")
        self.assertEqual(self.manager._last_user, {"id": "42", "name": "Known user"})
        return response

    def test_refresh_5xx_does_not_masquerade_as_rejected_credentials(self):
        for status in (500, 503):
            for error in ("server_error", "invalid_grant"):
                with self.subTest(status=status, error=error):
                    self.refresh = (status, {"error": error, "error_description": "Unavailable"})
                    self.requests.clear()
                    self._request(502)
                    self.assertEqual(self.requests, ["catalog", "refresh"])

    def test_refresh_rate_limit_is_not_auth_failure(self):
        self.refresh = (429, {"error": "slow_down", "error_description": "Try later"})
        self._request(429)
        self.assertEqual(self.requests, ["catalog", "refresh"])

    def test_rejected_refresh_token_remains_401(self):
        self.refresh = (400, {"error": "invalid_grant", "error_description": "Revoked"})
        self._request(401)
        self.assertEqual(self.requests, ["catalog", "refresh"])

    def test_malformed_error_body_preserves_known_http_status(self):
        for status in (400, 401, 403, 404, 422, 429, 500, 503):
            with self.subTest(status=status):
                self.upstream = (status, b"<html>error</html>")
                self._request(status if status < 500 else 502)

    def test_malformed_success_body_is_upstream_failure(self):
        self.upstream = (200, b"<html>invalid JSON</html>")
        self._request(502)

    def test_cached_favorites_survive_refresh_5xx(self):
        payload = [{"id": "cached-track"}]
        self.store.put("42", "tracks", payload)
        response = self._request(200, "/favorites")
        self.assertEqual(response.json(), payload)
        self.assertEqual(self.store.get("42", "tracks"), payload)
        self.assertEqual(self.requests, ["catalog", "refresh"])

    def test_json_http_errors_keep_their_status(self):
        for status in (400, 401, 403, 404, 422, 429, 500, 503):
            with self.subTest(status=status):
                self.upstream = (status, {"userMessage": "Request rejected"})
                self._request(status if status < 500 else 502)


if __name__ == "__main__":
    unittest.main()
