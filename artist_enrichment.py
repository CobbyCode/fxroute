# SPDX-License-Identifier: AGPL-3.0-only

"""Shared artist enrichment owner (MusicBrainz / Wikidata / Wikipedia / ListenBrainz).

Library albums and streaming providers (TIDAL today, Qobuz later) both resolve
a *provider artist* to a canonical MusicBrainz artist and, from that, to curated
"about" text and "similar artists" suggestions.  This module owns that generic
enrichment pipeline and its SQLite persistence, so the same artist enrichment is
never fetched again or stored a second time per album or per provider.

Pipeline
--------

* provider artist name (+ optional provider album titles) -> canonical MB artist
* canonical MB artist (MBID) -> about text + similar-artist items

Persistence
-----------

``ArtistEnrichmentStore`` keeps three tables:

* ``artists``: one row per canonical MusicBrainz artist (MBID) holding the cached
  about text, similar-artist items and attempt/refresh timestamps/errors so the
  retry/cooldown behaviour matches the library smart-metadata cache.
* ``provider_artists``: the provider->canonical mapping (``provider`` +
  ``provider_artist_id`` -> ``mb_artist_id``) with match state and timestamps, so
  a confidently matched artist is never re-matched and an ambiguous/unmatched one
  is only retried after a cooldown.  The same table drives the reverse lookup
  (MBID -> provider artist id) used to jump from a similar artist straight to the
  provider artist without a search.
* ``releases``: cached MusicBrainz release supplements (release type, country,
  label, genres) keyed by normalized album + artist, so provider album views do
  not re-query the network per album.

Network access is rate limited.  A consumer that owns its request boundary (the
library metadata store patches its ``_request_json`` in tests) can inject itself
as ``request_backend``; the service then dispatches every network call through
that backend at call time instead of keeping its own requests path.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

MUSICBRAINZ_API = "https://musicbrainz.org/ws/2"
WIKIDATA_API = "https://www.wikidata.org/wiki/Special:EntityData"
WIKIPEDIA_SUMMARY_APIS = {
    "enwiki": "https://en.wikipedia.org/api/rest_v1/page/summary",
    "dewiki": "https://de.wikipedia.org/api/rest_v1/page/summary",
}
LISTENBRAINZ_API = "https://api.listenbrainz.org/1"
USER_AGENT = "FXRoute/0.6 (https://github.com/CobbyCode/fxroute)"
FETCH_COOLDOWN_SECONDS = 7 * 24 * 60 * 60
TRANSIENT_ERROR_RETRY_SECONDS = 60 * 60
DISCOVER_COOLDOWN_SECONDS = 7 * 24 * 60 * 60
# Cooldown for an artist whose summary could not be found (not a transient
# error): do not hammer Wikipedia/Wikidata for the same artist on every request.
MISSING_DESCRIPTION_COOLDOWN = FETCH_COOLDOWN_SECONDS
SIMILAR_MAX_ITEMS = 8
MS_MATCH_SCORE_MIN = 90

# Cross-instance rate limiting: library store and standalone provider instances
# share one clock so simultaneous enrichment jobs stay below the MusicBrainz
# politeness envelope, even though each service may own its own request path.
_GLOBAL_RATE_LOCK = threading.Lock()
_GLOBAL_LAST_REQUEST_AT = 0.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _config_dir() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return root / "fxroute"


def _shared_enrichment_db_path() -> Path:
    return _config_dir() / "artist-enrichment.sqlite"


def _derive_enrichment_db_path(store_db_path: Path) -> Path:
    """Pick the enrichment DB for a library metadata store.

    The default production store shares the single global enrichment DB so the
    library and streaming providers see the same cache.  Any custom/test store
    gets an isolated sibling so tests never touch the real enrichment DB.
    """
    store_path = Path(store_db_path)
    if str(store_path) == str(_config_dir() / "library-metadata.sqlite"):
        return _shared_enrichment_db_path()
    return store_path.parent / "artist-enrichment.sqlite"


def _normalize_text(value: str) -> str:
    text = (value or "").lower()
    text = re.sub(r"\([^)]*\)|\[[^]]*\]", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _json_list(values: Any) -> str:
    if not isinstance(values, list):
        return "[]"
    cleaned = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        key = text.lower()
        if text and key not in seen:
            cleaned.append(text)
            seen.add(key)
    return json.dumps(cleaned[:6], ensure_ascii=False)


def _useful_label(value: Any) -> Optional[str]:
    label = str(value or "").strip()
    if not label:
        return None
    if label.lower() in {"[no label]", "no label", "none", "unknown"}:
        return None
    return label


def _compact_description(text: str) -> Optional[str]:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        return None
    sentences = re.split(r"(?<=[.!?])\s+", text)
    compact = " ".join(sentence for sentence in sentences[:2] if sentence).strip()
    if len(compact) > 320:
        compact = compact[:317].rsplit(" ", 1)[0].rstrip(".,;:") + "..."
    return compact or None


def _wikidata_id_from_relations(relations: list[Any]) -> Optional[str]:
    for relation in relations:
        if not isinstance(relation, dict):
            continue
        url = relation.get("url") or {}
        resource = str(url.get("resource") or "")
        match = re.search(r"wikidata\.org/wiki/(Q\d+)", resource)
        if match:
            return match.group(1)
    return None


def _release_key(album: str, artist: str) -> str:
    return f"{_normalize_text(album)}::{_normalize_text(artist)}"


def _iso_timestamp(value: Any) -> Optional[float]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


class ArtistEnrichmentStore:
    """SQLite-backed cache for canonical artists, provider mappings and releases."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path) if db_path is not None else _shared_enrichment_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass  # WAL is best-effort; the DB still works
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
                CREATE TABLE IF NOT EXISTS artists (
                    mb_artist_id TEXT PRIMARY KEY,
                    name TEXT,
                    description TEXT,
                    description_attempted_at TEXT,
                    description_error TEXT,
                    similar_json TEXT DEFAULT '[]',
                    similar_attempted_at TEXT,
                    similar_error TEXT,
                    updated_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_artists (
                    provider TEXT NOT NULL,
                    provider_artist_id TEXT NOT NULL,
                    name TEXT,
                    art_url TEXT,
                    mb_artist_id TEXT,
                    canonical_name TEXT,
                    match_state TEXT,
                    attempted_at TEXT,
                    matched_at TEXT,
                    error TEXT,
                    PRIMARY KEY (provider, provider_artist_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS releases (
                    release_key TEXT PRIMARY KEY,
                    album TEXT,
                    artist TEXT,
                    mb_artist_id TEXT,
                    mb_release_id TEXT,
                    mb_release_group_id TEXT,
                    release_type TEXT,
                    year INTEGER,
                    country TEXT,
                    label TEXT,
                    genres_json TEXT DEFAULT '[]',
                    artist_description TEXT,
                    album_description TEXT,
                    attempted_at TEXT,
                    error TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_provider_artists_mb ON provider_artists(provider, mb_artist_id)")

    # -- artists (canonical MB artist, about + similar caches) ----------------

    def get_artist(self, mb_artist_id: str) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM artists WHERE mb_artist_id = ?", (mb_artist_id,)
            ).fetchone()

    def upsert_artist_description(
        self,
        mb_artist_id: str,
        name: Optional[str],
        description: Optional[str],
        *,
        attempted_at: str,
        error: Optional[str],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO artists (mb_artist_id, name, description, description_attempted_at, description_error, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(mb_artist_id) DO UPDATE SET
                    name = COALESCE(excluded.name, artists.name),
                    description = COALESCE(excluded.description, artists.description),
                    description_attempted_at = excluded.description_attempted_at,
                    description_error = excluded.description_error,
                    updated_at = excluded.updated_at
                """,
                (mb_artist_id, name, description, attempted_at, error, attempted_at),
            )

    def upsert_artist_similar(
        self,
        mb_artist_id: str,
        items: list[dict[str, Any]],
        *,
        attempted_at: str,
        error: Optional[str],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO artists (mb_artist_id, similar_json, similar_attempted_at, similar_error, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(mb_artist_id) DO UPDATE SET
                    similar_json = excluded.similar_json,
                    similar_attempted_at = excluded.similar_attempted_at,
                    similar_error = excluded.similar_error,
                    updated_at = excluded.updated_at
                """,
                (
                    mb_artist_id,
                    json.dumps(items[:SIMILAR_MAX_ITEMS], ensure_ascii=False),
                    attempted_at,
                    error,
                    attempted_at,
                ),
            )

    # -- provider artists (canonical mapping + reverse lookup) ----------------

    def get_provider_artist(self, provider: str, provider_artist_id: str) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM provider_artists WHERE provider = ? AND provider_artist_id = ?",
                (provider, provider_artist_id),
            ).fetchone()

    def set_provider_artist(
        self,
        provider: str,
        provider_artist_id: str,
        *,
        name: Optional[str],
        art_url: Optional[str],
        mb_artist_id: Optional[str],
        canonical_name: Optional[str],
        match_state: str,
        attempted_at: str,
        matched_at: Optional[str],
        error: Optional[str],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO provider_artists (
                    provider, provider_artist_id, name, art_url, mb_artist_id,
                    canonical_name, match_state, attempted_at, matched_at, error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, provider_artist_id) DO UPDATE SET
                    name = excluded.name,
                    art_url = CASE
                        WHEN excluded.art_url IS NOT NULL AND excluded.art_url != '' THEN excluded.art_url
                        ELSE provider_artists.art_url
                    END,
                    mb_artist_id = excluded.mb_artist_id,
                    canonical_name = COALESCE(excluded.canonical_name, provider_artists.canonical_name),
                    match_state = excluded.match_state,
                    attempted_at = excluded.attempted_at,
                    matched_at = COALESCE(excluded.matched_at, provider_artists.matched_at),
                    error = excluded.error
                """,
                (
                    provider,
                    provider_artist_id,
                    name,
                    art_url,
                    mb_artist_id,
                    canonical_name,
                    match_state,
                    attempted_at,
                    matched_at,
                    error,
                ),
            )

    def reverse_provider_by_mbid(self, provider: str, mb_artist_id: str) -> Optional[sqlite3.Row]:
        """Return one confident provider mapping for a canonical MBID.

        Used to jump from a similar artist (which carries a MusicBrainz ID) to
        the provider artist id without running a provider search.
        """
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT * FROM provider_artists
                WHERE provider = ? AND mb_artist_id = ? AND match_state = 'mapped'
                ORDER BY matched_at DESC
                LIMIT 1
                """,
                (provider, mb_artist_id),
            ).fetchone()

    def reverse_provider_by_name(self, provider: str, name: str) -> Optional[sqlite3.Row]:
        """Return the single confident provider mapping for a normalized name.

        Name-based reverse lookup is only safe when exactly one distinct provider
        artist is confidently mapped under that name; ambiguous names must not be
        guessed.  Always prefer ``reverse_provider_by_mbid`` when an MBID exists.
        """
        name_key = _normalize_text(name)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT provider_artist_id, name FROM provider_artists
                WHERE provider = ? AND match_state = 'mapped'
                """,
                (provider,),
            ).fetchall()
        matched_ids = {
            str(row["provider_artist_id"])
            for row in rows
            if _normalize_text(row["name"]) == name_key
        }
        if len(matched_ids) != 1:
            return None
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT * FROM provider_artists
                WHERE provider = ? AND provider_artist_id = ? AND match_state = 'mapped'
                """,
                (provider, next(iter(matched_ids))),
            ).fetchone()

    # -- releases (album supplement cache) ------------------------------------

    def get_release(self, release_key: str) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM releases WHERE release_key = ?", (release_key,)
            ).fetchone()

    def set_release(
        self,
        release_key: str,
        *,
        album: str,
        artist: str,
        data: dict[str, Any],
        attempted_at: str,
        error: Optional[str],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO releases (
                    release_key, album, artist, mb_artist_id, mb_release_id,
                    mb_release_group_id, release_type, year, country, label,
                    genres_json, artist_description, album_description,
                    attempted_at, error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(release_key) DO UPDATE SET
                    album = excluded.album,
                    artist = excluded.artist,
                    mb_artist_id = excluded.mb_artist_id,
                    mb_release_id = excluded.mb_release_id,
                    mb_release_group_id = excluded.mb_release_group_id,
                    release_type = excluded.release_type,
                    year = excluded.year,
                    country = excluded.country,
                    label = excluded.label,
                    genres_json = excluded.genres_json,
                    artist_description = excluded.artist_description,
                    album_description = excluded.album_description,
                    attempted_at = excluded.attempted_at,
                    error = excluded.error
                """,
                (
                    release_key,
                    album,
                    artist,
                    data.get("mb_artist_id"),
                    data.get("mb_release_id"),
                    data.get("mb_release_group_id"),
                    data.get("release_type"),
                    data.get("year"),
                    data.get("country"),
                    data.get("label"),
                    _json_list(data.get("genres")),
                    data.get("artist_description"),
                    data.get("album_description"),
                    attempted_at,
                    error,
                ),
            )


class ArtistEnrichmentService:
    """Retrieve and cache canonical artist enrichment for any provider.

    ``request_backend`` is an optional object exposing ``_request_json(url,
    params)``.  When given, every network call is dispatched through it at call
    time (the library metadata store injects itself so its patched request
    boundary stays authoritative in tests).
    """

    def __init__(self, db_path: Path | None = None, request_backend: Any = None):
        self.store = ArtistEnrichmentStore(db_path=db_path)
        self._backend = request_backend

    # -- request path ---------------------------------------------------------

    def _request_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._backend is not None:
            return self._backend._request_json(url, params)
        self._rate_limit()
        response = requests.get(
            url,
            params=params,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=12,
        )
        if response.status_code == 404:
            return {}
        response.raise_for_status()
        return response.json()

    def _rate_limit(self) -> None:
        global _GLOBAL_LAST_REQUEST_AT
        with _GLOBAL_RATE_LOCK:
            elapsed = time.monotonic() - _GLOBAL_LAST_REQUEST_AT
            if elapsed < 1.1:
                time.sleep(1.1 - elapsed)
            _GLOBAL_LAST_REQUEST_AT = time.monotonic()

    # -- public entry points --------------------------------------------------

    def enriched_artist(
        self,
        provider: str,
        provider_artist_id: str,
        name: str,
        *,
        art_url: str = "",
        album_titles: list[str] | None = None,
        load_similar: bool = True,
    ) -> dict[str, Any]:
        """Resolve a provider artist and return cached about + similar data.

        Never raises for enrichment problems: network/matching errors degrade to
        an ``available=False`` result so the provider's own data still renders.
        """
        provider = (provider or "").strip()
        provider_artist_id = str(provider_artist_id or "").strip()
        name = (name or "").strip()
        result: dict[str, Any] = {
            "provider": provider,
            "provider_artist_id": provider_artist_id,
            "name": name,
            "available": False,
            "match_state": "pending",
            "mb_artist_id": None,
            "canonical_name": None,
            "about": None,
            "similar": [],
            "cached": False,
            "error": None,
        }
        if not provider or not provider_artist_id or not name:
            result["match_state"] = "failed"
            result["error"] = "missing artist identity"
            return result

        mapping = self._resolve_mapping(
            provider, provider_artist_id, name, art_url=art_url, album_titles=album_titles
        )
        result["match_state"] = mapping.get("match_state", "unmatched")
        result["error"] = mapping.get("error")
        if mapping.get("match_state") != "mapped" or not mapping.get("mb_artist_id"):
            return result

        mb_artist_id = str(mapping["mb_artist_id"])
        result["available"] = True
        result["mb_artist_id"] = mb_artist_id
        result["canonical_name"] = mapping.get("canonical_name")
        result["cached"] = bool(mapping.get("cached"))
        result["about"] = self.artist_description(mb_artist_id, canonical_name=result["canonical_name"])
        if load_similar:
            result["similar"] = self._similar_with_mappings(provider, mb_artist_id, name)
        return result

    def enriched_album(
        self,
        provider: str,
        provider_artist_id: str,
        album_title: str,
        artist_name: str,
        *,
        art_url: str = "",
    ) -> dict[str, Any]:
        """Return the artist about plus additive MusicBrainz release metadata.

        The provider's own album fields stay untouched at the top level; the
        ``supplement`` dict only carries MusicBrainz fields the provider does
        not already expose (release type, country, label, genres).
        """
        artist = self.enriched_artist(
            provider,
            provider_artist_id,
            artist_name,
            art_url=art_url,
            album_titles=[album_title] if album_title else None,
            load_similar=False,
        )
        supplement: dict[str, Any] = {}
        if artist.get("available") and artist.get("mb_artist_id"):
            release = self._release_supplement(album_title, artist_name)
            for key in ("release_type", "country", "label", "genres"):
                value = release.get(key)
                if value in (None, "", []):
                    continue
                supplement[key] = value
        return {
            "available": artist.get("available") or bool(supplement),
            "artist": {
                "mb_artist_id": artist.get("mb_artist_id"),
                "about": artist.get("about"),
                "mapped": artist.get("available") is True,
                "cached": bool(artist.get("cached")),
            },
            "supplement": supplement,
            "error": artist.get("error"),
        }

    # -- provider artist -> canonical matching --------------------------------

    def _resolve_mapping(
        self,
        provider: str,
        provider_artist_id: str,
        name: str,
        *,
        art_url: str,
        album_titles: list[str] | None,
    ) -> dict[str, Any]:
        row = self.store.get_provider_artist(provider, provider_artist_id)
        now_ts = time.time()
        if row and row["mb_artist_id"] and row["match_state"] == "mapped":
            if art_url:
                self.store.set_provider_artist(
                    provider, provider_artist_id,
                    name=row["name"] or name, art_url=art_url,
                    mb_artist_id=row["mb_artist_id"], canonical_name=row["canonical_name"],
                    match_state="mapped", attempted_at=row["attempted_at"] or _utc_now(),
                    matched_at=row["matched_at"] or _utc_now(), error=None,
                )
            return {
                "match_state": "mapped",
                "mb_artist_id": row["mb_artist_id"],
                "canonical_name": row["canonical_name"],
                "cached": True,
                "error": None,
            }

        if row and row["attempted_at"]:
            age = now_ts - (_iso_timestamp(row["attempted_at"]) or 0)
            state = row["match_state"] or "failed"
            cooldown = FETCH_COOLDOWN_SECONDS if state in {"ambiguous", "unmatched"} else TRANSIENT_ERROR_RETRY_SECONDS
            if age < cooldown:
                return {
                    "match_state": state,
                    "mb_artist_id": row["mb_artist_id"],
                    "canonical_name": row["canonical_name"],
                    "cached": True,
                    "error": row["error"],
                }

        match = self._match_artist_by_name(name, album_titles=album_titles or [])
        now = _utc_now()
        matched_at = now if match["match_state"] == "mapped" else None
        self.store.set_provider_artist(
            provider, provider_artist_id,
            name=name, art_url=art_url,
            mb_artist_id=match.get("mb_artist_id"),
            canonical_name=match.get("canonical_name"),
            match_state=match["match_state"],
            attempted_at=now, matched_at=matched_at,
            error=match.get("error"),
        )
        match["cached"] = False
        return match

    def _match_artist_by_name(self, name: str, *, album_titles: list[str]) -> dict[str, Any]:
        """Match a provider artist name to a canonical MusicBrainz artist.

        Never blindly takes the first same-name result: candidates must score at
        least ``MS_MATCH_SCORE_MIN`` and normalize to the requested name.  When
        several distinct artists share the name, provider album titles are used
        (bounded release-group lookups) to pick the one; otherwise the match is
        reported as ambiguous so the UI shows no enrichment instead of the wrong
        artist's biography.
        """
        name_norm = _normalize_text(name)
        if not name_norm:
            return {"match_state": "failed", "mb_artist_id": None, "error": "missing artist name"}
        try:
            payload = self._request_json(
                f"{MUSICBRAINZ_API}/artist",
                {"query": f'artist:"{name}"', "fmt": "json", "limit": 8},
            )
        except Exception as exc:  # noqa: BLE001 - enrichment must degrade quietly
            return {"match_state": "failed", "mb_artist_id": None, "error": str(exc)[:200]}

        candidates = []
        for artist in payload.get("artists") or []:
            try:
                score = int(artist.get("score") or 0)
            except (TypeError, ValueError):
                continue
            if score < MS_MATCH_SCORE_MIN:
                continue
            artist_name = str(artist.get("name") or "").strip()
            if _normalize_text(artist_name) != name_norm:
                continue
            mbid = str(artist.get("id") or "").strip()
            if not mbid:
                continue
            candidates.append(
                {
                    "mb_artist_id": mbid,
                    "canonical_name": artist_name,
                    "disambiguation": str(artist.get("disambiguation") or "").strip(),
                }
            )

        if not candidates:
            return {
                "match_state": "unmatched",
                "mb_artist_id": None,
                "error": "no safe MusicBrainz match",
            }
        if len(candidates) == 1:
            return {"match_state": "mapped", **candidates[0], "error": None}

        # Several distinct artists share the name: disambiguate with the
        # provider's album titles (one bounded lookup per candidate) when
        # available; otherwise report ambiguity without inventing a mapping.
        album_keys = {_normalize_text(title) for title in album_titles if str(title or "").strip()}
        if album_keys:
            winners = []
            for candidate in candidates:
                try:
                    if self._candidate_matches_albums(candidate["mb_artist_id"], album_keys):
                        winners.append(candidate)
                except Exception:  # noqa: BLE001 - a lookup failure keeps the candidate unproven
                    continue
            if len(winners) == 1:
                return {"match_state": "mapped", **winners[0], "error": None}

        return {
            "match_state": "ambiguous",
            "mb_artist_id": None,
            "error": "ambiguous MusicBrainz match",
        }

    def _candidate_matches_albums(self, mb_artist_id: str, album_keys: set[str]) -> bool:
        """Return whether any of the requested release-group titles match."""
        payload = self._request_json(
            f"{MUSICBRAINZ_API}/release-group",
            {"artist": mb_artist_id, "fmt": "json", "limit": 50},
        )
        for group in payload.get("release-groups") or []:
            title = str(group.get("title") or "").strip()
            if title and _normalize_text(title) in album_keys:
                return True
        return False

    # -- about text -----------------------------------------------------------

    def artist_description(self, mb_artist_id: str, canonical_name: str | None = None) -> Optional[str]:
        """Return the cached Wikipedia/Wikidata artist summary for an MBID.

        A non-empty description is served from the cache without refreshing; an
        empty description is only retried after the cooldown has elapsed, so the
        same artist's bio is never re-fetched per request or per album.
        """
        mb_artist_id = str(mb_artist_id or "").strip()
        if not mb_artist_id:
            return None
        row = self.store.get_artist(mb_artist_id)
        if row and row["description"]:
            return row["description"]
        if row and row["description_attempted_at"]:
            age = time.time() - (_iso_timestamp(row["description_attempted_at"]) or 0)
            cooldown = MISSING_DESCRIPTION_COOLDOWN
            if age < cooldown:
                return None
        description = None
        error = None
        now = _utc_now()
        try:
            payload = self._request_json(
                f"{MUSICBRAINZ_API}/artist/{quote(mb_artist_id)}",
                {"fmt": "json", "inc": "url-rels"},
            )
            wikidata_id = _wikidata_id_from_relations(payload.get("relations") or [])
            description = self._wikipedia_summary_for_wikidata_id(wikidata_id)
            canonical_name = canonical_name or str(payload.get("name") or "").strip() or None
        except Exception as exc:  # noqa: BLE001 - enrichment must degrade quietly
            logger.debug("Artist description lookup failed for %s: %s", mb_artist_id, exc)
            error = str(exc)[:200]
        self.store.upsert_artist_description(
            mb_artist_id, canonical_name, description, attempted_at=now, error=error
        )
        return description

    def _wikipedia_summary_for_wikidata_id(self, wikidata_id: str | None) -> Optional[str]:
        if not wikidata_id:
            return None
        entity_data = self._request_json(f"{WIKIDATA_API}/{quote(wikidata_id)}.json")
        entity = (entity_data.get("entities") or {}).get(wikidata_id) or {}
        sitelinks = entity.get("sitelinks") or {}
        site_key = "enwiki" if sitelinks.get("enwiki") else "dewiki"
        site = sitelinks.get(site_key)
        title = str((site or {}).get("title") or "").strip()
        if not title:
            return None
        summary = self._request_json(f"{WIKIPEDIA_SUMMARY_APIS[site_key]}/{quote(title)}")
        extract = str(summary.get("extract") or "").strip()
        return _compact_description(extract)

    # -- similar artists ------------------------------------------------------

    def similar_artists(self, mb_artist_id: str, artist_name: str = "") -> list[dict[str, Any]]:
        """Return cached ListenBrainz similar-artist items for an MBID.

        Fresh cached items are served without a request; stale or missing items
        trigger one ListenBrainz radio lookup (the whole list at once — never a
        per-item request).  An empty/error result respects the discover cooldown.
        """
        mb_artist_id = str(mb_artist_id or "").strip()
        if not mb_artist_id:
            return []
        row = self.store.get_artist(mb_artist_id)
        now_ts = time.time()
        if row:
            items = _items_from_similar_json(row["similar_json"])
            attempted = _iso_timestamp(row["similar_attempted_at"])
            if items and attempted and now_ts - attempted < DISCOVER_COOLDOWN_SECONDS:
                return items
            if not items and attempted and now_ts - attempted < DISCOVER_COOLDOWN_SECONDS:
                return []

        items: list[dict[str, Any]] = []
        error = None
        now = _utc_now()
        try:
            items = self._fetch_listenbrainz_similar(mb_artist_id, artist_name)
        except Exception as exc:  # noqa: BLE001 - enrichment must degrade quietly
            logger.debug("ListenBrainz similar lookup failed for %s: %s", mb_artist_id, exc)
            error = str(exc)[:200]
        if items:
            self.store.upsert_artist_similar(mb_artist_id, items, attempted_at=now, error=None)
            return items
        self.store.upsert_artist_similar(mb_artist_id, [], attempted_at=now, error=error)
        return items

    def _fetch_listenbrainz_similar(self, mb_artist_id: str, seed_artist_name: str = "") -> list[dict[str, Any]]:
        payload: dict[str, Any] = {}
        for mode, max_artists in (("easy", 8), ("medium", 8), ("easy", 5)):
            try:
                payload = self._request_json(
                    f"{LISTENBRAINZ_API}/lb-radio/artist/{quote(mb_artist_id)}",
                    {
                        "mode": mode,
                        "max_similar_artists": max_artists,
                        "max_recordings_per_artist": 2,
                        "pop_begin": 0,
                        "pop_end": 100,
                    },
                )
                if payload:
                    break
            except Exception as exc:  # noqa: BLE001
                logger.debug("ListenBrainz radio lookup failed for %s mode=%s: %s", mb_artist_id, mode, exc)
                payload = {}
        results: list[dict[str, Any]] = []
        seen_artist_ids = {mb_artist_id.lower()}
        seen_artist_names: set[str] = set()
        seed_artist_key = _normalize_text(seed_artist_name)
        if seed_artist_key:
            seen_artist_names.add(seed_artist_key)
        for recordings in (payload or {}).values():
            if not isinstance(recordings, list):
                continue
            for item in recordings:
                if not isinstance(item, dict):
                    continue
                artist_id = str(item.get("similar_artist_mbid") or "").strip()
                artist_name = str(item.get("similar_artist_name") or "").strip()
                artist_key = artist_id.lower()
                name_key = _normalize_text(artist_name)
                if not artist_name:
                    continue
                if artist_key and artist_key in seen_artist_ids:
                    continue
                if (not artist_key and name_key in seen_artist_names) or name_key in seen_artist_names:
                    continue
                if artist_key:
                    seen_artist_ids.add(artist_key)
                if name_key:
                    seen_artist_names.add(name_key)
                results.append(
                    {
                        "type": "artist",
                        "artist": artist_name,
                        "artist_mbid": artist_id,
                        "listen_count": int(item.get("total_listen_count") or 0),
                    }
                )
                if len(results) >= SIMILAR_MAX_ITEMS:
                    return results
        return results

    def _similar_with_mappings(self, provider: str, mb_artist_id: str, artist_name: str) -> list[dict[str, Any]]:
        """Attach cached provider mappings to similar items without any request.

        A similar artist whose canonical MBID (or, failing that, whose unique
        normalized name) already maps to a provider artist gets its provider
        artist id and stored art URL, so the UI can jump straight to it.  No
        per-item enrichment is ever triggered here.
        """
        out = []
        for item in self.similar_artists(mb_artist_id, artist_name):
            enriched = dict(item)
            mapped = None
            if item.get("artist_mbid"):
                mapped = self.store.reverse_provider_by_mbid(provider, str(item["artist_mbid"]))
            if mapped is None and item.get("artist"):
                mapped = self.store.reverse_provider_by_name(provider, str(item["artist"]))
            if mapped is not None:
                enriched["provider_artist_id"] = mapped["provider_artist_id"]
                if mapped["art_url"]:
                    enriched["art_url"] = mapped["art_url"]
                if mapped["name"]:
                    enriched["artist"] = mapped["name"]
            out.append(enriched)
        return out

    # -- release supplement ---------------------------------------------------

    def _release_supplement(self, album: str, artist: str) -> dict[str, Any]:
        """Return additive MusicBrainz release fields for a provider album.

        Cached in the shared ``releases`` store keyed by normalized album +
        artist; the same (album, artist) is never re-queried per provider album.
        """
        album = str(album or "").strip()
        artist = str(artist or "").strip()
        key = _release_key(album, artist)
        if not album or not artist:
            return {}
        row = self.store.get_release(key)
        if row and row["mb_release_id"]:
            return _release_supplement_from_row(row)
        attempted = _iso_timestamp(row["attempted_at"]) if row else None
        if attempted:
            cooldown = FETCH_COOLDOWN_SECONDS if (row and row["error"] == "no safe MusicBrainz match") else TRANSIENT_ERROR_RETRY_SECONDS
            if time.time() - attempted < cooldown:
                return {}

        now = _utc_now()
        try:
            match = self.match_release(album, artist, include_descriptions=False)
            if match.get("mb_release_id"):
                self.store.set_release(key, album=album, artist=artist, data=match, attempted_at=now, error=None)
                return _release_supplement_from_mapping(match)
            if match.get("error"):
                self.store.set_release(key, album=album, artist=artist, data={}, attempted_at=now, error=match["error"])
                return {}
            self.store.set_release(key, album=album, artist=artist, data={}, attempted_at=now, error="no safe MusicBrainz match")
            return {}
        except Exception as exc:  # noqa: BLE001 - enrichment must degrade quietly
            logger.info("MusicBrainz release supplement failed for %s - %s: %s", artist, album, exc)
            self.store.set_release(key, album=album, artist=artist, data={}, attempted_at=now, error=str(exc)[:200])
            return {}

    def match_release(self, album: str, artist: str, *, include_descriptions: bool = True) -> dict[str, Any]:
        """Resolve a release by exact album/artist and enrich it.

        Mirrors the library smart-metadata release matcher: candidates must score
        at least 90 and normalize to the requested album/artist credit.  Returns
        the shared release field set (MBIDs, release type, year, country, label,
        genres) plus the artist/album descriptions when requested.
        """
        album = str(album or "").strip()
        artist = str(artist or "").strip()
        query = f'release:"{album}" AND artist:"{artist}"'
        try:
            payload = self._request_json(
                f"{MUSICBRAINZ_API}/release",
                {"query": query, "fmt": "json", "limit": 5},
            )
        except Exception as exc:  # noqa: BLE001 - enrichment must degrade quietly
            return {"error": str(exc)[:200]}
        releases = payload.get("releases") or []
        album_norm = _normalize_text(album)
        artist_norm = _normalize_text(artist)
        for release in releases:
            try:
                score = int(release.get("score") or 0)
            except (TypeError, ValueError):
                continue
            title_norm = _normalize_text(str(release.get("title") or ""))
            credit = " ".join(
                str(item.get("name") or "")
                for item in (release.get("artist-credit") or [])
                if isinstance(item, dict)
            )
            credit_norm = _normalize_text(credit)
            if score < MS_MATCH_SCORE_MIN or title_norm != album_norm:
                continue
            if artist_norm and artist_norm not in credit_norm and credit_norm not in artist_norm:
                continue
            release_id = release.get("id")
            if not release_id:
                continue
            return self._lookup_release(str(release_id), release, include_descriptions=include_descriptions)
        return {"error": "no safe MusicBrainz match"}

    def _lookup_release(
        self,
        release_id: str,
        fallback: dict[str, Any],
        *,
        include_descriptions: bool,
    ) -> dict[str, Any]:
        payload = self._request_json(
            f"{MUSICBRAINZ_API}/release/{quote(release_id)}",
            {"fmt": "json", "inc": "artist-credits+release-groups+labels+tags"},
        ) or fallback
        release_group = payload.get("release-group") or {}
        release_group_detail: dict[str, Any] = {}
        if release_group.get("id"):
            try:
                release_group_detail = self._request_json(
                    f"{MUSICBRAINZ_API}/release-group/{quote(str(release_group.get('id')))}",
                    {"fmt": "json", "inc": "tags+url-rels"},
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Release-group lookup failed for %s: %s", release_group.get("id"), exc)
        artist_credit = payload.get("artist-credit") or []
        first_artist = next(
            (
                item.get("artist")
                for item in artist_credit
                if isinstance(item, dict) and isinstance(item.get("artist"), dict)
            ),
            {},
        )
        labels = payload.get("label-info") or []
        label = next(
            (item.get("label", {}).get("name") for item in labels if isinstance(item, dict) and isinstance(item.get("label"), dict)),
            None,
        )
        label = _useful_label(label)
        tags = sorted(
            (release_group_detail.get("tags") or payload.get("tags") or []),
            key=lambda item: int(item.get("count") or 0),
            reverse=True,
        )
        date = str(
            release_group_detail.get("first-release-date")
            or release_group.get("first-release-date")
            or payload.get("date")
            or ""
        )
        year = None
        match = re.search(r"(?:19|20)\d{2}", date)
        if match:
            year = int(match.group(0))
        artist_id = first_artist.get("id")
        release_group_id = release_group.get("id")
        result: dict[str, Any] = {
            "mb_artist_id": artist_id,
            "mb_release_id": payload.get("id") or release_id,
            "mb_release_group_id": release_group_id,
            "release_type": release_group_detail.get("primary-type") or release_group.get("primary-type"),
            "year": year,
            "country": payload.get("country"),
            "label": label,
            "genres": [
                str(item.get("name") or "").strip().title()
                for item in tags[:6]
                if str(item.get("name") or "").strip()
            ],
        }
        if include_descriptions:
            result["artist_description"] = self.artist_description(artist_id) if artist_id else None
            result["album_description"] = self._release_group_description(release_group_id, release_group_detail)
        return result

    def _release_group_description(self, release_group_id: Any, payload: dict[str, Any] | None = None) -> Optional[str]:
        release_group_id = str(release_group_id or "").strip()
        if not release_group_id:
            return None
        try:
            detail = payload or self._request_json(
                f"{MUSICBRAINZ_API}/release-group/{quote(release_group_id)}",
                {"fmt": "json", "inc": "url-rels"},
            )
            wikidata_id = _wikidata_id_from_relations(detail.get("relations") or [])
            return self._wikipedia_summary_for_wikidata_id(wikidata_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Release-group description lookup failed for %s: %s", release_group_id, exc)
            return None


def _items_from_similar_json(value: Any) -> list[dict[str, Any]]:
    try:
        items = json.loads(value or "[]")
    except Exception:
        items = []
    return items if isinstance(items, list) else []


def _release_supplement_from_row(row: sqlite3.Row) -> dict[str, Any]:
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
    }


def _release_supplement_from_mapping(match: dict[str, Any]) -> dict[str, Any]:
    return {
        "release_type": match.get("release_type"),
        "year": match.get("year"),
        "country": match.get("country"),
        "label": _useful_label(match.get("label")),
        "genres": match.get("genres") or [],
    }


# Keep the module importable without side effects beyond class definitions.
_shared_service: Optional[ArtistEnrichmentService] = None
_shared_service_lock = threading.Lock()


def get_shared_artist_enrichment() -> ArtistEnrichmentService:
    """Return the process-wide shared enrichment service (global cache DB)."""
    global _shared_service
    if _shared_service is None:
        with _shared_service_lock:
            if _shared_service is None:
                _shared_service = ArtistEnrichmentService()
    return _shared_service
