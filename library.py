"""Local music library scanner."""

import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from mutagen import File as MutagenFile
from mutagen.id3 import ID3NoHeaderError

from models import Track
from config import get_settings

logger = logging.getLogger(__name__)


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

    def refresh(self, force: bool = False) -> List[Track]:
        """
        Scan the music directory and build track list.
        Returns list of Track objects.
        """
        if self._scan_in_progress and not force:
            logger.warning("Scan already in progress, returning cached")
            return self._track_cache

        self._scan_in_progress = True
        self._scan_error = None
        tracks = []

        try:
            if not self.music_root.exists():
                logger.warning(f"Music root does not exist: {self.music_root}")
                self._scan_error = f"Music directory not found: {self.music_root}"
                return []

            logger.info(f"Scanning music directory: {self.music_root}")
            for root, dirs, files in os.walk(self.music_root):
                for filename in files:
                    filepath = Path(root) / filename
                    if filepath.suffix.lower() in AUDIO_EXTENSIONS:
                        try:
                            track = self._create_track_from_file(filepath)
                            if track:
                                tracks.append(track)
                        except Exception as e:
                            logger.warning(f"Failed to read metadata for {filepath}: {e}")

            # Sort by path for consistency
            tracks.sort(key=lambda t: (t.title or '', t.path or Path('')))

            self._track_cache = tracks
            self._last_scan = datetime.now()
            logger.info(f"Library scan complete: {len(tracks)} tracks")

        except Exception as e:
            logger.error(f"Library scan failed: {e}")
            self._scan_error = str(e)
        finally:
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
            duration = None
            sample_rate_hz = None

            try:
                audio = MutagenFile(str(filepath), easy=True)
                if audio and audio.tags:
                    title = audio.get("title", [None])[0]
                    artist = audio.get("artist", [None])[0]
                if audio and audio.info:
                    duration = audio.info.length
                    sample_rate_hz = getattr(audio.info, "sample_rate", None)
                # WAV with ID3: easy=True may not map TIT2/TPE1 to title/artist
                if not title and not artist and audio and audio.tags:
                    tIT2 = audio.tags.get("TIT2")
                    if tIT2:
                        title = tIT2.text[0] if hasattr(tIT2, "text") else str(tIT2)
                    tpe1 = audio.tags.get("TPE1")
                    if tpe1:
                        artist = tpe1.text[0] if hasattr(tpe1, "text") else str(tpe1)

                if duration is None or sample_rate_hz is None:
                    raw_audio = MutagenFile(str(filepath), easy=False)
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

            if not title:
                title = filepath.stem
            if not artist:
                artist = None

            return Track(
                id=track_id,
                title=filepath.stem,
                artist=artist,
                source="local",
                url=str(filepath.absolute()),
                duration=duration,
                path=filepath,
                sample_rate_hz=int(sample_rate_hz) if sample_rate_hz else None,
            )

        except Exception as e:
            logger.warning(f"Failed to create track for {filepath}: {e}")
            return None

    def get_tracks(self, refresh: bool = False) -> List[Track]:
        """Get tracks list, optionally forcing a refresh."""
        if refresh or not self._track_cache:
            return self.refresh(force=refresh)
        return self._track_cache

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
