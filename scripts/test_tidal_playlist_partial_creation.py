# SPDX-License-Identifier: AGPL-3.0-only
"""Confirmed playlist creation survives an uncertain items-write response."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient
import streaming
from streaming import api
from streaming.tidal import auth, catalog
from streaming.tidal.cache import TidalLibraryCache
from streaming.tidal.provider import TidalProvider


class UpstreamSession:
    """Stateful external boundary: the playlist/items may exist before JSON fails."""
    def __init__(self):
        self.created = {}
        self.favorites = set()
        self.writes = []
        self.payload = {"addedItemIds": ["1", "2"]}
        self.failure = None
        self.create_failure = None
        self.favorite_failure = None
        self.no_response = False
        self.user = SimpleNamespace(id=42, create_playlist=self.create,
                                    favorites=SimpleNamespace(add_playlist=self.favorite))
        self.request = SimpleNamespace(request=self.add)

    def create(self, title, description):
        if self.create_failure:
            raise self.create_failure
        playlist = SimpleNamespace(id=f"pl-{len(self.created) + 1}", name=title,
                                   description=description, num_tracks=0, _etag='"etag-1"')
        self.created[playlist.id] = {"playlist": playlist, "tracks": []}
        return playlist

    def playlist(self, playlist_id):
        return self.created[playlist_id]["playlist"]

    def favorite(self, playlist_id):
        if self.favorite_failure:
            raise self.favorite_failure
        self.favorites.add(playlist_id)

    def add(self, method, path, data, headers):
        self.writes.append((method, path, data, headers))
        playlist_id = path.split('/')[1]
        tracks = self.created[playlist_id]["tracks"]
        for track_id in data["trackIds"].split(','):
            if track_id not in tracks:
                tracks.append(track_id)
        if self.failure:
            raise self.failure
        if self.no_response:
            return None

        def json():
            if isinstance(self.payload, Exception):
                raise self.payload
            return self.payload

        return SimpleNamespace(json=json)


class PlaylistPartialCreationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cache = TidalLibraryCache(db_path=Path(temporary.name) / "cache.sqlite")
        self.session = UpstreamSession()
        for patcher in (
            mock.patch.object(auth, "tidalapi_available", return_value=True),
            mock.patch.object(auth, "manager", SimpleNamespace(session=lambda: self.session, last_user_id=lambda: "42")),
            mock.patch.object(catalog, "library_cache", self.cache),
            mock.patch.object(streaming, "get_provider", return_value=TidalProvider()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(api.router)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.cache.put("42", "playlists", [{"id": "old"}])
        self.cache.put("42", "ids", {"playlists": ["old"]})

    def create(self, track_ids=None):
        return self.client.post("/api/streaming/tidal/playlists/create", json={
            "name": " Mix ", "description": "Description",
            "track_ids": ["1", "2"] if track_ids is None else track_ids,
        })

    def assert_created_with_warning(self, response, status="unknown", added=None):
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertIn(payload["id"], self.session.created)
        self.assertEqual(payload["name"], "Mix")
        self.assertEqual(payload.get("seed_status"), status)
        self.assertIn("added_track_ids", payload)
        self.assertEqual(payload["added_track_ids"], added)
        self.assertTrue(payload.get("warning"), "partial success must not look like a complete save")
        self.assertIn(payload["id"], payload["warning"])
        self.assertIn(payload["id"], self.session.favorites)
        self.assertIn(payload["id"], self.cache.get("42", "ids")["playlists"])
        self.assertIsNone(self.cache.get("42", "playlists"))
        return payload

    def test_invalid_response_shapes_preserve_created_identity(self):
        for body in ([], None, "text", 1, True, {}, {"addedItemIds": None},
                     {"addedItemIds": "1"}, {"addedItemIds": {}},
                     {"addedItemIds": [None]}, {"addedItemIds": [True]},
                     {"addedItemIds": [{}]}, {"addedItemIds": [""]}):
            with self.subTest(body=body):
                self.session.payload = body
                before = len(self.session.created)
                payload = self.assert_created_with_warning(self.create())
                self.assertEqual(len(self.session.created), before + 1, "must not replay creation internally")
                self.assertEqual(self.session.created[payload["id"]]["tracks"], ["1", "2"])

    def test_invalid_json_preserves_created_identity(self):
        self.session.payload = ValueError("invalid JSON")
        self.assert_created_with_warning(self.create())

    def test_missing_response_is_unknown_not_empty_success(self):
        self.session.no_response = True
        self.assert_created_with_warning(self.create())

    def test_network_failure_after_creation_preserves_identity(self):
        self.session.failure = ConnectionError("connection lost after write")
        self.assert_created_with_warning(self.create())

    def test_auth_failure_while_seeding_does_not_hide_created_playlist(self):
        self.session.failure = auth.TidalAuthError("expired while adding")
        self.assert_created_with_warning(self.create())

    def test_partial_and_empty_confirmation_are_explicit(self):
        for added in ([], ["1"]):
            with self.subTest(added=added):
                self.session.payload = {"addedItemIds": added}
                self.assert_created_with_warning(self.create(), status="partial", added=added)

    def test_valid_confirmation_and_etag_keep_normal_success(self):
        self.session.payload = {"addedItemIds": [1, "2"]}
        response = self.create()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json().get("warning"))
        self.assertEqual(response.json().get("seed_status"), "complete")
        self.assertEqual(response.json()["added_track_ids"], ["1", "2"])
        self.assertEqual(self.session.writes, [("POST", "playlists/pl-1/items",
                         {"onArtifactNotFound": "SKIP", "onDupes": "SKIP", "trackIds": "1,2"},
                         {"If-None-Match": '"etag-1"'})])

    def test_empty_playlist_does_not_send_items_request(self):
        response = self.create([])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.session.writes, [])
        self.assertFalse(response.json().get("warning"))

    def test_recovery_adds_to_existing_playlist_without_duplicate_creation(self):
        self.session.payload = []
        payload = self.assert_created_with_warning(self.create())
        self.session.payload = {"addedItemIds": []}  # onDupes=SKIP: already present
        response = self.client.post(f'/api/streaming/tidal/playlists/{payload["id"]}/tracks',
                                    json={"track_ids": ["1", "2"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.session.created), 1)
        self.assertEqual(self.session.created[payload["id"]]["tracks"], ["1", "2"])

    def test_creation_failure_keeps_original_error_and_cache(self):
        self.session.create_failure = ConnectionError("creation unavailable")
        response = self.create()
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(self.session.created, {})
        self.assertEqual(self.session.writes, [])
        self.assertEqual(self.cache.get("42", "playlists"), [{"id": "old"}])

    def test_favorite_remains_best_effort_after_uncertain_seed(self):
        self.session.payload = []
        self.session.favorite_failure = ConnectionError("favorite unavailable")
        response = self.create()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(response.json()["id"], self.session.created)
        self.assertTrue(response.json().get("warning"))
        self.assertIsNone(self.cache.get("42", "playlists"))

    def test_existing_playlist_add_rejects_invalid_confirmation(self):
        self.session.create("Existing", "")
        for body in ([], None, {}, {"addedItemIds": "1"}):
            with self.subTest(body=body):
                self.session.payload = body
                response = self.client.post('/api/streaming/tidal/playlists/pl-1/tracks',
                                            json={"track_ids": ["1"]})
                self.assertEqual(response.status_code, 502, response.text)
                self.assertIn("detail", response.json())


if __name__ == "__main__":
    unittest.main()
