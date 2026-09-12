"""M3U/M3U8 playlist import and export helpers.

Parsing and path matching are independent of the application runtime. Callers
provide the music root and, where needed, the current library tracks.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional
from urllib.parse import unquote

from library.playlists import save_playlist


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


def track_relative_m3u_path(track, music_root: Path) -> str:
    if track.path:
        try:
            return track.path.resolve().relative_to(music_root.resolve()).as_posix()
        except Exception:
            pass
    return Path(track.url or track.id).name


def build_m3u_for_playlist(playlist, tracks, music_root: Path) -> str:
    tracks_by_id = {track.id: track for track in tracks}
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
        lines.append(track_relative_m3u_path(track, music_root))
    return "\n".join(lines) + "\n"


def _normalize_match_key(key: str) -> str:
    return (key or "").replace("\\", "/").strip().lstrip("./").lower()


def build_track_match_index(tracks, music_root: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Build the M3U resolution indexes as ``(exact, basenames)``.

    ``exact`` holds unambiguous relative paths, absolute paths and URLs.
    ``basenames`` holds bare filenames and is consulted only after an exact
    miss.  Keeping them apart means a basename collision (two tracks named
    ``song.flac`` in different folders) can never remove another track's exact
    relative path from the index; the same-named file remains resolvable by
    the path an M3U actually recorded.
    """
    exact: dict[str, str] = {}
    basenames: dict[str, str] = {}
    ambiguous_exact: set[str] = set()
    ambiguous_basenames: set[str] = set()

    def add(index: dict[str, str], ambiguous: set[str], key: str, track_id: str) -> None:
        key = _normalize_match_key(key)
        if not key:
            return
        if key in index and index[key] != track_id:
            ambiguous.add(key)
            index.pop(key, None)
            return
        if key not in ambiguous:
            index[key] = track_id

    for track in tracks:
        if not track.path:
            continue
        path = track.path.resolve()
        try:
            rel = path.relative_to(music_root.resolve()).as_posix()
            add(exact, ambiguous_exact, rel, track.id)
        except Exception:
            pass
        add(exact, ambiguous_exact, path.as_posix(), track.id)
        add(basenames, ambiguous_basenames, path.name, track.id)
        if track.url:
            add(exact, ambiguous_exact, str(track.url), track.id)
    return exact, basenames


def resolve_m3u_track_ids(
    entries: List[str],
    music_root: Path,
    base_dir: Optional[Path] = None,
    tracks=None,
) -> List[str]:
    if tracks is None:
        raise ValueError("tracks are required")
    exact_index, basename_index = build_track_match_index(tracks, music_root)
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
                candidates.append(resolved.relative_to(music_root.resolve()).as_posix())
            except Exception:
                pass
        candidates.append(Path(value).name)

        # Exact identities win over bare filenames, so a recorded relative path
        # is never shadowed by another folder's same-named file.
        track_id = next(
            (exact_index[key] for key in (_normalize_match_key(candidate) for candidate in candidates) if key in exact_index),
            None,
        )
        if track_id is None:
            track_id = next(
                (basename_index[key] for key in (_normalize_match_key(candidate) for candidate in candidates) if key in basename_index),
                None,
            )
        if track_id and track_id not in seen:
            seen.add(track_id)
            track_ids.append(track_id)
    return track_ids


def import_m3u_playlist(
    name: str,
    content: str,
    music_root: Path,
    base_dir: Optional[Path] = None,
    tracks=None,
) -> Optional[dict]:
    entries = parse_m3u_entries(content)
    track_ids = resolve_m3u_track_ids(
        entries,
        music_root,
        base_dir=base_dir,
        tracks=tracks,
    )
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
