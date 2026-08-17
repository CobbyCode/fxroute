# SPDX-License-Identifier: AGPL-3.0-only

"""Provider capability model.

A provider declares which functions it supports. The UI and API read these
flags instead of branching on provider identity (``spotify``, ``qobuz``,
``tidal``), so a new provider only has to publish its capabilities.

Canonical flag names:

* Transport/playback: ``transport`` (play/pause + previous/next as one
  control cluster), ``seek``, ``shuffle``, ``loop`` (repeat), ``progress``
  (position/duration reporting), ``volume``.
* Catalog/content: ``search``, ``library``, ``favorites``, ``playlists``,
  ``recommendations``, ``radio``, ``lyrics``, ``queue_editing``.
* Stream info: ``cover``, ``audio_format``, ``sample_rate``, ``bit_depth``.

``transport`` keeps the existing FXRoute UI contract and covers the
play/pause and next/previous capabilities; ``loop`` covers ``repeat``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

# Ordered capability surface. The order doubles as documentation and as the
# stable key order in ``Capabilities.to_dict()``.
CAPABILITY_NAMES: tuple[str, ...] = (
    # transport / playback
    "transport",
    "seek",
    "shuffle",
    "loop",
    "progress",
    "volume",
    # catalog / content
    "search",
    "library",
    "favorites",
    "playlists",
    "recommendations",
    "radio",
    "lyrics",
    "queue_editing",
    # stream info
    "cover",
    "audio_format",
    "sample_rate",
    "bit_depth",
)


@dataclass(frozen=True)
class Capabilities:
    """Immutable set of provider capability flags.

    Every flag defaults to ``False``; providers opt in explicitly. The flat
    dict produced by :meth:`to_dict` is the single source of truth for the
    API/UI boundary.
    """

    transport: bool = False
    seek: bool = False
    shuffle: bool = False
    loop: bool = False
    progress: bool = False
    volume: bool = False

    search: bool = False
    library: bool = False
    favorites: bool = False
    playlists: bool = False
    recommendations: bool = False
    radio: bool = False
    lyrics: bool = False
    queue_editing: bool = False

    cover: bool = False
    audio_format: bool = False
    sample_rate: bool = False
    bit_depth: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {name: bool(getattr(self, name)) for name in CAPABILITY_NAMES}

    @classmethod
    def from_dict(cls, data: Mapping[str, object] | None) -> "Capabilities":
        if not isinstance(data, Mapping):
            return cls()
        return cls(**{name: bool(data.get(name)) for name in CAPABILITY_NAMES})

    def supported(self) -> set[str]:
        """Return the set of capability names currently enabled."""
        return {name for name in CAPABILITY_NAMES if getattr(self, name)}
