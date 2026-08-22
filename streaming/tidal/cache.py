# SPDX-License-Identifier: AGPL-3.0-only

"""Persistent cache for the last successful TIDAL library/browse state.

The slow part of opening the TIDAL tab is the live catalog walk (favorited
tracks/albums/artists, playlist merge, favorite-id collection), which hits the
TIDAL API page by page.  This store keeps the last *successfully fetched*
payloads in SQLite under the FXRoute config directory — the same persistence
family as ``library-metadata.sqlite`` / ``artist-enrichment.sqlite`` — so the
UI can render the last-known library instantly and refresh in the background.

Entries are keyed by the TIDAL user id: data of one account is never served
for another.  Only browse/library payloads live here (favorite ids, favorite
lists per category, playlists); playback state, Connect/session live state and
search results are explicitly out of scope.  Failed refreshes never clear or
overwrite entries — a cache row is only replaced by a successful fetch.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# Cache kinds mirroring the provider's browse surfaces. ``ids`` is the
# canonical favorited-id state (heart source); the others are the rendered
# list payloads served by the favorites/playlists endpoints.
CACHE_KINDS = ("ids", "tracks", "albums", "artists", "playlists")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "fxroute"


class TidalLibraryCache:
    """SQLite-backed store of the last successful TIDAL browse payloads."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else (_config_dir() / "tidal-cache.sqlite")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=15)
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
                CREATE TABLE IF NOT EXISTS library_cache (
                    user_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, kind)
                )
                """
            )

    # -- single-kind access -------------------------------------------------

    def get(self, user_id: str, kind: str) -> Any | None:
        """Return the last successfully fetched payload for ``kind`` or None.

        Returns the decoded payload (list or dict as stored); a corrupted row
        is treated as missing rather than raised.
        """
        if not user_id or kind not in CACHE_KINDS:
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT payload_json FROM library_cache WHERE user_id = ? AND kind = ?",
                    (user_id, kind),
                ).fetchone()
        except sqlite3.Error as exc:
            logger.warning("TIDAL cache read failed for %s/%s: %s", user_id, kind, exc)
            return None
        if row is None:
            return None
        try:
            return json.loads(row["payload_json"])
        except (TypeError, ValueError) as exc:
            logger.warning("TIDAL cache row %s/%s is corrupted: %s", user_id, kind, exc)
            return None

    def put(self, user_id: str, kind: str, payload: Any) -> None:
        """Replace the cache row for ``kind`` with a successful payload.

        Rows are only ever written here (never deleted on failure); a missing
        user id (unknown account) is a no-op.
        """
        if not user_id or kind not in CACHE_KINDS:
            return
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO library_cache (user_id, kind, payload_json, fetched_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(user_id, kind) DO UPDATE SET
                        payload_json = excluded.payload_json,
                        fetched_at = excluded.fetched_at
                    """,
                    (user_id, kind, json.dumps(payload), _utc_now()),
                )
        except sqlite3.Error as exc:
            logger.warning("TIDAL cache write failed for %s/%s: %s", user_id, kind, exc)

    # -- snapshot (whole library for one account) ---------------------------

    def snapshot(self, user_id: str) -> dict | None:
        """Return every cached kind for one account, or None when empty.

        The returned dict uses the flat snapshot shape served by
        ``/api/streaming/tidal/library/snapshot``: ``ids`` is a dict of id
        lists, each list kind is a list, and ``fetched_at`` is the newest
        fetch time across kinds.
        """
        if not user_id:
            return None
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT kind, payload_json, fetched_at FROM library_cache WHERE user_id = ?",
                    (user_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("TIDAL cache snapshot failed for %s: %s", user_id, exc)
            return None
        if not rows:
            return None
        out: dict[str, Any] = {
            "user_id": user_id,
            "fetched_at": None,
            "ids": {},
            "tracks": [],
            "albums": [],
            "artists": [],
            "playlists": [],
        }
        newest: str | None = None
        for row in rows:
            kind = row["kind"]
            if kind not in CACHE_KINDS:
                continue
            try:
                out[kind] = json.loads(row["payload_json"])
            except (TypeError, ValueError) as exc:
                logger.warning("TIDAL cache row %s/%s is corrupted: %s", user_id, kind, exc)
                continue
            fetched = row["fetched_at"] or ""
            if fetched and (newest is None or fetched > newest):
                newest = fetched
        out["fetched_at"] = newest
        if not any(out[kind] for kind in CACHE_KINDS):
            return None
        return out

    def delete(self, user_id: str, kind: str) -> None:
        """Drop one kind's row so the next successful fetch repopulates it.

        Used by playlist writes: after creating a playlist or adding tracks the
        cached playlist list is stale, and the next live ``user_playlists``
        fetch (which caches again on success) serves the fresh state.
        """
        if not user_id or kind not in CACHE_KINDS:
            return
        try:
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM library_cache WHERE user_id = ? AND kind = ?",
                    (user_id, kind),
                )
        except sqlite3.Error as exc:
            logger.warning("TIDAL cache delete failed for %s/%s: %s", user_id, kind, exc)

    def clear_user(self, user_id: str) -> None:
        """Remove every cache row of one account (logout/account switch)."""
        if not user_id:
            return
        try:
            with self._connect() as conn:
                conn.execute("DELETE FROM library_cache WHERE user_id = ?", (user_id,))
        except sqlite3.Error as exc:
            logger.warning("TIDAL cache clear failed for %s: %s", user_id, exc)


# Shared instance; the provider/API/catalog import this singleton.
library_cache = TidalLibraryCache()
