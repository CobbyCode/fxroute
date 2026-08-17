# SPDX-License-Identifier: AGPL-3.0-only

"""Streaming provider layer.

Owns the provider-agnostic models, capability declarations and the registry.
Spotify Desktop and spotifyd are both the ``spotify`` provider; Qobuz and
TIDAL are declared providers whose backends are not implemented yet.
"""

from streaming.base.capabilities import CAPABILITY_NAMES, Capabilities
from streaming.base.models import (
    Album,
    Artist,
    PlaybackState,
    Playlist,
    ProviderState,
    Track,
)
from streaming.base.provider import (
    DeclaredStreamingProvider,
    ProviderNotImplemented,
    ProviderRegistry,
    StreamingProvider,
)
from streaming.spotify.provider import (
    SPOTIFY_PREARM_SAMPLE_RATE_HZ,
    SpotifyProvider,
)
from streaming.qobuz import QobuzProvider
from streaming.tidal import TidalProvider

registry = ProviderRegistry()
registry.register(SpotifyProvider())
registry.register(QobuzProvider())
registry.register(TidalProvider())


def get_provider(provider_id: str) -> StreamingProvider | None:
    return registry.get(provider_id)


def describe_providers() -> list[dict]:
    return registry.describe_all()


__all__ = [
    "Album",
    "Artist",
    "CAPABILITY_NAMES",
    "Capabilities",
    "DeclaredStreamingProvider",
    "PlaybackState",
    "Playlist",
    "ProviderNotImplemented",
    "ProviderRegistry",
    "ProviderState",
    "SPOTIFY_PREARM_SAMPLE_RATE_HZ",
    "SpotifyProvider",
    "StreamingProvider",
    "Track",
    "describe_providers",
    "get_provider",
    "registry",
]
