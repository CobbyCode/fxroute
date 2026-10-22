# SPDX-License-Identifier: AGPL-3.0-only
"""Invalid favorite requests must not turn into destructive provider calls."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient
import streaming
from streaming import api
from streaming.tidal import auth, catalog


class FavoriteProvider:
    """Stateful boundary double: writes change favorites, not just a call count."""

    def __init__(self):
        self.ids = {kind: {"keep"} for kind in ("tracks", "albums", "artists", "playlists")}
        self.writes = []
        self.failure = None

    def _set(self, kind, item_id, favorite):
        self.writes.append((kind, item_id, favorite))
        if self.failure is not None:
            raise self.failure
        if favorite:
            self.ids[kind].add(item_id)
        else:
            self.ids[kind].discard(item_id)
        return {"id": item_id, "favorite": favorite}

    async def set_track_favorite(self, item_id, favorite):
        return self._set("tracks", item_id, favorite)

    async def set_album_favorite(self, item_id, favorite):
        return self._set("albums", item_id, favorite)

    async def set_artist_favorite(self, item_id, favorite):
        return self._set("artists", item_id, favorite)

    async def set_playlist_favorite(self, item_id, favorite):
        return self._set("playlists", item_id, favorite)


class FavoriteValidationTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(api.router)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.provider = FavoriteProvider()
        self.get_provider = mock.Mock(return_value=self.provider)
        self.lookup = mock.patch.object(streaming, "get_provider", new=self.get_provider)
        self.lookup.start()
        self.addCleanup(self.lookup.stop)

    def _reject_without_writes(self, payloads):
        for kind in self.provider.ids:
            for payload in payloads:
                with self.subTest(kind=kind, payload=payload):
                    lookups = self.get_provider.call_count
                    response = self.client.post(f"/api/streaming/tidal/{kind}/keep/favorite",
                                                content=payload, headers={"Content-Type": "application/json"})
                    self.assertEqual(response.status_code, 400,
                                     f"{response.text}; writes={self.provider.writes}; ids={self.provider.ids}")
                    self.assertIsInstance(response.json()["detail"], str)
                    self.assertEqual(self.get_provider.call_count, lookups,
                                     "invalid bodies must be rejected before provider lookup")
                    self.assertEqual(self.provider.writes, [])
                    self.assertEqual(self.provider.ids, {key: {"keep"} for key in self.provider.ids})

    def test_absent_empty_and_malformed_json_cannot_remove_favorites(self):
        self._reject_without_writes((None, b"", b"{broken", b'{"favorite":'))

    def test_nonobject_json_cannot_mutate_favorites(self):
        self._reject_without_writes((b"null", b"[]", b'"false"', b"123", b"false"))

    def test_missing_favorite_is_not_an_implicit_remove(self):
        self._reject_without_writes((b"{}", b'{"unrelated":false}'))

    def test_favorite_requires_a_boolean_without_coercion(self):
        self._reject_without_writes((b'{"favorite":null}', b'{"favorite":0}', b'{"favorite":1}',
                                     b'{"favorite":"false"}', b'{"favorite":"true"}',
                                     b'{"favorite":""}', b'{"favorite":[]}', b'{"favorite":{}}'))

    def test_explicit_true_and_false_keep_exact_mutation_intent(self):
        for kind in self.provider.ids:
            for favorite in (False, True):
                with self.subTest(kind=kind, favorite=favorite):
                    response = self.client.post(f"/api/streaming/tidal/{kind}/keep/favorite",
                                                json={"favorite": favorite})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertIs(response.json()["favorite"], favorite)
                    self.assertEqual(self.provider.writes[-1], (kind, "keep", favorite))
                    self.assertIs(type(self.provider.writes[-1][2]), bool)
                    self.assertEqual("keep" in self.provider.ids[kind], favorite)

    def test_provider_lookup_and_unsupported_method_contracts_are_preserved(self):
        for kind in self.provider.ids:
            for provider, status in ((None, 404), (object(), 501)):
                with self.subTest(kind=kind, status=status), \
                        mock.patch.object(streaming, "get_provider", return_value=provider):
                    response = self.client.post(f"/api/streaming/unknown/{kind}/keep/favorite",
                                                json={"favorite": False})
                    self.assertEqual(response.status_code, status)

    def test_invalid_body_wins_before_unknown_provider_lookup(self):
        for kind in self.provider.ids:
            for payload in (b"", b"{broken"):
                with self.subTest(kind=kind, payload=payload):
                    self.get_provider.reset_mock()
                    response = self.client.post(f"/api/streaming/unknown/{kind}/keep/favorite",
                                                content=payload,
                                                headers={"Content-Type": "application/json"})
                    self.assertEqual(response.status_code, 400, response.text)
                    self.get_provider.assert_not_called()

    def test_valid_request_preserves_provider_error_mapping(self):
        for kind in self.provider.ids:
            for failure, status in ((auth.TidalAuthError("Login required"), 401),
                                    (catalog.TidalCatalogError("Unavailable"), 502),
                                    (catalog.TidalCatalogError("Rate limited", status_code=429), 429)):
                with self.subTest(kind=kind, status=status):
                    self.provider.failure = failure
                    response = self.client.post(f"/api/streaming/tidal/{kind}/keep/favorite",
                                                json={"favorite": False})
                    self.assertEqual(response.status_code, status, response.text)
                    self.assertIn("keep", self.provider.ids[kind])


if __name__ == "__main__":
    unittest.main()
