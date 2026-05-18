"""Data models for the FXRoute."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel


class PlaybackState(str, Enum):
    PLAYING = "playing"
    PAUSED = "paused"
    STOPPED = "stopped"


@dataclass
class Track:
    """Represents an audio track (local file or radio station)."""
    id: str
    title: str
    artist: Optional[str] = None
    album: Optional[str] = None
    album_artist: Optional[str] = None
    genre: Optional[str] = None
    year: Optional[int] = None
    track_number: Optional[int] = None
    disc_number: Optional[int] = None
    source: str = "local"  # "local" or "radio"
    url: Optional[str] = None
    duration: Optional[float] = None  # seconds
    path: Optional[Path] = None  # for local files
    sample_rate_hz: Optional[int] = None

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "album_artist": self.album_artist,
            "genre": self.genre,
            "year": self.year,
            "track_number": self.track_number,
            "disc_number": self.disc_number,
            "source": self.source,
            "url": self.url,
            "duration": self.duration,
            "path": str(self.path) if self.path else None,
            "sample_rate_hz": self.sample_rate_hz,
        }


@dataclass
class PlaybackStateData:
    """Current playback state."""
    state: PlaybackState = PlaybackState.STOPPED
    current_track: Optional[Track] = None
    position: float = 0.0  # seconds
    duration: float = 0.0
    volume: int = 100  # 0-100
    error: Optional[str] = None

    def to_dict(self):
        return {
            "state": self.state.value,
            "current_track": self.current_track.to_dict() if self.current_track else None,
            "position": self.position,
            "duration": self.duration,
            "volume": self.volume,
            "error": self.error,
        }


@dataclass
class DownloadProgress:
    """Download progress information."""
    url: str
    filename: str
    progress_percent: float = 0.0
    status: str = "downloading"  # "downloading", "complete", "error"
    error: Optional[str] = None
    started_at: datetime = field(default_factory=datetime.now)

    def to_dict(self):
        return {
            "url": self.url,
            "filename": self.filename,
            "progress_percent": self.progress_percent,
            "status": self.status,
            "error": self.error,
            "started_at": self.started_at.isoformat(),
        }


class PlayRequest(BaseModel):
    source: str
    track_id: str
    url: Optional[str] = None
    queue_track_ids: Optional[list[str]] = None
    shuffle: bool = False
    loop: bool = False


class StationUpsertRequest(BaseModel):
    name: Optional[str] = None
    stream_url: str
    custom_image_url: Optional[str] = None


class PlaylistSaveRequest(BaseModel):
    name: str
    track_ids: list[str]


class DeleteTracksRequest(BaseModel):
    track_ids: list[str]


class DeleteFolderRequest(BaseModel):
    folder: str


class DownloadTracksRequest(BaseModel):
    track_ids: list[str]
