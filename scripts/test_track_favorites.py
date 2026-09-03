#!/usr/bin/env python3
"""Focused tests for per-track favorites in the library metadata store.

Covers:
  - set / unset track favorite (persistence + independence from album favorite)
  - favorite-first Top-40 ordering without touching play counts
  - non-favorite ordering, limit, and zero-play favorites
  - migration safety for existing libraries
  - scanner cache sync and the central mutation route
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = tempfile.mkdtemp(prefix="fxroute-test-track-fav-")
os.environ["MUSIC_ROOT"] = str(Path(BASE_DIR) / "music")
os.environ["XDG_CONFIG_HOME"] = str(Path(BASE_DIR) / "config")
os.environ["LOG_LEVEL"] = "WARNING"
Path(os.environ["MUSIC_ROOT"]).mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from library.api import LibraryApiRuntime, configure_runtime, set_track_favorite as set_track_favorite_route
from library.metadata import LibraryMetadataStore
from library.core import LibraryScanner
from models import Track


def _payload(track_id: str, title: str = "Song") -> dict:
    return {
        "rel_path": f"album/{track_id}.flac",
        "track_id": track_id,
        "mtime_ns": 1,
        "size_bytes": 2,
        "title": title,
        "artist": "Artist",
        "album": "Album",
        "album_artist": "Artist",
        "genre": None,
        "year": None,
        "track_number": 1,
        "disc_number": 1,
        "duration": 180.0,
        "sample_rate_hz": 44100,
    }


class _FakeRequest:
    def __init__(self, body: dict) -> None:
        self._body = body

    async def json(self) -> dict:
        return self._body


class _FakeScanner:
    """Scanner stub for the route test: no filesystem, no scans."""

    def __init__(self, tracks: list[Track]) -> None:
        self._tracks = list(tracks)

    def get_tracks(self, authoritative: bool = False) -> list[Track]:
        return list(self._tracks)

    def set_track_favorite(self, track_id: str, favorite: bool) -> dict:
        for track in self._tracks:
            if track.id == track_id:
                track.favorite = bool(favorite)
        return {"track_id": track_id, "favorite": bool(favorite)}


class StoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="fxroute-test-track-fav-")
        self.db_path = Path(tmp) / "library-metadata.sqlite"
        self.cover_dir = Path(tmp) / "covers"
        self.store = LibraryMetadataStore(db_path=self.db_path, cover_dir=self.cover_dir)

    def _seed(self, track_id: str, play_count: int = 0, last_played_at: str | None = None, favorite: bool = False):
        self.store.upsert_track_metadata(_payload(track_id))
        with self.store._connect() as conn:
            conn.execute(
                "UPDATE tracks SET play_count = ?, last_played_at = ?, favorite = ? WHERE track_id = ?",
                (play_count, last_played_at, 1 if favorite else 0, track_id),
            )

    def test_set_and_unset_track_favorite(self):
        self._seed("t1")
        result = self.store.set_track_favorite("t1", True)
        self.assertTrue(result["favorite"])
        with self.store._connect() as conn:
            row = conn.execute("SELECT favorite FROM tracks WHERE track_id = 't1'").fetchone()
        self.assertEqual(row["favorite"], 1)

        result = self.store.set_track_favorite("t1", False)
        self.assertFalse(result["favorite"])
        with self.store._connect() as conn:
            row = conn.execute("SELECT favorite FROM tracks WHERE track_id = 't1'").fetchone()
        self.assertEqual(row["favorite"], 0)

    def test_favorite_persists_after_reopen(self):
        self._seed("t1")
        self.store.set_track_favorite("t1", True)
        reopened = LibraryMetadataStore(db_path=self.db_path, cover_dir=self.cover_dir)
        with reopened._connect() as conn:
            row = conn.execute("SELECT favorite FROM tracks WHERE track_id = 't1'").fetchone()
        self.assertEqual(row["favorite"], 1)

    def test_album_and_track_favorite_are_independent(self):
        self._seed("t1")
        self.store.set_album_favorite("album-key", True)
        self.store.set_track_favorite("t1", True)

        album = self.store.get_album("album-key")
        self.assertTrue(album["favorite"])
        with self.store._connect() as conn:
            track_row = conn.execute("SELECT favorite FROM tracks WHERE track_id = 't1'").fetchone()
        self.assertEqual(track_row["favorite"], 1)

        # Unfavoriting the track must not touch the album.
        self.store.set_track_favorite("t1", False)
        album = self.store.get_album("album-key")
        self.assertTrue(album["favorite"])

    def test_favorite_track_prioritized_in_top_tracks(self):
        self._seed("low_fav", play_count=1, last_played_at="2024-01-01T00:00:00+00:00", favorite=True)
        self._seed("high_plain", play_count=50, last_played_at="2024-01-02T00:00:00+00:00")

        top = self.store.get_top_tracks(limit=40)
        ids = [row["track_id"] for row in top]
        self.assertEqual(ids[0], "low_fav", "favorited track must come first regardless of play_count")
        self.assertEqual(ids[1], "high_plain")

    def test_non_favorite_ordering_unchanged(self):
        self._seed("older", play_count=10, last_played_at="2024-01-01T00:00:00+00:00")
        self._seed("newer", play_count=10, last_played_at="2024-02-01T00:00:00+00:00")
        self._seed("most", play_count=99, last_played_at="2024-01-01T00:00:00+00:00")

        top = self.store.get_top_tracks(limit=40)
        self.assertEqual([row["track_id"] for row in top], ["most", "newer", "older"])

    def test_limit_respected(self):
        for i in range(50):
            self._seed(f"t{i:02d}", play_count=i + 1, last_played_at=f"2024-01-01T00:00:{i:02d}+00:00")
        top = self.store.get_top_tracks(limit=40)
        self.assertEqual(len(top), 40)

    def test_zero_play_favorite_is_included(self):
        self._seed("fresh_fav", play_count=0, favorite=True)
        self._seed("played", play_count=5, last_played_at="2024-01-01T00:00:00+00:00")
        top = self.store.get_top_tracks(limit=40)
        ids = [row["track_id"] for row in top]
        self.assertIn("fresh_fav", ids)
        self.assertEqual(ids[0], "fresh_fav")

    def test_migration_preserves_existing_track_data(self):
        # Build a pre-favorite library in a fresh path: tracks table without the favorite column.
        tmp = tempfile.mkdtemp(prefix="fxroute-test-track-fav-migrate-")
        db_path = Path(tmp) / "library-metadata.sqlite"
        old = sqlite3.connect(db_path)
        old.execute(
            """
            CREATE TABLE tracks (
                rel_path TEXT PRIMARY KEY,
                track_id TEXT NOT NULL,
                mtime_ns INTEGER NOT NULL,
                size_bytes INTEGER NOT NULL,
                title TEXT NOT NULL,
                artist TEXT,
                album TEXT,
                album_artist TEXT,
                genre TEXT,
                year INTEGER,
                track_number INTEGER,
                disc_number INTEGER,
                duration REAL,
                sample_rate_hz INTEGER,
                play_count INTEGER NOT NULL DEFAULT 0,
                last_played_at TEXT,
                last_seen_at TEXT,
                missing_since TEXT
            )
            """
        )
        old.execute(
            "INSERT INTO tracks (rel_path, track_id, mtime_ns, size_bytes, title, play_count, last_played_at, last_seen_at, missing_since) "
            "VALUES ('album/t1.flac', 't1', 1, 2, 'Song', 7, '2024-01-01T00:00:00+00:00', NULL, NULL)"
        )
        old.commit()
        old.close()

        reopened = LibraryMetadataStore(db_path=db_path, cover_dir=Path(tmp) / "covers")
        with reopened._connect() as conn:
            row = conn.execute("SELECT play_count, last_played_at, favorite FROM tracks WHERE track_id = 't1'").fetchone()
        self.assertEqual(row["play_count"], 7)
        self.assertEqual(row["last_played_at"], "2024-01-01T00:00:00+00:00")
        self.assertEqual(row["favorite"], 0, "existing tracks must default to not favorited")


class TrackModelTests(unittest.TestCase):
    def test_to_dict_includes_favorite(self):
        track = Track(id="t1", title="Song", favorite=True)
        self.assertTrue(track.to_dict()["favorite"])
        track.favorite = False
        self.assertFalse(track.to_dict()["favorite"])


class ScannerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="fxroute-test-track-fav-scanner-")
        self.db_path = Path(tmp) / "library-metadata.sqlite"
        self.cover_dir = Path(tmp) / "covers"
        self.store = LibraryMetadataStore(db_path=self.db_path, cover_dir=self.cover_dir)
        self.scanner = LibraryScanner(metadata_store=self.store)

    def _seed_track(self, track_id: str, play_count: int = 0, last_played_at: str | None = None, favorite: bool = False):
        self.store.upsert_track_metadata(_payload(track_id))
        with self.store._connect() as conn:
            conn.execute(
                "UPDATE tracks SET play_count = ?, last_played_at = ?, favorite = ? WHERE track_id = ?",
                (play_count, last_played_at, 1 if favorite else 0, track_id),
            )

    def test_set_track_favorite_updates_cache(self):
        self.scanner._track_cache = [Track(id="t1", title="Song")]
        self._seed_track("t1")
        self.scanner.set_track_favorite("t1", True)
        self.assertTrue(self.scanner._track_cache[0].favorite)
        self.assertTrue(self.scanner._track_cache[0].to_dict()["favorite"])

    def test_rescan_of_edited_file_preserves_favorite(self):
        """A cache miss (edited file) must not drop a persisted favorite.

        The store keeps the flag on upsert; the rebuilt in-memory Track used
        to lose it, so the live listing silently showed the track as
        unfavorited after any file change triggered a rescan.
        """
        import wave as _wave

        rel = "album/fav.wav"
        filepath = Path(os.environ["MUSIC_ROOT"]) / rel
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with _wave.open(str(filepath), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(b"\x00\x00" * 4410)

        track_id = f"local_{rel}"
        # First scan already happened and the user favorited the track.
        self._seed_track(track_id, favorite=True)
        with self.store._connect() as conn:
            conn.execute(
                "UPDATE tracks SET rel_path = ? WHERE track_id = ?",
                (rel, track_id),
            )

        # File edited (mtime/size change) -> full rescan hits the miss path.
        with _wave.open(str(filepath), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(b"\x00\x00" * 8820)

        rebuilt = self.scanner._create_track_from_file(filepath)
        self.assertIsNotNone(rebuilt)
        self.assertEqual(rebuilt.id, track_id)
        self.assertTrue(
            rebuilt.favorite,
            "rescan of an edited, favorited file must keep the favorite",
        )

        # The store row is still the old fingerprint (miss path re-upserts
        # with the new one), so the favorite must survive that too.
        self.store.upsert_track_metadata(self.scanner._track_cache_payload(
            rebuilt, rel, filepath.stat()
        ))
        row = self.store.get_cached_track(rel, filepath.stat().st_mtime_ns, filepath.stat().st_size)
        self.assertTrue(bool(row["favorite"]))

    def test_new_file_without_history_stays_unfavorited(self):
        import wave as _wave

        rel = "album/fresh.wav"
        filepath = Path(os.environ["MUSIC_ROOT"]) / rel
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with _wave.open(str(filepath), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(b"\x00\x00" * 4410)

        rebuilt = self.scanner._create_track_from_file(filepath)
        self.assertIsNotNone(rebuilt)
        self.assertFalse(rebuilt.favorite)

    def test_get_top_played_tracks_carries_favorite(self):
        self._seed_track("fav", play_count=1, last_played_at="2024-01-01T00:00:00+00:00", favorite=True)
        self._seed_track("plain", play_count=10, last_played_at="2024-01-02T00:00:00+00:00")
        self.scanner._track_cache = [
            Track(id="fav", title="Fav"),
            Track(id="plain", title="Plain"),
        ]
        result = self.scanner.get_top_played_tracks(limit=40)
        self.assertEqual([row["id"] for row in result], ["fav", "plain"])
        self.assertTrue(result[0]["favorite"])
        self.assertFalse(result[1]["favorite"])


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tracks = [Track(id="local_album/a.flac", title="A")]
        self.scanner = _FakeScanner(tracks)
        configure_runtime(LibraryApiRuntime(
            get_scanner=lambda: self.scanner,
            get_settings=lambda: None,
        ))

    async def test_route_favorites_and_unfavorites(self):
        resp = await set_track_favorite_route("local_album/a.flac", _FakeRequest({"favorite": True}))
        self.assertEqual(resp, {"status": "ok", "track_id": "local_album/a.flac", "favorite": True})
        self.assertTrue(self.scanner.get_tracks()[0].favorite)

    async def test_route_syncs_playback_track_favorite(self):
        """The runtime hook must mirror a persisted favorite into live playback dicts."""
        synced = []
        configure_runtime(LibraryApiRuntime(
            get_scanner=lambda: self.scanner,
            get_settings=lambda: None,
            sync_track_favorite=lambda track_id, favorite: synced.append((track_id, favorite)),
        ))
        await set_track_favorite_route("local_album/a.flac", _FakeRequest({"favorite": True}))
        self.assertEqual(synced, [("local_album/a.flac", True)])
        await set_track_favorite_route("local_album/a.flac", _FakeRequest({"favorite": False}))
        self.assertEqual(synced[-1], ("local_album/a.flac", False))

        resp = await set_track_favorite_route("local_album/a.flac", _FakeRequest({"favorite": False}))
        self.assertEqual(resp["favorite"], False)

    async def test_route_404_for_unknown_track(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await set_track_favorite_route("local_album/nope.flac", _FakeRequest({"favorite": True}))
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
