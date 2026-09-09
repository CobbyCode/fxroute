"""Persistent playlist storage for local library tracks."""

import json
import logging
import os
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from common.atomic_write import atomic_write_text
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Playlist:
    id: str
    name: str
    track_ids: List[str]


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
    path = _ensure_storage()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Failed to load playlists.json: {e}")
        data = []
    if not isinstance(data, list):
        raise ValueError("playlists.json must contain a JSON array")
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
        for item in _load_raw_playlists():
            if not isinstance(item, dict):
                continue
            playlist_id = str(item.get("id") or "").strip()
            name = str(item.get("name") or "").strip()
            track_ids = item.get("track_ids") or []
            if not playlist_id or not name or not isinstance(track_ids, list):
                continue
            playlists.append(Playlist(id=playlist_id, name=name, track_ids=[str(track_id) for track_id in track_ids if str(track_id).strip()]))

        playlists.sort(key=lambda playlist: playlist.name.lower())

        with _cache_lock:
            if _cache_generation == generation_before:
                _cached_playlists = playlists
                return playlists
            current = _cached_playlists
        if current is not None:
            return current


def save_playlist(name: str, track_ids: List[str]) -> Playlist:
    cleaned_name = (name or "").strip()
    if not cleaned_name:
        raise ValueError("Playlist name is required")

    cleaned_track_ids = [str(track_id).strip() for track_id in track_ids if str(track_id).strip()]
    if not cleaned_track_ids:
        raise ValueError("Playlist must contain at least one track")

    with _mutation_lock:
        raw = _load_raw_playlists()
        existing_ids = {str(item.get("id") or "").strip() for item in raw}

        for item in raw:
            if str(item.get("name") or "").strip().lower() == cleaned_name.lower():
                item["name"] = cleaned_name
                item["track_ids"] = cleaned_track_ids
                _save_raw_playlists(raw)
                return Playlist(id=str(item["id"]), name=cleaned_name, track_ids=cleaned_track_ids)

        playlist = Playlist(
            id=_make_unique_id(cleaned_name, existing_ids),
            name=cleaned_name,
            track_ids=cleaned_track_ids,
        )
        raw.append(asdict(playlist))
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
