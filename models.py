"""Data models for the FXRoute."""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from pydantic import BaseModel


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
    favorite: bool = False  # user-starred track, independent of album favorite

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
            "favorite": self.favorite,
        }


class PlayRequest(BaseModel):
    source: str
    track_id: str
    url: Optional[str] = None
    queue_track_ids: Optional[list[str]] = None
    shuffle: bool = False
    loop: bool = False


class PlaylistSaveRequest(BaseModel):
    name: str
    track_ids: list[str]


class DeleteTracksRequest(BaseModel):
    track_ids: list[str]


class DeleteFolderRequest(BaseModel):
    folder: str


class DownloadTracksRequest(BaseModel):
    track_ids: list[str]
