# SPDX-License-Identifier: AGPL-3.0-only

"""Provider-agnostic foundations for the streaming layer."""

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
    "StreamingProvider",
    "Track",
]
