"""Persistent playlist storage for local library tracks."""

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from common.atomic_write import atomic_write_text
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class PlaylistStoreCorruptedError(ValueError):
    """The persisted playlists.json cannot be reliably parsed.

    Raised by the strict load path used for every mutation, so a damaged
    store is never silently overwritten.  A ValueError subclass, matching
    the store-corruption convention used elsewhere in the codebase.
    """


# A playlist entry is either a local library track id or a TIDAL reference
# ``tidal:<id>``.  References carry no stream data: the audio is resolved at
# play time, and ``track_meta`` only keeps what is needed to list the entry
# while TIDAL is not connected.
TIDAL_REF_PREFIX = "tidal:"
_TIDAL_REF_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TRACK_META_TEXT_LIMIT = 300
_TRACK_META_URL_LIMIT = 600
_TRACK_META_TEXT_FIELDS = ("title", "artist", "album")


def is_tidal_ref(track_id: str) -> bool:
    return str(track_id or "").startswith(TIDAL_REF_PREFIX)


def tidal_ref_id(track_id: str) -> str:
    return str(track_id)[len(TIDAL_REF_PREFIX):]


def _valid_tidal_ref(track_id: str) -> bool:
    return is_tidal_ref(track_id) and bool(_TIDAL_REF_ID.match(tidal_ref_id(track_id)))


@dataclass
class Playlist:
    id: str
    name: str
    track_ids: List[str]
    track_meta: Dict[str, dict] = field(default_factory=dict)


_cached_playlists: Optional[List[Playlist]] = None
# Module-local ownership for playlist mutations: every complete
# read -> mutate -> atomic persist -> cache update cycle runs under this
# lock, so parallel callers (API requests or future offload threads)
# cannot lose each other's changes.  RLock keeps nested persistence
# helpers safe to compose.  Reads stay lock-free.
_mutation_lock = threading.RLock()
# Generation counter for the playlist cache.  A writer bumps it after every
# successful store commit; readers only publish a freshly loaded snapshot
# when the generation is unchanged, so a read that overlapped a commit can
# never publish stale state as the current cache.  The threading lock below
# guards ONLY the cache pointer and this counter (pure Python assignments);
# no file I/O ever happens under it.
_cache_generation = 0
_cache_lock = threading.Lock()


BASE_DIR = Path(__file__).resolve().parent.parent


def _config_dir() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return root / "fxroute"


def _playlists_file() -> Path:
    return _config_dir() / "playlists.json"


def _legacy_playlists_file() -> Path:
    return BASE_DIR / "playlists.json"


def _ensure_storage() -> Path:
    path = _playlists_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        legacy_path = _legacy_playlists_file()
        if legacy_path.exists():
            atomic_write_text(path, legacy_path.read_text(encoding="utf-8"))
            logger.info("Migrated playlists storage to %s", path)
        else:
            atomic_write_text(path, "[]\n")
    return path


def _load_raw_playlists() -> List[dict]:
    """Load the raw playlist documents, fail-closed on corruption.

    An unreadable or unparsable store raises PlaylistStoreCorruptedError
    so no mutation can overwrite the damaged file.  Reads that must stay
    available catch it and fall back explicitly.
    """
    path = _ensure_storage()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Failed to load playlists.json: {e}")
        raise PlaylistStoreCorruptedError(f"playlists.json is corrupted and cannot be parsed: {e}") from e
    if not isinstance(data, list):
        raise PlaylistStoreCorruptedError("playlists.json is corrupted and must contain a JSON array")
    return data


def _save_raw_playlists(data: List[dict]) -> None:
    global _cache_generation, _cached_playlists
    path = _ensure_storage()
    atomic_write_text(path, json.dumps(data, indent=2) + "\n")
    with _cache_lock:
        _cache_generation += 1
        _cached_playlists = None


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or "playlist"


def _make_unique_id(name: str, existing_ids: set[str]) -> str:
    candidate = _slugify(name)
    if candidate not in existing_ids:
        return candidate
    index = 2
    while f"{candidate}-{index}" in existing_ids:
        index += 1
    return f"{candidate}-{index}"


def _unique_name_for(raw: List[dict], name: str) -> str:
    """First free variant of ``name`` among the persisted playlists.

    Case-insensitive, matching the identity ``save_playlist`` compares on.
    """
    taken = {str(item.get("name") or "").strip().lower() for item in raw}
    if name.lower() not in taken:
        return name
    index = 2
    while f"{name} ({index})".lower() in taken:
        index += 1
    return f"{name} ({index})"


def _clean_track_ids(track_ids: List[str]) -> List[str]:
    """Trim ids and reject malformed TIDAL references."""
    cleaned = [str(track_id).strip() for track_id in track_ids if str(track_id).strip()]
    for track_id in cleaned:
        if is_tidal_ref(track_id) and not _valid_tidal_ref(track_id):
            raise ValueError(f"Invalid TIDAL track reference: {track_id[:80]}")
    return cleaned


def clean_track_meta(track_ids: List[str], track_meta: Optional[dict]) -> Dict[str, dict]:
    """Whitelist and cap the display metadata of TIDAL references.

    Only references that are part of ``track_ids`` keep an entry, so removed
    tracks never leave orphaned metadata behind.  Anything but short text and a
    non-negative duration is dropped.
    """
    if not isinstance(track_meta, dict):
        return {}
    refs = {track_id for track_id in track_ids if _valid_tidal_ref(track_id)}
    cleaned: Dict[str, dict] = {}
    for ref, meta in track_meta.items():
        if ref not in refs or not isinstance(meta, dict):
            continue
        entry: dict = {}
        for key in _TRACK_META_TEXT_FIELDS:
            value = meta.get(key)
            if isinstance(value, str) and value.strip():
                entry[key] = value.strip()[:_TRACK_META_TEXT_LIMIT]
        art_url = meta.get("art_url")
        if isinstance(art_url, str) and art_url.strip():
            entry["art_url"] = art_url.strip()[:_TRACK_META_URL_LIMIT]
        duration = meta.get("duration")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and 0 <= duration < 86400 * 7:
            entry["duration"] = float(duration)
        if entry:
            cleaned[ref] = entry
    return cleaned


def _playlist_document(playlist: Playlist) -> dict:
    """Persisted form; ``track_meta`` is only written when a playlist has TIDAL entries."""
    document = {"id": playlist.id, "name": playlist.name, "track_ids": list(playlist.track_ids)}
    if playlist.track_meta:
        document["track_meta"] = {ref: dict(meta) for ref, meta in playlist.track_meta.items()}
    return document


def get_playlists() -> List[Playlist]:
    """Load saved playlists, cache-publication-safe.

    The file is read outside the cache lock and a snapshot is only
    published when no writer committed while it was being loaded;
    otherwise the current cache wins or the state is reloaded, so a
    reader can never publish a stale snapshot after a newer commit.
    """
    global _cached_playlists
    while True:
        with _cache_lock:
            if _cached_playlists is not None:
                return _cached_playlists
            generation_before = _cache_generation

        playlists: List[Playlist] = []
        try:
            raw = _load_raw_playlists()
        except PlaylistStoreCorruptedError:
            # Fail-open read: stay available, but never publish the fallback
            # to the cache and never touch the damaged file, so mutations
            # keep failing closed and a later restore is picked up.
            return []
        for item in raw:
            if not isinstance(item, dict):
                continue
            playlist_id = str(item.get("id") or "").strip()
            name = str(item.get("name") or "").strip()
            track_ids = item.get("track_ids") or []
            if not playlist_id or not name or not isinstance(track_ids, list):
                continue
            ids = [str(track_id) for track_id in track_ids if str(track_id).strip()]
            playlists.append(Playlist(id=playlist_id, name=name, track_ids=ids, track_meta=clean_track_meta(ids, item.get("track_meta"))))

        playlists.sort(key=lambda playlist: playlist.name.lower())

        with _cache_lock:
            if _cache_generation == generation_before:
                _cached_playlists = playlists
                return playlists
            current = _cached_playlists
        if current is not None:
            return current


def save_playlist(name: str, track_ids: List[str], track_meta: Optional[dict] = None) -> Playlist:
    cleaned_name = (name or "").strip()
    if not cleaned_name:
        raise ValueError("Playlist name is required")

    cleaned_track_ids = _clean_track_ids(track_ids)
    if not cleaned_track_ids:
        raise ValueError("Playlist must contain at least one track")
    cleaned_meta = clean_track_meta(cleaned_track_ids, track_meta)

    with _mutation_lock:
        raw = _load_raw_playlists()
        existing_ids = {str(item.get("id") or "").strip() for item in raw}

        for item in raw:
            if str(item.get("name") or "").strip().lower() == cleaned_name.lower():
                item["name"] = cleaned_name
                item["track_ids"] = cleaned_track_ids
                if cleaned_meta:
                    item["track_meta"] = cleaned_meta
                else:
                    item.pop("track_meta", None)
                _save_raw_playlists(raw)
                return Playlist(id=str(item["id"]), name=cleaned_name, track_ids=cleaned_track_ids, track_meta=cleaned_meta)

        playlist = Playlist(
            id=_make_unique_id(cleaned_name, existing_ids),
            name=cleaned_name,
            track_ids=cleaned_track_ids,
            track_meta=cleaned_meta,
        )
        raw.append(_playlist_document(playlist))
        _save_raw_playlists(raw)
        return playlist


def save_new_playlist(name: str, track_ids: List[str], track_meta: Optional[dict] = None) -> Playlist:
    """Create a playlist without ever replacing an existing one.

    ``save_playlist`` deliberately overwrites a same-named playlist, which is
    what an explicit user save means.  An import must never do that: its name
    is derived from a file stem, so it would silently replace a user playlist
    that happens to carry the same name.  The name is therefore made unique
    inside the same locked read -> decide -> persist cycle that creates the
    entry, so two concurrent imports cannot pick the same free name.
    """
    cleaned_name = (name or "").strip()
    if not cleaned_name:
        raise ValueError("Playlist name is required")

    cleaned_track_ids = _clean_track_ids(track_ids)
    if not cleaned_track_ids:
        raise ValueError("Playlist must contain at least one track")
    cleaned_meta = clean_track_meta(cleaned_track_ids, track_meta)

    with _mutation_lock:
        raw = _load_raw_playlists()
        unique_name = _unique_name_for(raw, cleaned_name)
        existing_ids = {str(item.get("id") or "").strip() for item in raw}
        playlist = Playlist(
            id=_make_unique_id(unique_name, existing_ids),
            name=unique_name,
            track_ids=cleaned_track_ids,
            track_meta=cleaned_meta,
        )
        raw.append(_playlist_document(playlist))
        _save_raw_playlists(raw)
        return playlist


def delete_playlist(playlist_id: str) -> None:
    with _mutation_lock:
        raw = _load_raw_playlists()
        target = (playlist_id or "").strip()
        next_raw = [item for item in raw if str(item.get("id") or "").strip() != target]
        if len(next_raw) == len(raw):
            raise FileNotFoundError(f"Playlist not found: {playlist_id}")
        _save_raw_playlists(next_raw)
