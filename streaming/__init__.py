# SPDX-License-Identifier: AGPL-3.0-only

"""Streaming provider layer.

Owns the provider-agnostic capability declarations and the registry.
Spotify Desktop and spotifyd are both the ``spotify`` provider; Qobuz is
implemented via the ``qbzd`` daemon; TIDAL is a native provider via
``tidalapi`` whose playback rides the shared FXRoute owner.

Providers normalize their backend objects into plain dicts at the provider
boundary; those dicts (not dataclass models) are the wire contract that
crosses to the API/UI.
"""

from streaming.base.capabilities import CAPABILITY_NAMES, Capabilities
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


async def describe_providers() -> list[dict]:
    return await registry.describe_all()


__all__ = [
    "CAPABILITY_NAMES",
    "Capabilities",
    "DeclaredStreamingProvider",
    "ProviderNotImplemented",
    "ProviderRegistry",
    "SPOTIFY_PREARM_SAMPLE_RATE_HZ",
    "SpotifyProvider",
    "StreamingProvider",
    "describe_providers",
    "get_provider",
    "registry",
]
