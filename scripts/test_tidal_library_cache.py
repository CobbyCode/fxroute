# SPDX-License-Identifier: AGPL-3.0-only

"""Focused tests for the TIDAL library/browse cache.

Covers the SQLite store (per-account keying, snapshot shape, corruption
tolerance), the catalog wrappers (write on success, serve last-known state on
failure, favorite-toggle id updates), the cache-only library snapshot API
endpoint, and the outage-resilient auth check (last-known user state across
transient TIDAL failures, persisted across session reloads).
"""

import asyncio
import json
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import main as main_module
from streaming.tidal import auth, catalog
from streaming.tidal.cache import TidalLibraryCache
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class FakeFavorites:
    """Minimal ``session.user.favorites`` with paging + add/remove recording."""

    def __init__(self, tracks=None, albums=None, artists=None, playlists=None):
        self._tracks = list(tracks or [])
        self._albums = list(albums or [])
        self._artists = list(artists or [])
        self._playlists = list(playlists or [])
        self._fail = False

    def _page(self, items, limit, offset):
        return items[offset:offset + limit]

    def tracks(self, limit=50, offset=0, **kw):
        if self._fail:
            raise ConnectionError("tidal unreachable")
        return self._page(self._tracks, limit, offset)

    def albums(self, limit=50, offset=0, **kw):
        if self._fail:
            raise ConnectionError("tidal unreachable")
        return self._page(self._albums, limit, offset)

    def artists(self, limit=50, offset=0, **kw):
        if self._fail:
            raise ConnectionError("tidal unreachable")
        return self._page(self._artists, limit, offset)

    def playlists(self, limit=50, offset=0, **kw):
        if self._fail:
            raise ConnectionError("tidal unreachable")
        return self._page(self._playlists, limit, offset)

    def tracks_paginated(self, **kw):
        if self._fail:
            raise ConnectionError("tidal unreachable")
        return list(self._tracks)

    def albums_paginated(self, **kw):
        if self._fail:
            raise ConnectionError("tidal unreachable")
        return list(self._albums)

    def artists_paginated(self, **kw):
        if self._fail:
            raise ConnectionError("tidal unreachable")
        return list(self._artists)

    def playlists_paginated(self, **kw):
        if self._fail:
            raise ConnectionError("tidal unreachable")
        return list(self._playlists)

    def add_track(self, track_id):
        return True

    def remove_track(self, track_id):
        return True

    def add_album(self, album_id):
        return True

    def remove_album(self, album_id):
        return True

    def add_artist(self, artist_id):
        return True

    def remove_artist(self, artist_id):
        return True

    def add_playlist(self, playlist_id):
        return True

    def remove_playlist(self, playlist_id):
        return True


def _track(track_id, name="Track"):
    return SimpleNamespace(
        id=track_id, name=name, title=name, duration=200,
        artist=SimpleNamespace(name="Artist"), artists=[SimpleNamespace(name="Artist")],
        album=SimpleNamespace(name="Album", image=lambda size=640: ""),
        audio_quality="LOSSLESS", is_hi_res_lossless=False, is_lossless=True,
        available=True, explicit=False,
    )


def _album(album_id, name="Album"):
    return SimpleNamespace(
        id=album_id, name=name, title=name, artist=SimpleNamespace(id=7, name="Artist"),
        image=lambda size=640: "", num_tracks=10, audio_quality="LOSSLESS",
        available=True, year=1996,
    )


def _artist(artist_id, name="Artist"):
    return SimpleNamespace(id=artist_id, name=name, picture=None)


def _playlist(playlist_id, name="Mix"):
    return SimpleNamespace(
        id=playlist_id, name=name, num_tracks=3, description="",
        square_picture=lambda size=640: "", image=None, picture=None,
    )


def _session(user_id, favorites):
    session = SimpleNamespace(user=SimpleNamespace(id=user_id, favorites=favorites))
    session.user.playlist_and_favorite_playlists = (
        lambda offset=0, limit=50: [] if offset else []
    )
    return session


def _patch_catalog(session, store):
    manager = SimpleNamespace(session=lambda: session, last_user_id=lambda: "")
    return (
        mock.patch.object(auth, "tidalapi_available", return_value=True),
        mock.patch.object(auth, "manager", manager),
        mock.patch.object(catalog, "library_cache", store),
    )


class FakeResp:
    def __init__(self, ok=True, status_code=200):
        self.ok = ok
        self.status_code = status_code


class FakeRequestLayer:
    def __init__(self, outcome):
        # outcome: a FakeResp to return or an Exception to raise
        self._outcome = outcome

    def basic_request(self, method, path):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def _auth_session(user_id=42, request_layer=None, access_token="tok"):
    return SimpleNamespace(
        token_type="Bearer",
        access_token=access_token,
        refresh_token="refresh",
        expiry_time=None,
        country_code="US",
        user=SimpleNamespace(id=user_id, email="u@example.com"),
        request=request_layer or FakeRequestLayer(FakeResp()),
    )


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------

class TidalLibraryCacheStoreTests(unittest.TestCase):
    def _store(self):
        return TidalLibraryCache(db_path=pathlib.Path(tempfile.mkdtemp()) / "cache.sqlite")

    def test_put_get_roundtrip(self):
        store = self._store()
        store.put("42", "tracks", [{"id": "1", "title": "T"}])
        self.assertEqual(store.get("42", "tracks"), [{"id": "1", "title": "T"}])

    def test_get_missing_and_unknown_kind(self):
        store = self._store()
        self.assertIsNone(store.get("42", "albums"))
        self.assertIsNone(store.get("42", "nope"))
        self.assertIsNone(store.get("", "tracks"))

    def test_put_empty_user_is_noop(self):
        store = self._store()
        store.put("", "tracks", [{"id": "1"}])
        self.assertIsNone(store.get("", "tracks"))

    def test_snapshot_shape_and_fetched_at(self):
        store = self._store()
        store.put("42", "ids", {"tracks": ["1"], "albums": []})
        store.put("42", "albums", [{"id": "a1"}])
        snap = store.snapshot("42")
        self.assertEqual(snap["user_id"], "42")
        self.assertEqual(snap["ids"], {"tracks": ["1"], "albums": []})
        self.assertEqual(snap["albums"], [{"id": "a1"}])
        self.assertEqual(snap["tracks"], [])
        self.assertEqual(snap["artists"], [])
        self.assertEqual(snap["playlists"], [])
        self.assertIsNotNone(snap["fetched_at"])

    def test_snapshot_empty_is_none(self):
        store = self._store()
        self.assertIsNone(store.snapshot("42"))
        self.assertIsNone(store.snapshot(""))

    def test_accounts_are_isolated(self):
        store = self._store()
        store.put("42", "albums", [{"id": "a1"}])
        self.assertIsNone(store.snapshot("99"))
        self.assertIsNone(store.get("99", "albums"))
        self.assertEqual(store.get("42", "albums"), [{"id": "a1"}])

    def test_clear_user(self):
        store = self._store()
        store.put("42", "ids", {"tracks": []})
        store.put("99", "ids", {"tracks": ["9"]})
        store.clear_user("42")
        self.assertIsNone(store.snapshot("42"))
        self.assertEqual(store.get("99", "ids"), {"tracks": ["9"]})

    def test_corrupted_row_is_missing(self):
        store = self._store()
        store.put("42", "tracks", [{"id": "1"}])
        with store._connect() as conn:
            conn.execute(
                "UPDATE library_cache SET payload_json = ? WHERE user_id = ? AND kind = ?",
                ("{not json", "42", "tracks"),
            )
        self.assertIsNone(store.get("42", "tracks"))
        snap = store.snapshot("42")
        self.assertIsNone(snap)


# ---------------------------------------------------------------------------
# catalog wrappers
# ---------------------------------------------------------------------------

class CatalogCacheTests(unittest.TestCase):
    def _store(self):
        return TidalLibraryCache(db_path=pathlib.Path(tempfile.mkdtemp()) / "cache.sqlite")

    def test_success_caches_and_failure_serves_cached(self):
        store = self._store()
        favorites = FakeFavorites(tracks=[_track(1), _track(2)])
        session = _session(42, favorites)
        p1, p2, p3 = self._patch_catalog(session, store)
        with p1, p2, p3:
            payload = catalog.favorites_tracks()
        self.assertEqual([t["id"] for t in payload], ["1", "2"])
        self.assertEqual(store.get("42", "tracks"), payload)

        # TIDAL now unreachable: the last-known state is served instead.
        favorites._fail = True
        with p1, p2, p3:
            served = catalog.favorites_tracks()
        self.assertEqual([t["id"] for t in served], ["1", "2"])

    def test_failure_without_cache_raises(self):
        store = self._store()
        favorites = FakeFavorites(tracks=[_track(1)])
        favorites._fail = True
        session = _session(42, favorites)
        p1, p2, p3 = self._patch_catalog(session, store)
        with p1, p2, p3:
            with self.assertRaises(auth.TidalAuthError):
                catalog.favorites_tracks()

    def test_favorite_state_cached_and_fallback(self):
        store = self._store()
        favorites = FakeFavorites(
            tracks=[SimpleNamespace(id=11)],
            albums=[SimpleNamespace(id="a1")],
            artists=[SimpleNamespace(id="ar1")],
            playlists=[SimpleNamespace(id="pl1")],
        )
        session = _session(42, favorites)
        p1, p2, p3 = self._patch_catalog(session, store)
        with p1, p2, p3:
            state = catalog.favorite_state()
        self.assertEqual(state["tracks"], ["11"])
        self.assertEqual(state["albums"], ["a1"])
        self.assertEqual(store.get("42", "ids"), state)

        favorites._fail = True
        with p1, p2, p3:
            served = catalog.favorite_state()
        self.assertEqual(served, state)

    def test_playlists_cached_and_fallback(self):
        store = self._store()
        session = _session(42, FakeFavorites())
        session.user.playlist_and_favorite_playlists = (
            lambda offset=0, limit=50: [_playlist("p1")] if offset == 0 else []
        )
        p1, p2, p3 = self._patch_catalog(session, store)
        with p1, p2, p3:
            payload = catalog.user_playlists()
        self.assertEqual([p["id"] for p in payload], ["p1"])
        self.assertEqual(store.get("42", "playlists"), payload)

        def fail(offset=0, limit=50):
            raise ConnectionError("tidal unreachable")

        session.user.playlist_and_favorite_playlists = fail
        with p1, p2, p3:
            served = catalog.user_playlists()
        self.assertEqual([p["id"] for p in served], ["p1"])

    def test_accounts_never_mix(self):
        store = self._store()
        favorites_a = FakeFavorites(tracks=[_track(1)])
        favorites_b = FakeFavorites(tracks=[_track(9)])
        p1a, p2a, p3a = self._patch_catalog(_session(42, favorites_a), store)
        with p1a, p2a, p3a:
            catalog.favorites_tracks()
        # Account B has its own cache key: a failing B never sees A's tracks.
        favorites_b._fail = True
        p1b, p2b, p3b = self._patch_catalog(_session(99, favorites_b), store)
        with p1b, p2b, p3b:
            with self.assertRaises(auth.TidalAuthError):
                catalog.favorites_tracks()

    def test_toggle_updates_cached_ids(self):
        store = self._store()
        favorites = FakeFavorites(tracks=[SimpleNamespace(id=11)], albums=[SimpleNamespace(id="a1")])
        session = _session(42, favorites)
        p1, p2, p3 = self._patch_catalog(session, store)
        with p1, p2, p3:
            catalog.favorite_state()
            catalog.set_track_favorite("11", False)
            catalog.set_album_favorite("a2", True)
        ids = store.get("42", "ids")
        self.assertEqual(ids["tracks"], [])
        self.assertIn("a2", ids["albums"])

    def _patch_catalog(self, session, store):
        return _patch_catalog(session, store)


# ---------------------------------------------------------------------------
# snapshot API endpoint
# ---------------------------------------------------------------------------

class SnapshotEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main_module.app)

    def test_snapshot_serves_only_cache(self):
        store = TidalLibraryCache(db_path=pathlib.Path(tempfile.mkdtemp()) / "cache.sqlite")
        store.put("42", "ids", {"tracks": ["1"]})
        store.put("42", "albums", [{"id": "a1"}])
        with mock.patch.object(main_module, "tidal_library_cache", store):
            resp = self.client.get("/api/streaming/tidal/library/snapshot?user=42")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["user_id"], "42")
        self.assertEqual(data["ids"], {"tracks": ["1"]})
        self.assertEqual(data["albums"], [{"id": "a1"}])
        self.assertEqual(data["tracks"], [])

    def test_snapshot_empty_payload_without_user(self):
        store = TidalLibraryCache(db_path=pathlib.Path(tempfile.mkdtemp()) / "cache.sqlite")
        with mock.patch.object(main_module, "tidal_library_cache", store):
            resp = self.client.get("/api/streaming/tidal/library/snapshot")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["user_id"], "")
        self.assertEqual(data["ids"], {})
        self.assertEqual(data["albums"], [])

    def test_snapshot_unknown_user_is_empty_not_error(self):
        store = TidalLibraryCache(db_path=pathlib.Path(tempfile.mkdtemp()) / "cache.sqlite")
        with mock.patch.object(main_module, "tidal_library_cache", store):
            resp = self.client.get("/api/streaming/tidal/library/snapshot?user=99")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["user_id"], "99")


# ---------------------------------------------------------------------------
# outage-resilient auth state
# ---------------------------------------------------------------------------

class AuthResilienceTests(unittest.TestCase):
    def test_success_populates_last_user(self):
        manager = auth.TidalSession()
        manager._session = _auth_session(user_id=42)
        self.assertTrue(manager.authenticated())
        self.assertEqual(manager.last_user_id(), "42")
        self.assertEqual(manager.user_payload()["email"], "u@example.com")

    def test_network_failure_keeps_last_known_state(self):
        manager = auth.TidalSession()
        manager._session = _auth_session(
            user_id=42, request_layer=FakeRequestLayer(ConnectionError("boom"))
        )
        manager._last_user = {"id": "42", "email": "u@example.com", "country_code": "US"}
        self.assertTrue(manager.authenticated())
        self.assertEqual(manager.last_user_id(), "42")

    def test_network_failure_without_history_is_not_authenticated(self):
        manager = auth.TidalSession()
        manager._session = _auth_session(
            user_id=42, request_layer=FakeRequestLayer(ConnectionError("boom"))
        )
        self.assertFalse(manager.authenticated())

    def test_revoked_token_clears_last_user(self):
        manager = auth.TidalSession()
        manager._session = _auth_session(
            user_id=42, request_layer=FakeRequestLayer(FakeResp(ok=False, status_code=401))
        )
        manager._last_user = {"id": "42", "email": "u@example.com", "country_code": "US"}
        self.assertFalse(manager.authenticated())
        self.assertEqual(manager.last_user_id(), "")
        self.assertIsNone(manager._last_user)

    def test_server_error_keeps_last_known_state(self):
        manager = auth.TidalSession()
        manager._session = _auth_session(
            user_id=42, request_layer=FakeRequestLayer(FakeResp(ok=False, status_code=503))
        )
        manager._last_user = {"id": "42", "email": "u@example.com", "country_code": "US"}
        self.assertTrue(manager.authenticated())
        self.assertEqual(manager.last_user_id(), "42")

    def test_session_file_roundtrip_persists_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_file = pathlib.Path(tmp) / "tidal-session.json"

            class FakeLoadSession:
                def __init__(self):
                    self.access_token = "tok"
                    self._fail = False

                def load_oauth_session(self, *args, **kwargs):
                    if self._fail:
                        raise ConnectionError("tidal unreachable")
                    return True

            fake_session = FakeLoadSession()
            manager = auth.TidalSession()
            manager._last_user = {"id": "42", "email": "u@example.com", "country_code": "US"}
            manager._session = _auth_session(user_id=42)
            with mock.patch.object(auth, "SESSION_FILE", session_file), \
                 mock.patch.object(auth, "tidalapi", SimpleNamespace(Session=object)), \
                 mock.patch.object(auth, "_new_session", return_value=fake_session):
                manager._save_session(manager._session)
                self.assertIn("user", json.loads(session_file.read_text()))

                restored = auth.TidalSession()
                loaded = restored._load_session()
                self.assertIsNotNone(loaded)
                self.assertEqual(restored.last_user_id(), "42")
                self.assertEqual(restored.user_payload()["email"], "u@example.com")

    def test_load_tolerates_network_failure_and_keeps_tokens_and_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_file = pathlib.Path(tmp) / "tidal-session.json"
            session_file.write_text(json.dumps({
                "token_type": "Bearer",
                "access_token": "tok",
                "user": {"id": "42", "email": "u@example.com", "country_code": "US"},
            }))

            class FailingLoadSession:
                def __init__(self):
                    self.access_token = "tok"

                def load_oauth_session(self, *args, **kwargs):
                    raise ConnectionError("tidal unreachable")

            manager = auth.TidalSession()
            with mock.patch.object(auth, "SESSION_FILE", session_file), \
                 mock.patch.object(auth, "tidalapi", SimpleNamespace(Session=object)), \
                 mock.patch.object(auth, "_new_session", return_value=FailingLoadSession()):
                loaded = manager._load_session()
            self.assertIsNotNone(loaded)
            self.assertEqual(manager.last_user_id(), "42")

    def test_clear_resets_last_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_file = pathlib.Path(tmp) / "tidal-session.json"
            session_file.write_text(json.dumps({"token_type": "Bearer"}))
            manager = auth.TidalSession()
            manager._last_user = {"id": "42", "email": "u@example.com", "country_code": "US"}
            with mock.patch.object(auth, "SESSION_FILE", session_file):
                asyncio.run(manager.clear())
            self.assertIsNone(manager._last_user)
            self.assertEqual(manager.last_user_id(), "")
            self.assertFalse(session_file.exists())

    def test_parse_user_payload_tolerates_legacy_file(self):
        self.assertIsNone(auth._parse_user_payload(None))
        self.assertIsNone(auth._parse_user_payload("nope"))
        payload = auth._parse_user_payload({"id": 7, "email": "a@b.c"})
        self.assertEqual(payload["id"], "7")


# ---------------------------------------------------------------------------
# playlist writes (create + add tracks, ETag, cache invalidation)
# ---------------------------------------------------------------------------

class FakePlaylistObj:
    """Minimal tidalapi Playlist stand-in carrying the v1 write ETag."""

    def __init__(self, playlist_id="pl9", name="New Mix"):
        self.id = playlist_id
        self.name = name
        self.num_tracks = 0
        self.description = ""
        self.square_picture = lambda size=640: ""
        self.image = None
        self.picture = None
        self._etag = '"etag-1"'


class FakeJsonResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeWriteSession:
    """Session faking the write path: create_playlist, playlist(), request layer."""

    def __init__(self, user_id=42, add_payload=None, fail=None):
        self.user = SimpleNamespace(id=user_id, create_playlist=self.create_playlist)
        self.request = SimpleNamespace(request=self._request)
        self._add_payload = add_payload if add_payload is not None else {"addedItemIds": ["1", "2"]}
        self._fail = fail  # 'create' | 'playlist' | 'add' | None
        self.calls = []

    def create_playlist(self, title, description=""):
        self.calls.append(("create", title, description))
        if self._fail == "create":
            raise ConnectionError("tidal unreachable")
        return FakePlaylistObj("pl9", title)

    def playlist(self, playlist_id):
        self.calls.append(("playlist", playlist_id))
        if self._fail == "playlist":
            raise ConnectionError("tidal unreachable")
        return FakePlaylistObj(str(playlist_id), "Existing")

    def _request(self, method, path, data=None, headers=None):
        self.calls.append(("request", method, path, data, headers))
        if self._fail == "add":
            raise ConnectionError("tidal unreachable")
        return FakeJsonResp(self._add_payload)


class PlaylistWriteTests(unittest.TestCase):
    def _store(self):
        return TidalLibraryCache(db_path=pathlib.Path(tempfile.mkdtemp()) / "cache.sqlite")

    def _patch_catalog(self, session, store):
        manager = SimpleNamespace(session=lambda: session, last_user_id=lambda: "")
        return (
            mock.patch.object(auth, "tidalapi_available", return_value=True),
            mock.patch.object(auth, "manager", manager),
            mock.patch.object(catalog, "library_cache", store),
        )

    def test_create_playlist_with_tracks_sends_etag_and_invalidates_cache(self):
        store = self._store()
        store.put("42", "playlists", [{"id": "old"}])
        session = FakeWriteSession()
        p1, p2, p3 = self._patch_catalog(session, store)
        with p1, p2, p3:
            result = catalog.create_playlist("  My Mix  ", "", ["1", "2"])
        self.assertEqual(result["id"], "pl9")
        self.assertEqual(result["name"], "My Mix")
        self.assertEqual(session.calls[0], ("create", "My Mix", ""))
        kind, method, path, data, headers = session.calls[1]
        self.assertEqual((kind, method, path), ("request", "POST", "playlists/pl9/items"))
        self.assertEqual(data["trackIds"], "1,2")
        self.assertEqual(headers, {"If-None-Match": '"etag-1"'})
        # The stale cached playlist list is dropped so the next load refreshes.
        self.assertIsNone(store.get("42", "playlists"))

    def test_create_playlist_without_tracks_skips_items_write(self):
        store = self._store()
        session = FakeWriteSession()
        p1, p2, p3 = self._patch_catalog(session, store)
        with p1, p2, p3:
            result = catalog.create_playlist("Empty Mix")
        self.assertEqual(result["id"], "pl9")
        self.assertEqual([c[0] for c in session.calls], ["create"])

    def test_create_playlist_failure_raises_and_keeps_cache(self):
        store = self._store()
        store.put("42", "playlists", [{"id": "old"}])
        session = FakeWriteSession(fail="create")
        p1, p2, p3 = self._patch_catalog(session, store)
        with p1, p2, p3:
            with self.assertRaises(auth.TidalAuthError):
                catalog.create_playlist("My Mix")
        self.assertEqual(store.get("42", "playlists"), [{"id": "old"}])

    def test_add_playlist_tracks_success(self):
        store = self._store()
        store.put("42", "playlists", [{"id": "p1"}])
        session = FakeWriteSession(add_payload={"addedItemIds": ["1", "2"]})
        p1, p2, p3 = self._patch_catalog(session, store)
        with p1, p2, p3:
            result = catalog.add_playlist_tracks("pl9", ["1", "2"])
        self.assertEqual(result["playlist_id"], "pl9")
        self.assertEqual(result["added_track_ids"], ["1", "2"])
        self.assertEqual(session.calls[0], ("playlist", "pl9"))
        self.assertEqual(session.calls[1][4], {"If-None-Match": '"etag-1"'})
        self.assertIsNone(store.get("42", "playlists"))

    def test_add_playlist_tracks_requires_ids(self):
        store = self._store()
        session = FakeWriteSession()
        p1, p2, p3 = self._patch_catalog(session, store)
        with p1, p2, p3:
            with self.assertRaises(auth.TidalAuthError):
                catalog.add_playlist_tracks("pl9", [])

    def test_add_playlist_tracks_failure_keeps_cache(self):
        store = self._store()
        store.put("42", "playlists", [{"id": "p1"}])
        session = FakeWriteSession(fail="add")
        p1, p2, p3 = self._patch_catalog(session, store)
        with p1, p2, p3:
            with self.assertRaises(auth.TidalAuthError):
                catalog.add_playlist_tracks("pl9", ["1"])
        self.assertEqual(store.get("42", "playlists"), [{"id": "p1"}])


class PlaylistWriteEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main_module.app)

    class FakeWriteProvider:
        def __init__(self):
            self.calls = []

        async def create_playlist(self, title, description="", track_ids=None):
            self.calls.append(("create", title, description, track_ids))
            return {"id": "pl9", "name": title, "track_count": len(track_ids or []), "art_url": "", "description": ""}

        async def add_playlist_tracks(self, playlist_id, track_ids):
            self.calls.append(("add", playlist_id, track_ids))
            return {"playlist_id": playlist_id, "added_track_ids": list(track_ids)}

    def test_create_playlist_endpoint(self):
        provider = self.FakeWriteProvider()
        with mock.patch.object(main_module, "_streaming_provider", return_value=provider):
            resp = self.client.post(
                "/api/streaming/tidal/playlists/create",
                json={"name": "My Mix", "track_ids": ["1", "2"]},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "My Mix")
        self.assertEqual(provider.calls, [("create", "My Mix", "", ["1", "2"])])

    def test_create_playlist_requires_name(self):
        provider = self.FakeWriteProvider()
        with mock.patch.object(main_module, "_streaming_provider", return_value=provider):
            resp = self.client.post("/api/streaming/tidal/playlists/create", json={"track_ids": ["1"]})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(provider.calls, [])

    def test_add_tracks_endpoint(self):
        provider = self.FakeWriteProvider()
        with mock.patch.object(main_module, "_streaming_provider", return_value=provider):
            resp = self.client.post(
                "/api/streaming/tidal/playlists/pl9/tracks",
                json={"track_ids": ["1", "2"]},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(provider.calls, [("add", "pl9", ["1", "2"])])

    def test_add_tracks_requires_ids(self):
        provider = self.FakeWriteProvider()
        with mock.patch.object(main_module, "_streaming_provider", return_value=provider):
            resp = self.client.post("/api/streaming/tidal/playlists/pl9/tracks", json={"track_ids": []})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(provider.calls, [])

    def test_write_error_maps_to_401(self):
        class FailingProvider:
            async def create_playlist(self, *args, **kwargs):
                raise auth.TidalAuthError("TIDAL playlist creation failed: boom")

        with mock.patch.object(main_module, "_streaming_provider", return_value=FailingProvider()):
            resp = self.client.post("/api/streaming/tidal/playlists/create", json={"name": "X"})
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
