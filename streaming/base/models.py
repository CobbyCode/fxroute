# SPDX-License-Identifier: AGPL-3.0-only

"""Provider-agnostic data models shared by the streaming layer.

Providers normalize their backend objects (MPRIS/playerctl handles, qbzd and
tidalapi sessions) into these models. Provider-specific objects must never
reach the UI/API boundary; the serialized forms returned by ``to_dict`` are
the contract that crosses it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Artist:
    name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name}


@dataclass(frozen=True)
class Album:
    title: str = ""
    artists: tuple[Artist, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"title": self.title, "artists": [a.to_dict() for a in self.artists]}


@dataclass(frozen=True)
class Track:
    id: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    art_url: str = ""
    duration: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "art_url": self.art_url,
            "duration": self.duration,
        }


@dataclass(frozen=True)
class Playlist:
    id: str = ""
    name: str = ""
    track_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "track_count": self.track_count}


@dataclass(frozen=True)
class PlaybackState:
    """Normalized transport/position state for one provider."""

    status: str = "Stopped"
    track: Track | None = None
    shuffle: bool = False
    loop: str = "none"
    position: float = 0.0
    duration: float = 0.0
    volume: int = 100

    def to_dict(self) -> dict[str, Any]:
        track = self.track
        return {
            "status": self.status,
            "title": track.title if track else "",
            "artist": track.artist if track else "",
            "album": track.album if track else "",
            "trackId": track.id if track else "",
            "artUrl": track.art_url if track else "",
            "shuffle": self.shuffle,
            "loop": self.loop,
            "position": self.position,
            "duration": self.duration,
            "volume": self.volume,
        }


@dataclass(frozen=True)
class ProviderState:
    """Provider availability plus the normalized playback state."""

    provider_id: str
    available: bool = False
    installed: bool = False
    backend: str | None = None
    playback: PlaybackState | None = None
    capabilities: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "available": self.available,
            "installed": self.installed,
            "source": self.provider_id,
            "capabilities": dict(self.capabilities),
        }
        if self.backend:
            data["backend"] = self.backend
        if self.playback is not None:
            data.update(self.playback.to_dict())
        return data
