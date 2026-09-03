"""Persistent smart metadata cache for the local music library."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional
from urllib.parse import quote

import requests

from safe_http import COVER_ART_MAX_BYTES, ENRICHMENT_JSON_MAX_BYTES, safe_get

logger = logging.getLogger(__name__)

# Artist/release enrichment (MusicBrainz matching, Wikipedia/Wikidata about
# text, ListenBrainz similar artists) lives in the shared owner; the library
# store keeps a compatible request boundary and delegates the network work.
from artist_enrichment import (
    DISCOVER_COOLDOWN_SECONDS,
    FETCH_COOLDOWN_SECONDS,
    LISTENBRAINZ_API,
    TRANSIENT_ERROR_RETRY_SECONDS,
    USER_AGENT,
    ArtistEnrichmentService,
    _derive_enrichment_db_path,
    _json_list,
    _useful_label,
)

COVER_ART_API = "https://coverartarchive.org"
COVER_ART_MAX_BYTES = 8 * 1024 * 1024  # single cover image
MISSING_RETENTION_SECONDS = 60 * 24 * 60 * 60
MAX_ENRICH_PER_SCAN = 8


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _config_dir() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return root / "fxroute"


class LibraryMetadataStore:
    """SQLite-backed cache for external album metadata and covers."""

    def __init__(self, db_path: Path | None = None, cover_dir: Path | None = None, artist_enrichment: ArtistEnrichmentService | None = None):
        self.db_path = Path(db_path) if db_path is not None else (_config_dir() / "library-metadata.sqlite")
        self.cover_dir = cover_dir or (_config_dir() / "library-metadata-covers")
        if artist_enrichment is None:
            # Default: a shared enrichment service whose network calls dispatch
            # through this store's (patchable) _request_json, caching into the
            # shared artist-enrichment DB so library and providers share data.
            artist_enrichment = ArtistEnrichmentService(
                db_path=_derive_enrichment_db_path(self.db_path),
                request_backend=self,
            )
        self.artist_enrichment = artist_enrichment
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.cover_dir.mkdir(parents=True, exist_ok=True)
        self._last_request_at = 0.0
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS albums (
                    album_key TEXT PRIMARY KEY,
                    album TEXT NOT NULL,
                    artist TEXT NOT NULL,
                    mb_artist_id TEXT,
                    mb_release_id TEXT,
                    mb_release_group_id TEXT,
                    release_type TEXT,
                    year INTEGER,
                    country TEXT,
                    label TEXT,
                    genres_json TEXT DEFAULT '[]',
                    favorite INTEGER NOT NULL DEFAULT 0,
                    artist_description TEXT,
                    album_description TEXT,
                    local_cover_source TEXT,
                    external_cover_path TEXT,
                    external_cover_mime TEXT,
                    last_seen_at TEXT,
                    missing_since TEXT,
                    metadata_updated_at TEXT,
                    about_attempted_at TEXT,
                    fetch_attempted_at TEXT,
                    fetch_error TEXT
                )
                """
            )
            self._ensure_column(conn, "albums", "favorite", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "albums", "artist_description", "TEXT")
            self._ensure_column(conn, "albums", "album_description", "TEXT")
            self._ensure_column(conn, "albums", "about_attempted_at", "TEXT")
            self._ensure_column(conn, "albums", "local_cover_source", "TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_albums_missing_since ON albums(missing_since)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS album_discover (
                    album_key TEXT PRIMARY KEY,
                    seed_type TEXT,
                    seed_id TEXT,
                    items_json TEXT DEFAULT '[]',
                    updated_at TEXT,
                    attempted_at TEXT,
                    error TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tracks (
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
                    favorite INTEGER NOT NULL DEFAULT 0,
                    play_count INTEGER NOT NULL DEFAULT 0,
                    last_played_at TEXT,
                    last_seen_at TEXT,
                    missing_since TEXT
                )
                """
            )
            self._ensure_column(conn, "tracks", "play_count", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "tracks", "last_played_at", "TEXT")
            self._ensure_column(conn, "tracks", "favorite", "INTEGER NOT NULL DEFAULT 0")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_missing_since ON tracks(missing_since)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_play_count ON tracks(play_count DESC, last_played_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_favorite_play_count ON tracks(favorite DESC, play_count DESC, last_played_at DESC)")

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def sync_albums(self, albums: Iterable[dict[str, Any]]) -> None:
        """Mark current albums active and enrich a small batch of stale entries."""
        now = _utc_now()
        active = []
        with self._connect() as conn:
            for album in albums:
                album_key = str(album.get("id") or "").strip()
                name = str(album.get("name") or "").strip()
                artist = str(album.get("artist") or "").strip()
                if not album_key or not name or not artist or name == "Various":
                    continue
                active.append(album_key)
                conn.execute(
                    """
                    INSERT INTO albums (album_key, album, artist, local_cover_source, last_seen_at, missing_since)
                    VALUES (?, ?, ?, ?, ?, NULL)
                    ON CONFLICT(album_key) DO UPDATE SET
                        album=excluded.album,
                        artist=excluded.artist,
                        local_cover_source=excluded.local_cover_source,
                        last_seen_at=excluded.last_seen_at,
                        missing_since=NULL
                    """,
                    (album_key, name, artist, album.get("cover_source"), now),
                )

            if active:
                placeholders = ",".join("?" for _ in active)
                conn.execute(
                    f"UPDATE albums SET missing_since=COALESCE(missing_since, ?) WHERE album_key NOT IN ({placeholders}) AND missing_since IS NULL",
                    [now, *active],
                )

            cutoff = time.time() - MISSING_RETENTION_SECONDS
            stale_rows = conn.execute("SELECT album_key, external_cover_path, missing_since FROM albums WHERE missing_since IS NOT NULL").fetchall()
            for row in stale_rows:
                try:
                    missing_ts = datetime.fromisoformat(str(row["missing_since"]).replace("Z", "+00:00")).timestamp()
                except Exception:
                    continue
                if missing_ts > cutoff:
                    continue
                cover_path = row["external_cover_path"]
                if cover_path:
                    try:
                        Path(cover_path).unlink(missing_ok=True)
                    except Exception:
                        pass
                conn.execute("DELETE FROM albums WHERE album_key = ?", (row["album_key"],))

        self.enrich_due_albums(limit=MAX_ENRICH_PER_SCAN)

    def enrich_due_albums(self, limit: int = MAX_ENRICH_PER_SCAN) -> None:
        rows = []
        with self._connect() as conn:
            candidates = conn.execute(
                """
                SELECT * FROM albums
                WHERE missing_since IS NULL
                ORDER BY COALESCE(fetch_attempted_at, ''), album COLLATE NOCASE
                """
            ).fetchall()
        now_ts = time.time()
        for row in candidates:
            needs_external_cover = not row["local_cover_source"] and not row["external_cover_path"]
            needs_about = (
                not row["about_attempted_at"]
                and not row["artist_description"]
                and not row["album_description"]
                and (row["mb_artist_id"] or row["mb_release_group_id"] or row["mb_release_id"])
            )
            if row["mb_release_id"] and row["metadata_updated_at"] and not needs_external_cover and not needs_about:
                continue
            attempted = row["fetch_attempted_at"]
            if attempted and not needs_about:
                try:
                    retry_after = FETCH_COOLDOWN_SECONDS
                    if row["fetch_error"] and row["fetch_error"] != "no safe MusicBrainz match":
                        retry_after = TRANSIENT_ERROR_RETRY_SECONDS
                    if now_ts - datetime.fromisoformat(str(attempted).replace("Z", "+00:00")).timestamp() < retry_after:
                        continue
                except Exception:
                    pass
            rows.append(row)
            if len(rows) >= limit:
                break

        for row in rows:
            try:
                self._enrich_album(row)
            except Exception as exc:
                logger.info("Smart metadata enrichment failed for %s - %s: %s", row["artist"], row["album"], exc)
                self._mark_attempt(row["album_key"], str(exc)[:240])

    def get_album(self, album_key: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM albums WHERE album_key = ?", (album_key,)).fetchone()
        result = self._row_to_api(row) if row else {}
        if row:
            self._adopt_existing_artist_enrichment(row)
        return result

    def _adopt_existing_artist_enrichment(self, row: sqlite3.Row) -> None:
        """Lazily move pre-existing per-album artist enrichment into the shared
        cache so provider enrichment reuses it instead of re-fetching the net.

        Only rows that already carry a MusicBrainz artist id and a description
        trigger the (cheap) shared-cache check; the library album row itself is
        never modified and stays the compatibility copy.
        """
        mb_artist_id = str(row["mb_artist_id"] or "").strip()
        description = str(row["artist_description"] or "").strip()
        if not mb_artist_id or not description:
            return
        try:
            existing = self.artist_enrichment.store.get_artist(mb_artist_id)
            if existing and existing["description"]:
                return
            self.artist_enrichment.store.upsert_artist_description(
                mb_artist_id, None, description,
                attempted_at=_utc_now(), error=None,
            )
        except Exception as exc:  # noqa: BLE001 - adoption is best-effort
            logger.debug("Artist enrichment adoption failed for %s: %s", mb_artist_id, exc)

    def set_album_favorite(self, album_key: str, favorite: bool) -> dict[str, Any]:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO albums (album_key, album, artist, favorite, last_seen_at)
                VALUES (?, '', '', ?, ?)
                ON CONFLICT(album_key) DO UPDATE SET favorite=excluded.favorite
                """,
                (album_key, 1 if favorite else 0, now),
            )
            row = conn.execute("SELECT * FROM albums WHERE album_key = ?", (album_key,)).fetchone()
        return self._row_to_api(row) if row else {"favorite": bool(favorite)}

    def get_album_discover(self, album_key: str, force: bool = False) -> dict[str, Any]:
        """Return cached ListenBrainz suggestions for an album, refreshing when stale."""
        album_key = str(album_key or "").strip()
        if not album_key:
            return {"items": [], "source": None, "cached": False}
        with self._connect() as conn:
            album = conn.execute("SELECT * FROM albums WHERE album_key = ?", (album_key,)).fetchone()
            cached = conn.execute("SELECT * FROM album_discover WHERE album_key = ?", (album_key,)).fetchone()
        if not album:
            return {"items": [], "source": None, "cached": False}

        if cached and not force:
            items = self._discover_items_from_row(cached)
            cached_has_artist_items = items and all(str(item.get("type") or "") == "artist" for item in items if isinstance(item, dict))
            updated_at = cached["updated_at"] or cached["attempted_at"]
            if updated_at and cached_has_artist_items:
                try:
                    age = time.time() - datetime.fromisoformat(str(updated_at).replace("Z", "+00:00")).timestamp()
                    if age < DISCOVER_COOLDOWN_SECONDS:
                        return {
                            "items": items,
                            "source": cached["seed_type"],
                            "seed_id": cached["seed_id"],
                            "cached": True,
                            "error": cached["error"],
                        }
                except Exception:
                    pass

        seed_type, seed_id = self._discover_seed(album)
        if not seed_id:
            return self._store_album_discover(album_key, None, None, [], "missing MusicBrainz seed")
        try:
            items = self._fetch_listenbrainz_discover_items(seed_type, seed_id, str(album["artist"] or ""))
            return self._store_album_discover(album_key, seed_type, seed_id, items, None)
        except Exception as exc:
            logger.info("ListenBrainz discover failed for %s (%s:%s): %s", album_key, seed_type, seed_id, exc)
            if cached:
                result = {
                    "items": self._discover_items_from_row(cached),
                    "source": cached["seed_type"],
                    "seed_id": cached["seed_id"],
                    "cached": True,
                    "error": str(exc)[:240],
                }
                self._store_album_discover(album_key, seed_type, seed_id, result["items"], str(exc)[:240])
                return result
            return self._store_album_discover(album_key, seed_type, seed_id, [], str(exc)[:240])

    def _discover_items_from_row(self, row: sqlite3.Row) -> list[dict[str, Any]]:
        try:
            items = json.loads(row["items_json"] or "[]")
        except Exception:
            items = []
        return items if isinstance(items, list) else []

    def _discover_seed(self, album: sqlite3.Row) -> tuple[Optional[str], Optional[str]]:
        # ListenBrainz radio currently supports artist seeds reliably. Keep release-group IDs cached for later modes.
        artist_id = str(album["mb_artist_id"] or "").strip()
        if artist_id:
            return "artist", artist_id
        return None, None

    def _store_album_discover(
        self,
        album_key: str,
        seed_type: str | None,
        seed_id: str | None,
        items: list[dict[str, Any]],
        error: str | None,
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO album_discover (album_key, seed_type, seed_id, items_json, updated_at, attempted_at, error)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(album_key) DO UPDATE SET
                    seed_type=excluded.seed_type,
                    seed_id=excluded.seed_id,
                    items_json=excluded.items_json,
                    updated_at=CASE WHEN excluded.error IS NULL THEN excluded.updated_at ELSE album_discover.updated_at END,
                    attempted_at=excluded.attempted_at,
                    error=excluded.error
                """,
                (album_key, seed_type, seed_id, json.dumps(items[:6], ensure_ascii=False), now, now, error),
            )
        return {"items": items[:6], "source": seed_type, "seed_id": seed_id, "cached": False, "error": error}

    def _fetch_listenbrainz_discover_items(self, seed_type: str | None, seed_id: str, seed_artist_name: str = "") -> list[dict[str, Any]]:
        """Return similar-artist items for an artist MusicBrainz seed.

        Delegates to the shared enrichment owner so the same canonical artist's
        similar set is fetched once and cached per MBID instead of per album.
        """
        if seed_type != "artist" or not seed_id:
            return []
        return self.artist_enrichment.similar_artists(str(seed_id), seed_artist_name or "")

    def _listenbrainz_recording_metadata(self, recording_ids: list[str]) -> dict[str, dict[str, str]]:
        ids = [str(item or "").strip() for item in recording_ids if str(item or "").strip()]
        if not ids:
            return {}
        payload = self._request_json(
            f"{LISTENBRAINZ_API}/metadata/recording/",
            {"recording_mbids": ",".join(ids[:25])},
        )
        results: dict[str, dict[str, str]] = {}
        for recording_id, item in (payload or {}).items():
            if not isinstance(item, dict):
                continue
            recording = item.get("recording") or {}
            title = str(recording.get("name") or "").strip()
            artist = self._recording_artist_name(recording)
            if title:
                results[str(recording_id)] = {"title": title, "artist": artist}
        return results

    @staticmethod
    def _recording_artist_name(recording: dict[str, Any]) -> str:
        artists = []
        for relation in recording.get("rels") or []:
            if not isinstance(relation, dict):
                continue
            if relation.get("type") not in {"artist", "performer", "vocal", "instrument"}:
                continue
            name = str(relation.get("artist_name") or "").strip()
            if name and name != "[unknown]" and name not in artists:
                artists.append(name)
            if len(artists) >= 2:
                break
        return " / ".join(artists)

    def get_cached_track(self, rel_path: str, mtime_ns: int, size_bytes: int) -> Optional[dict[str, Any]]:
        rel_path = str(rel_path or "").strip()
        if not rel_path:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM tracks
                WHERE rel_path = ? AND mtime_ns = ? AND size_bytes = ? AND missing_since IS NULL
                """,
                (rel_path, int(mtime_ns), int(size_bytes)),
            ).fetchone()
        if not row:
            return None
        return dict(row)

    def get_last_known_track(self, rel_path: str) -> Optional[dict[str, Any]]:
        """Return the most recent cache row for a relative path, if any.

        Unlike :meth:`get_cached_track` this is not fingerprinted by
        mtime/size: a changed file (cache miss) still matches its previous
        row, so a rescan can carry persisted state (e.g. the favorite flag)
        forward onto the rebuilt track instead of silently dropping it.
        """
        rel_path = str(rel_path or "").strip()
        if not rel_path:
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT * FROM tracks
                    WHERE rel_path = ? AND missing_since IS NULL
                    ORDER BY last_seen_at DESC
                    LIMIT 1
                    """,
                    (rel_path,),
                ).fetchone()
        except sqlite3.Error as exc:
            logger.warning("Last-known track read failed for %s: %s", rel_path, exc)
            return None
        return dict(row) if row else None

    def upsert_track_metadata(self, payload: dict[str, Any]) -> None:
        rel_path = str(payload.get("rel_path") or "").strip()
        track_id = str(payload.get("track_id") or "").strip()
        title = str(payload.get("title") or "").strip()
        if not rel_path or not track_id or not title:
            return
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tracks (
                    rel_path, track_id, mtime_ns, size_bytes,
                    title, artist, album, album_artist, genre, year,
                    track_number, disc_number, duration, sample_rate_hz,
                    last_seen_at, missing_since
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(rel_path) DO UPDATE SET
                    track_id=excluded.track_id,
                    mtime_ns=excluded.mtime_ns,
                    size_bytes=excluded.size_bytes,
                    title=excluded.title,
                    artist=excluded.artist,
                    album=excluded.album,
                    album_artist=excluded.album_artist,
                    genre=excluded.genre,
                    year=excluded.year,
                    track_number=excluded.track_number,
                    disc_number=excluded.disc_number,
                    duration=excluded.duration,
                    sample_rate_hz=excluded.sample_rate_hz,
                    last_seen_at=excluded.last_seen_at,
                    missing_since=NULL
                """,
                (
                    rel_path,
                    track_id,
                    int(payload.get("mtime_ns") or 0),
                    int(payload.get("size_bytes") or 0),
                    title,
                    payload.get("artist"),
                    payload.get("album"),
                    payload.get("album_artist"),
                    payload.get("genre"),
                    payload.get("year"),
                    payload.get("track_number"),
                    payload.get("disc_number"),
                    payload.get("duration"),
                    payload.get("sample_rate_hz"),
                    now,
                ),
            )

    def sync_tracks_seen(self, rel_paths: Iterable[str]) -> None:
        active = [str(path or "").strip() for path in rel_paths if str(path or "").strip()]
        now = _utc_now()
        with self._connect() as conn:
            if active:
                placeholders = ",".join("?" for _ in active)
                conn.execute(
                    f"UPDATE tracks SET last_seen_at = ?, missing_since = NULL WHERE rel_path IN ({placeholders})",
                    [now, *active],
                )
                conn.execute(
                    f"UPDATE tracks SET missing_since = COALESCE(missing_since, ?) WHERE rel_path NOT IN ({placeholders}) AND missing_since IS NULL",
                    [now, *active],
                )

            cutoff = time.time() - MISSING_RETENTION_SECONDS
            stale_rows = conn.execute("SELECT rel_path, missing_since FROM tracks WHERE missing_since IS NOT NULL").fetchall()
            for row in stale_rows:
                try:
                    missing_ts = datetime.fromisoformat(str(row["missing_since"]).replace("Z", "+00:00")).timestamp()
                except Exception:
                    continue
                if missing_ts <= cutoff:
                    conn.execute("DELETE FROM tracks WHERE rel_path = ?", (row["rel_path"],))

    def set_track_favorite(self, track_id: str, favorite: bool) -> dict[str, Any]:
        """Persist a local track's favorite state without touching play stats."""
        track_id = str(track_id or "").strip()
        if not track_id:
            return {"track_id": track_id, "favorite": bool(favorite)}
        with self._connect() as conn:
            conn.execute(
                "UPDATE tracks SET favorite = ? WHERE track_id = ? AND missing_since IS NULL",
                (1 if favorite else 0, track_id),
            )
            row = conn.execute(
                "SELECT track_id, favorite FROM tracks WHERE track_id = ? AND missing_since IS NULL",
                (track_id,),
            ).fetchone()
        if row:
            return {"track_id": row["track_id"], "favorite": bool(row["favorite"])}
        return {"track_id": track_id, "favorite": bool(favorite)}

    def increment_track_play_count(self, track_id: str) -> None:
        track_id = str(track_id or "").strip()
        if not track_id:
            return
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE tracks
                SET play_count = COALESCE(play_count, 0) + 1,
                    last_played_at = ?
                WHERE track_id = ? AND missing_since IS NULL
                """,
                (now, track_id),
            )

    def get_top_tracks(self, limit: int = 20) -> list[dict[str, Any]]:
        try:
            safe_limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            safe_limit = 20
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT track_id, play_count, last_played_at, favorite
                FROM tracks
                WHERE missing_since IS NULL
                  AND (favorite = 1 OR COALESCE(play_count, 0) > 0)
                ORDER BY favorite DESC, play_count DESC, COALESCE(last_played_at, '') DESC, title COLLATE NOCASE
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def external_cover_path(self, album_key: str) -> Optional[Path]:
        with self._connect() as conn:
            row = conn.execute("SELECT external_cover_path FROM albums WHERE album_key = ?", (album_key,)).fetchone()
        if not row or not row["external_cover_path"]:
            return None
        path = Path(str(row["external_cover_path"]))
        return path if path.is_file() else None

    def _row_to_api(self, row: sqlite3.Row | None) -> dict[str, Any]:
        if not row:
            return {}
        try:
            genres = json.loads(row["genres_json"] or "[]")
        except Exception:
            genres = []
        return {
            "release_type": row["release_type"],
            "year": row["year"],
            "country": row["country"],
            "label": _useful_label(row["label"]),
            "genres": genres if isinstance(genres, list) else [],
            "favorite": bool(row["favorite"]),
            "artist_description": row["artist_description"],
            "album_description": row["album_description"],
            "mb_artist_id": row["mb_artist_id"],
            "mb_release_id": row["mb_release_id"],
            "mb_release_group_id": row["mb_release_group_id"],
            "has_external_cover": bool(row["external_cover_path"] and Path(str(row["external_cover_path"])).is_file()),
        }

    def _mark_attempt(self, album_key: str, error: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE albums SET fetch_attempted_at = ?, fetch_error = ? WHERE album_key = ?",
                (_utc_now(), error, album_key),
            )

    def _enrich_album(self, row: sqlite3.Row) -> None:
        album = str(row["album"])
        artist = str(row["artist"])
        match = self._find_musicbrainz_release(album, artist)
        if not match:
            self._mark_attempt(row["album_key"], "no safe MusicBrainz match")
            return

        # Share the canonical MusicBrainz release + the library album mapping
        # through the shared enrichment owner so streaming providers reuse the
        # same release row instead of duplicating it.  The library's own albums
        # table stays the compatibility copy.
        self.artist_enrichment.record_provider_release(
            "library", str(row["album_key"]), album, artist, match
        )

        cover_path, cover_mime = (None, None)
        if not row["local_cover_source"]:
            cover_path, cover_mime = self._fetch_cover(
                row["album_key"],
                match.get("mb_release_id"),
                match.get("mb_release_group_id"),
            )
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE albums SET
                    mb_artist_id = ?,
                    mb_release_id = ?,
                    mb_release_group_id = ?,
                    release_type = ?,
                    year = ?,
                    country = ?,
                    label = ?,
                    genres_json = ?,
                    artist_description = COALESCE(?, artist_description),
                    album_description = COALESCE(?, album_description),
                    external_cover_path = COALESCE(?, external_cover_path),
                    external_cover_mime = COALESCE(?, external_cover_mime),
                    metadata_updated_at = ?,
                    about_attempted_at = ?,
                    fetch_attempted_at = ?,
                    fetch_error = NULL
                WHERE album_key = ?
                """,
                (
                    match.get("mb_artist_id"),
                    match.get("mb_release_id"),
                    match.get("mb_release_group_id"),
                    match.get("release_type"),
                    match.get("year"),
                    match.get("country"),
                    match.get("label"),
                    _json_list(match.get("genres")),
                    match.get("artist_description"),
                    match.get("album_description"),
                    str(cover_path) if cover_path else None,
                    cover_mime,
                    _utc_now(),
                    _utc_now(),
                    _utc_now(),
                    row["album_key"],
                ),
            )

    def _request_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._rate_limit()
        response = safe_get(
            url,
            params=params,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=12,
            max_bytes=ENRICHMENT_JSON_MAX_BYTES,
        )
        if response.status_code == 404:
            return {}
        response.raise_for_status()
        return response.json()

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < 1.1:
            time.sleep(1.1 - elapsed)
        self._last_request_at = time.monotonic()

    def _find_musicbrainz_release(self, album: str, artist: str) -> Optional[dict[str, Any]]:
        """Match a release through the shared enrichment owner.

        Compatibility wrapper for the library smart-metadata batch enrich path;
        returns ``None`` when no safe MusicBrainz release match exists so the
        caller records the usual "no safe MusicBrainz match" attempt.
        """
        match = self.artist_enrichment.match_release(album, artist, include_descriptions=True)
        if not match.get("mb_release_id"):
            return None
        return match

    def _fetch_artist_description(self, artist_id: Any) -> Optional[str]:
        artist_id = str(artist_id or "").strip()
        if not artist_id:
            return None
        return self.artist_enrichment.artist_description(artist_id)

    def _fetch_release_group_description(self, release_group_id: Any, payload: dict[str, Any] | None = None) -> Optional[str]:
        return self.artist_enrichment._release_group_description(release_group_id, payload)

    def _fetch_cover(
        self,
        album_key: str,
        release_id: str | None,
        release_group_id: str | None = None,
    ) -> tuple[Optional[Path], Optional[str]]:
        if not release_id and not release_group_id:
            return None, None
        suffix = ".jpg"
        destination = self.cover_dir / f"{album_key}{suffix}"
        if destination.is_file():
            return destination, "image/jpeg"

        candidates = []
        if release_id:
            candidates.append(f"{COVER_ART_API}/release/{quote(str(release_id))}/front-500")
        if release_group_id:
            candidates.append(f"{COVER_ART_API}/release-group/{quote(str(release_group_id))}/front-500")

        response = None
        for url in candidates:
            self._rate_limit()
            response = safe_get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "image/*"},
                timeout=10,
                max_bytes=COVER_ART_MAX_BYTES,
            )
            if response.status_code == 404:
                continue
            response.raise_for_status()
            break
        else:
            return None, None

        if response is None:
            return None, None
        content_type = response.headers.get("content-type") or "image/jpeg"
        if "png" in content_type:
            suffix = ".png"
        elif "webp" in content_type:
            suffix = ".webp"
        destination = self.cover_dir / f"{album_key}{suffix}"
        tmp = destination.with_suffix(destination.suffix + ".tmp")
        tmp.write_bytes(response.content)
        tmp.replace(destination)
        return destination, content_type.split(";")[0]
