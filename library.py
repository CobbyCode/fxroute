"""Local music library scanner."""

import hashlib
import html
import logging
import os
import re
import subprocess
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from mutagen import File as MutagenFile
from mutagen.id3 import ID3NoHeaderError

from models import Track
from config import get_settings
from library_metadata import LibraryMetadataStore

ALBUM_COVER_NAMES = (
    "cover.jpg", "cover.jpeg", "cover.png", "cover.webp",
    "folder.jpg", "folder.jpeg", "folder.png", "folder.webp",
    "front.jpg", "front.jpeg", "front.png", "front.webp",
)

logger = logging.getLogger(__name__)


def _first_tag_value(tags: Any, *keys: str) -> Optional[str]:
    """Return the first non-empty tag value from a mutagen tag mapping."""
    if not tags:
        return None
    for key in keys:
        try:
            values = tags.get(key)
        except Exception:
            values = None
        if values is None:
            continue
        if not isinstance(values, (list, tuple)):
            values = [values]
        for value in values:
            if hasattr(value, "text"):
                text_values = getattr(value, "text") or []
                value = text_values[0] if text_values else value
            text = str(value).strip()
            if text:
                return text
    return None


def _tag_number(tags: Any, *keys: str) -> Optional[int]:
    """Parse a numeric tag value such as '03', '3/12', or MP4 tuple values."""
    raw = _first_tag_value(tags, *keys)
    if not raw:
        return None
    match = re.search(r"\d+", raw)
    if not match:
        return None
    try:
        value = int(match.group(0))
    except ValueError:
        return None
    return value if value > 0 else None


def _tag_year(tags: Any, *keys: str) -> Optional[int]:
    raw = _first_tag_value(tags, *keys)
    if not raw:
        return None
    match = re.search(r"(?:19|20)\d{2}", raw)
    if not match:
        return None
    try:
        year = int(match.group(0))
    except ValueError:
        return None
    return year if 1900 <= year <= 2099 else None


def _track_sort_key(track: Track) -> tuple:
    """Stable library ordering: folder/file first, then tag track order within folders."""
    path = track.path or Path("")
    folder = path.parent.as_posix().lower()
    filename = path.name.lower()
    disc = track.disc_number if track.disc_number is not None else 0
    track_no = track.track_number if track.track_number is not None else 9999
    return (folder, disc, track_no, filename, (track.title or "").lower())


def _filename_artist_title(filepath: Path) -> tuple[Optional[str], Optional[str]]:
    """Best-effort fallback for files named like 'Artist - Title.ext'."""
    stem = re.sub(r"\s+", " ", filepath.stem).strip()
    parts = re.split(r"\s+-\s+", stem, maxsplit=1)
    if len(parts) != 2:
        return None, None
    artist, title = (part.strip(" -_\t") for part in parts)
    if not artist or not title:
        return None, None
    if len(artist) < 2 or len(title) < 2:
        return None, None
    return artist, title


def _clean_import_folder_text(value: str) -> str:
    """Normalize archive/folder names from web downloads for display metadata."""
    text = (value or "").replace("_amp_", "&").replace("&amp;", "&")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip(" -_\t")
    return text


def _looks_like_import_album_dir(folder: Path) -> bool:
    """Return true for imported album folders, without affecting loose libraries."""
    return _folder_has_local_audio_playlist(folder)


def _folder_has_local_audio_playlist(folder: Path) -> bool:
    for playlist in folder.iterdir():
        if not playlist.is_file() or playlist.suffix.lower() not in {".m3u", ".m3u8"}:
            continue
        try:
            lines = playlist.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            entry = line.strip()
            if not entry or entry.startswith("#"):
                continue
            if re.match(r"^[a-z][a-z0-9+.-]*://", entry, re.IGNORECASE):
                continue
            candidate = (folder / entry).resolve()
            try:
                candidate.relative_to(folder.resolve())
            except ValueError:
                continue
            if candidate.is_file() and candidate.suffix.lower() in AUDIO_EXTENSIONS:
                return True
    return False


def _infer_album_from_folder_name(folder_name: str, track_artist: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """
    Best-effort album metadata for ZIP imports that contain no album tags.

    Common Jamendo ZIP names look like:
    "Artist - Album - 123456 --- Jamendo - MP3".
    """
    name = _clean_import_folder_text(folder_name)
    name = re.sub(r"\s+---\s+.*$", "", name).strip()
    parts = [_clean_import_folder_text(part) for part in re.split(r"\s+-\s+", name) if _clean_import_folder_text(part)]

    if len(parts) >= 3 and re.fullmatch(r"\d{3,}", parts[-1]):
        artist = parts[0]
        album = " - ".join(parts[1:-1])
    elif len(parts) >= 2:
        artist = parts[0]
        album = " - ".join(parts[1:])
    else:
        artist = _clean_import_folder_text(track_artist or "")
        album = name

    return (album or None), (artist or track_artist or None)


def _probe_sample_rate_with_ffprobe(filepath: Path) -> Optional[int]:
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=sample_rate",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(filepath),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except Exception as e:
        logger.debug(f"ffprobe sample-rate probe failed for {filepath}: {e}")
        return None

    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        if stderr:
            logger.debug(f"ffprobe sample-rate probe returned {completed.returncode} for {filepath}: {stderr}")
        return None

    first_line = (completed.stdout or "").strip().splitlines()
    if not first_line:
        return None

    try:
        value = int(first_line[0].strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None

# Supported audio file extensions
AUDIO_EXTENSIONS = {".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".wav", ".wma", ".webm", ".weba"}


class LibraryScanner:
    """Scans the music directory and provides track listings."""

    def __init__(self):
        self.settings = get_settings()
        self.music_root: Path = self.settings.MUSIC_ROOT
        self._track_cache: List[Track] = []
        self._scan_in_progress = False
        self._last_scan: Optional[datetime] = None
        self._scan_error: Optional[str] = None
        self._scan_started_at: Optional[datetime] = None
        self._scan_current_dir: Optional[str] = None
        self._scan_files_seen = 0
        self._scan_audio_seen = 0
        self._scan_tracks_found = 0
        self._scan_track_cache_hits = 0
        self._scan_track_cache_misses = 0
        self.metadata_store = LibraryMetadataStore()

    def prepare_scan_status(self):
        """Mark a scan as active before scanner work starts."""
        self._scan_in_progress = True
        self._scan_error = None
        self._scan_started_at = datetime.now()
        self._scan_current_dir = None
        self._scan_files_seen = 0
        self._scan_audio_seen = 0
        self._scan_tracks_found = 0
        self._scan_track_cache_hits = 0
        self._scan_track_cache_misses = 0

    def refresh(self, force: bool = False) -> List[Track]:
        """
        Scan the music directory and build track list.
        Returns list of Track objects.
        """
        if self._scan_in_progress and not force:
            logger.warning("Scan already in progress, returning cached")
            return self._track_cache

        self.prepare_scan_status()
        tracks = []
        active_track_paths: List[str] = []

        try:
            if not self.music_root.exists():
                logger.warning(f"Music root does not exist: {self.music_root}")
                self._scan_error = f"Music directory not found: {self.music_root}"
                return []

            logger.info(f"Scanning music directory: {self.music_root}")
            for root, dirs, files in os.walk(self.music_root):
                try:
                    self._scan_current_dir = str(Path(root).relative_to(self.music_root))
                except ValueError:
                    self._scan_current_dir = str(root)
                if self._scan_current_dir == ".":
                    self._scan_current_dir = ""
                for filename in files:
                    self._scan_files_seen += 1
                    filepath = Path(root) / filename
                    if filepath.suffix.lower() in AUDIO_EXTENSIONS:
                        self._scan_audio_seen += 1
                        try:
                            rel_path = filepath.relative_to(self.music_root).as_posix()
                            active_track_paths.append(rel_path)
                            stat = filepath.stat()
                            cached = self.metadata_store.get_cached_track(rel_path, stat.st_mtime_ns, stat.st_size)
                            if cached:
                                track = self._track_from_cached_metadata(filepath, cached)
                                self._scan_track_cache_hits += 1
                            else:
                                track = self._create_track_from_file(filepath)
                                self._scan_track_cache_misses += 1
                                if track:
                                    self.metadata_store.upsert_track_metadata(self._track_cache_payload(track, rel_path, stat))
                            if track:
                                tracks.append(track)
                                self._scan_tracks_found = len(tracks)
                        except Exception as e:
                            logger.warning(f"Failed to read metadata for {filepath}: {e}")

            # Keep large-library browsing predictable by grouping paths/folders first,
            # while honoring tag track numbers inside the same folder/album when present.
            tracks.sort(key=_track_sort_key)

            self._track_cache = tracks
            try:
                self._scan_current_dir = "Finalizing metadata"
                self.metadata_store.sync_tracks_seen(active_track_paths)
                self.metadata_store.sync_albums(self.get_albums(refresh=False, include_metadata=False))
            except Exception as e:
                logger.info("Smart metadata sync skipped: %s", e)
            self._last_scan = datetime.now()
            logger.info(
                "Library scan complete: %s tracks (metadata cache: %s hits, %s misses)",
                len(tracks),
                self._scan_track_cache_hits,
                self._scan_track_cache_misses,
            )

        except Exception as e:
            logger.error(f"Library scan failed: {e}")
            self._scan_error = str(e)
        finally:
            self._scan_current_dir = None
            self._scan_in_progress = False

        return self._track_cache

    def _create_track_from_file(self, filepath: Path) -> Optional[Track]:
        """Create a Track object with metadata from file."""
        try:
            # Calculate relative path for ID
            rel_path = filepath.relative_to(self.music_root)
            track_id = f"local_{rel_path.as_posix()}"

            # Try to read metadata with mutagen
            title = None
            artist = None
            album = None
            album_artist = None
            genre = None
            year = None
            track_number = None
            disc_number = None
            duration = None
            sample_rate_hz = None

            try:
                audio = MutagenFile(str(filepath), easy=True)
                if audio and audio.tags:
                    title = _first_tag_value(audio.tags, "title")
                    artist = _first_tag_value(audio.tags, "artist")
                    album = _first_tag_value(audio.tags, "album")
                    album_artist = _first_tag_value(audio.tags, "albumartist", "album_artist")
                    genre = _first_tag_value(audio.tags, "genre")
                    year = _tag_year(audio.tags, "date", "year", "originaldate")
                    track_number = _tag_number(audio.tags, "tracknumber")
                    disc_number = _tag_number(audio.tags, "discnumber")
                if audio and audio.info:
                    duration = audio.info.length
                    sample_rate_hz = getattr(audio.info, "sample_rate", None)
                if not all([title, artist, album, album_artist, track_number, disc_number]) or duration is None or sample_rate_hz is None:
                    raw_audio = MutagenFile(str(filepath), easy=False)
                    if raw_audio and raw_audio.tags:
                        title = title or _first_tag_value(raw_audio.tags, "TIT2", "\xa9nam")
                        artist = artist or _first_tag_value(raw_audio.tags, "TPE1", "\xa9ART")
                        album = album or _first_tag_value(raw_audio.tags, "TALB", "\xa9alb")
                        album_artist = album_artist or _first_tag_value(raw_audio.tags, "TPE2", "aART")
                        genre = genre or _first_tag_value(raw_audio.tags, "TCON", "\xa9gen")
                        year = year or _tag_year(raw_audio.tags, "TDRC", "TYER", "TDOR", "\xa9day")
                        track_number = track_number or _tag_number(raw_audio.tags, "TRCK", "trkn")
                        disc_number = disc_number or _tag_number(raw_audio.tags, "TPOS", "disk")
                    if raw_audio and raw_audio.info:
                        if duration is None:
                            duration = getattr(raw_audio.info, "length", None)
                        if sample_rate_hz is None:
                            sample_rate_hz = getattr(raw_audio.info, "sample_rate", None)
            except ID3NoHeaderError:
                pass
            except Exception as e:
                logger.debug(f"Mutagen read error for {filepath}: {e}")

            if sample_rate_hz is None:
                sample_rate_hz = _probe_sample_rate_with_ffprobe(filepath)

            filename_artist, filename_title = _filename_artist_title(filepath)
            if not title and filename_title:
                title = filename_title
            if not artist and filename_artist:
                artist = filename_artist

            if not album:
                inferred_album, inferred_album_artist = self._infer_import_album(filepath, artist)
                if inferred_album:
                    album = inferred_album
                    album_artist = album_artist or inferred_album_artist

            if not title:
                title = filepath.stem
            if not artist:
                artist = None
            if not album:
                album = None
            if not album_artist:
                album_artist = None

            return Track(
                id=track_id,
                title=title,
                artist=artist,
                album=album,
                album_artist=album_artist,
                genre=genre,
                year=year,
                track_number=track_number,
                disc_number=disc_number,
                source="local",
                url=str(filepath.absolute()),
                duration=duration,
                path=filepath,
                sample_rate_hz=int(sample_rate_hz) if sample_rate_hz else None,
            )

        except Exception as e:
            logger.warning(f"Failed to create track for {filepath}: {e}")
            return None

    def _track_from_cached_metadata(self, filepath: Path, cached: Dict[str, Any]) -> Track:
        rel_path = filepath.relative_to(self.music_root)
        return Track(
            id=str(cached.get("track_id") or f"local_{rel_path.as_posix()}"),
            title=str(cached.get("title") or filepath.stem),
            artist=cached.get("artist"),
            album=cached.get("album"),
            album_artist=cached.get("album_artist"),
            genre=cached.get("genre"),
            year=int(cached["year"]) if cached.get("year") is not None else None,
            track_number=int(cached["track_number"]) if cached.get("track_number") is not None else None,
            disc_number=int(cached["disc_number"]) if cached.get("disc_number") is not None else None,
            source="local",
            url=str(filepath.absolute()),
            duration=float(cached["duration"]) if cached.get("duration") is not None else None,
            path=filepath,
            sample_rate_hz=int(cached["sample_rate_hz"]) if cached.get("sample_rate_hz") else None,
        )

    def _track_cache_payload(self, track: Track, rel_path: str, stat: os.stat_result) -> Dict[str, Any]:
        return {
            "rel_path": rel_path,
            "track_id": track.id,
            "mtime_ns": stat.st_mtime_ns,
            "size_bytes": stat.st_size,
            "title": track.title,
            "artist": track.artist,
            "album": track.album,
            "album_artist": track.album_artist,
            "genre": track.genre,
            "year": track.year,
            "track_number": track.track_number,
            "disc_number": track.disc_number,
            "duration": track.duration,
            "sample_rate_hz": track.sample_rate_hz,
        }

    def _infer_import_album(self, filepath: Path, track_artist: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        """Infer album metadata only for imported archive folders lacking tags."""
        folder = filepath.parent
        try:
            rel_folder = folder.relative_to(self.settings.download_dir)
        except ValueError:
            return None, None
        if rel_folder == Path("."):
            return None, None

        try:
            if not _looks_like_import_album_dir(folder):
                return None, None
        except OSError:
            return None, None

        return _infer_album_from_folder_name(folder.name, track_artist)

    def get_tracks(self, refresh: bool = False) -> List[Track]:
        """Get tracks list, optionally forcing a refresh."""
        if refresh or not self._track_cache:
            return self.refresh(force=refresh)
        return self._track_cache

    def status(self) -> Dict[str, Any]:
        """Return lightweight scan status for UI polling."""
        return {
            "scanning": self._scan_in_progress,
            "track_count": len(self._track_cache),
            "files_seen": self._scan_files_seen,
            "audio_seen": self._scan_audio_seen,
            "tracks_found": self._scan_tracks_found,
            "track_cache_hits": self._scan_track_cache_hits,
            "track_cache_misses": self._scan_track_cache_misses,
            "current_dir": self._scan_current_dir,
            "last_scan": self._last_scan.isoformat() if self._last_scan else None,
            "started_at": self._scan_started_at.isoformat() if self._scan_started_at else None,
            "error": self._scan_error,
        }

    @property
    def scanning(self) -> bool:
        """Whether a scan is currently in progress."""
        return self._scan_in_progress

    @property
    def last_scan(self) -> Optional[datetime]:
        """Timestamp of last successful scan."""
        return self._last_scan

    @property
    def error(self) -> Optional[str]:
        """Last scan error, if any."""
        return self._scan_error

    # ── Album grouping ──────────────────────────────────────────────

    def get_albums(self, refresh: bool = False, include_metadata: bool = True) -> List[Dict[str, Any]]:
        """
        Group cached tracks into albums.
        Returns a list of album dicts sorted by album_artist then album name.
        Tracks without album tags are grouped into a synthetic 'Various' album
        so they do not clutter the album view.
        """
        tracks = self.get_tracks(refresh=refresh)
        if not tracks:
            return []

        # Pre-scan: detect compilations (same album name, different artists, no album_artist)
        _album_artists: Dict[str, set] = {}
        for t in tracks:
            an = (t.album or "").strip()
            aa = (t.album_artist or "").strip()
            if not an or aa:
                continue
            _album_artists.setdefault(an.lower(), set())
            ar = (t.artist or "").strip()
            if ar:
                _album_artists[an.lower()].add(ar.lower())

        _compilation_albums = {name for name, artists in _album_artists.items() if len(artists) > 1}

        albums = OrderedDict()

        for track in tracks:
            album_name = (track.album or "").strip()
            album_artist = (track.album_artist or "").strip()

            if not album_name:
                # Loose tracks without album tag → 'Various'
                album_name = "Various"
                album_artist = "Various"
            elif not album_artist:
                if album_name.lower() in _compilation_albums:
                    album_artist = "Various Artists"
                else:
                    album_artist = (track.artist or "").strip() or "Various Artists"

            key = f"{album_artist.lower()}::{album_name.lower()}"

            if key not in albums:
                albums[key] = {
                    "id": _album_id(album_artist, album_name),
                    "name": album_name,
                    "artist": album_artist,
                    "track_count": 0,
                    "tracks": [],
                    "genres": set(),
                    "years": set(),
                    "folder_path": None,
                    "cover_source_track_id": None,
                    "cover_source": None,
                }

            entry = albums[key]
            entry["track_count"] += 1
            entry["tracks"].append(track)
            if track.genre:
                entry["genres"].add(str(track.genre).strip())
            if track.year:
                entry["years"].add(track.year)

            # Remember the folder path (all tracks in same album should share it)
            if entry["folder_path"] is None and track.path:
                entry["folder_path"] = track.path.parent

            # Pick a cover source: first track that has folder cover, then first with embedded
            if entry["cover_source_track_id"] is None and track.path:
                if _has_folder_cover(track.path):
                    entry["cover_source_track_id"] = track.id
                    entry["cover_source"] = "folder"
                elif entry["cover_source"] != "folder":
                    # Only set embedded if we haven't found a folder cover yet
                    if entry.get("cover_source") != "embedded" and _has_embedded_cover(track.path):
                        entry["cover_source_track_id"] = track.id
                        entry["cover_source"] = "embedded"

        # Sort: Various last, then by artist name, then album name
        result = list(albums.values())
        result.sort(key=lambda a: (
            a["name"] == "Various",
            a["artist"].lower(),
            a["name"].lower(),
        ))

        # Clean up internal fields before returning
        for entry in result:
            del entry["tracks"]
            del entry["folder_path"]
            del entry["cover_source_track_id"]
            entry["genres"] = sorted(entry["genres"], key=lambda item: item.lower())
            sorted_years = sorted(entry["years"])
            entry["years"] = sorted_years
            entry["year"] = sorted_years[0] if sorted_years else None
            if "cover_source" not in entry:
                entry["cover_source"] = None
            if include_metadata:
                metadata = self.metadata_store.get_album(entry["id"])
                if metadata:
                    external_genres = metadata.pop("genres", []) or []
                    if external_genres:
                        merged_genres = []
                        seen = set()
                        for genre in [*entry["genres"], *external_genres]:
                            key = str(genre).lower()
                            if key not in seen:
                                merged_genres.append(genre)
                                seen.add(key)
                        entry["genres"] = merged_genres[:6]
                    for key, value in metadata.items():
                        if value in (None, "", []):
                            continue
                        if key == "year" and entry.get("year"):
                            continue
                        entry[key] = value

        return result

    def get_album_tracks(self, album_id: str) -> List[Track]:
        """Return the track list for a given album id."""
        tracks = self.get_tracks()

        # Same compilation detection as get_albums()
        _album_artists: Dict[str, set] = {}
        for t in tracks:
            an = (t.album or "").strip()
            aa = (t.album_artist or "").strip()
            if not an or aa:
                continue
            _album_artists.setdefault(an.lower(), set())
            ar = (t.artist or "").strip()
            if ar:
                _album_artists[an.lower()].add(ar.lower())
        _compilation_albums = {name for name, artists in _album_artists.items() if len(artists) > 1}

        result = []
        for track in tracks:
            album_name = (track.album or "").strip()
            album_artist = (track.album_artist or "").strip()
            if not album_name:
                album_name = "Various"
                album_artist = "Various"
            elif not album_artist:
                if album_name.lower() in _compilation_albums:
                    album_artist = "Various Artists"
                else:
                    album_artist = (track.artist or "").strip() or "Various Artists"
            if _album_id(album_artist, album_name) == album_id:
                result.append(track)
        return sorted(result, key=_track_sort_key)

    def get_album_metadata(self, album_id: str) -> Dict[str, Any]:
        return self.metadata_store.get_album(album_id)

    def get_album_external_cover(self, album_id: str) -> Optional[Path]:
        return self.metadata_store.external_cover_path(album_id)

    def set_album_favorite(self, album_id: str, favorite: bool) -> Dict[str, Any]:
        return self.metadata_store.set_album_favorite(album_id, favorite)

    def get_album_discover(self, album_id: str, force: bool = False) -> Dict[str, Any]:
        return self.metadata_store.get_album_discover(album_id, force=force)


def _album_id(artist: str, album: str) -> str:
    """Stable album id from artist + album name."""
    raw = f"{artist.lower()}::{album.lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _is_compilation_by_tracks(album_name: str, tracks: List[Track]) -> bool:
    """Heuristic: album_artist is missing and tracks have different artists."""
    if not album_name or album_name == "Various":
        return False
    artists = set()
    for t in tracks:
        aa = (t.album_artist or "").strip()
        if aa:
            return False
        a = (t.artist or "").strip()
        if a:
            artists.add(a.lower())
    return len(artists) > 1


def _has_folder_cover(track_path: Path) -> bool:
    """Check if the track's folder contains a cover image."""
    if not track_path:
        return False
    parent = track_path.parent
    # First check exact names (fast path)
    for name in ALBUM_COVER_NAMES:
        if (parent / name).is_file():
            return True
    # Then check for any image file with cover/folder/art in the name
    try:
        for f in parent.iterdir():
            if not f.is_file():
                continue
            fl = f.name.lower()
            if any(kw in fl for kw in ("cover", "folder", "front", "album", "art")) and fl.endswith((".jpg", ".jpeg", ".png", ".webp")):
                return True
    except OSError:
        pass
    return False


def _has_embedded_cover(track_path: Path) -> bool:
    try:
        audio = MutagenFile(str(track_path), easy=False)
    except Exception:
        return False
    if not audio:
        return False
    if getattr(audio, "pictures", None):
        return True
    tags = getattr(audio, "tags", None)
    if not tags:
        return False
    try:
        if any(str(key).startswith("APIC") for key in tags.keys()):
            return True
        covers = tags.get("covr") if hasattr(tags, "get") else None
        return bool(covers)
    except Exception:
        return False
