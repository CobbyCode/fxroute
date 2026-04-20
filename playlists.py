"""Persistent playlist storage for local library tracks."""

import json
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Playlist:
    id: str
    name: str
    track_ids: List[str]


_cached_playlists: Optional[List[Playlist]] = None


def _playlists_file() -> Path:
    return Path(__file__).resolve().parent / "playlists.json"


def _ensure_storage() -> Path:
    path = _playlists_file()
    if not path.exists():
        path.write_text("[]\n", encoding="utf-8")
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
    global _cached_playlists
    path = _ensure_storage()
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
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
    global _cached_playlists
    if _cached_playlists is not None:
        return _cached_playlists

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
    _cached_playlists = playlists
    return playlists


def save_playlist(name: str, track_ids: List[str]) -> Playlist:
    cleaned_name = (name or "").strip()
    if not cleaned_name:
        raise ValueError("Playlist name is required")

    cleaned_track_ids = [str(track_id).strip() for track_id in track_ids if str(track_id).strip()]
    if not cleaned_track_ids:
        raise ValueError("Playlist must contain at least one track")

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
    raw = _load_raw_playlists()
    target = (playlist_id or "").strip()
    next_raw = [item for item in raw if str(item.get("id") or "").strip() != target]
    if len(next_raw) == len(raw):
        raise FileNotFoundError(f"Playlist not found: {playlist_id}")
    _save_raw_playlists(next_raw)
