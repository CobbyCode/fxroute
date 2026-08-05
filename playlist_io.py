"""M3U/M3U8 playlist import and export helpers.

Extracted verbatim from main.py (REFACTOR-002). Behavior is identical to the
previous inline implementation: parsing, path resolution, deduplication,
ordering, file naming, error responses and response payloads are unchanged.

Dependencies on runtime globals (`settings`, `library_scanner`) are imported
lazily from main to avoid a circular import; main.py sets them at startup.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional
from urllib.parse import unquote

from playlists import save_playlist


def parse_m3u_entries(content: str) -> List[str]:
    entries = []
    for raw_line in (content or "").splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        entries.append(line)
    return entries


def playlist_download_filename(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "playlist").strip()).strip("-._")
    return f"{slug or 'playlist'}.m3u8"


def track_relative_m3u_path(track) -> str:
    from main import settings

    if track.path and settings:
        try:
            return track.path.resolve().relative_to(settings.MUSIC_ROOT.resolve()).as_posix()
        except Exception:
            pass
    return Path(track.url or track.id).name


def build_m3u_for_playlist(playlist) -> str:
    from main import library_scanner

    tracks_by_id = {track.id: track for track in library_scanner.get_tracks(refresh=True)}
    lines = ["#EXTM3U"]
    for track_id in playlist.track_ids:
        track = tracks_by_id.get(track_id)
        if not track:
            continue
        duration = int(track.duration) if track.duration and track.duration > 0 else -1
        label = track.title or Path(track.path or track_id).stem
        if track.artist:
            label = f"{track.artist} - {label}"
        lines.append(f"#EXTINF:{duration},{label}")
        lines.append(track_relative_m3u_path(track))
    return "\n".join(lines) + "\n"


def build_track_match_index(tracks) -> dict[str, str]:
    from main import settings

    matches = {}
    ambiguous = set()

    def add(key: str, track_id: str) -> None:
        key = (key or "").replace("\\", "/").strip().lstrip("./").lower()
        if not key:
            return
        if key in matches and matches[key] != track_id:
            ambiguous.add(key)
            matches.pop(key, None)
            return
        if key not in ambiguous:
            matches[key] = track_id

    for track in tracks:
        if not track.path:
            continue
        path = track.path.resolve()
        try:
            rel = path.relative_to(settings.MUSIC_ROOT.resolve()).as_posix()
            add(rel, track.id)
        except Exception:
            pass
        add(path.as_posix(), track.id)
        add(path.name, track.id)
        if track.url:
            add(str(track.url), track.id)
    return matches


def resolve_m3u_track_ids(entries: List[str], base_dir: Optional[Path] = None, tracks=None) -> List[str]:
    from main import library_scanner, settings

    if tracks is None:
        tracks = library_scanner.get_tracks(refresh=True)
    match_index = build_track_match_index(tracks)
    track_ids = []
    seen = set()

    for entry in entries:
        value = unquote(entry.strip().strip('"'))
        if value.lower().startswith("file://"):
            value = value[7:]
        value = value.replace("\\", "/")
        candidates = [value]
        if base_dir and not Path(value).is_absolute():
            try:
                resolved = (base_dir / value).resolve()
                candidates.append(resolved.as_posix())
                candidates.append(resolved.relative_to(settings.MUSIC_ROOT.resolve()).as_posix())
            except Exception:
                pass
        candidates.append(Path(value).name)

        for candidate in candidates:
            track_id = match_index.get(candidate.replace("\\", "/").strip().lstrip("./").lower())
            if track_id and track_id not in seen:
                seen.add(track_id)
                track_ids.append(track_id)
                break
    return track_ids


def import_m3u_playlist(name: str, content: str, base_dir: Optional[Path] = None, tracks=None) -> Optional[dict]:
    entries = parse_m3u_entries(content)
    track_ids = resolve_m3u_track_ids(entries, base_dir=base_dir, tracks=tracks)
    if not track_ids:
        return None
    playlist = save_playlist(Path(name).stem or "Imported playlist", track_ids)
    return {
        "id": playlist.id,
        "name": playlist.name,
        "track_ids": playlist.track_ids,
        "track_count": len(playlist.track_ids),
        "matched_track_count": len(track_ids),
        "entry_count": len(entries),
    }
