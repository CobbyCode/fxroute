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

from streaming import activation
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
    """Describe all providers, annotated with the persisted enabled flag."""
    described = await registry.describe_all()
    enabled = activation.enabled_map([entry.get("id", "") for entry in described])
    for entry in described:
        entry["enabled"] = enabled.get(str(entry.get("id") or ""), True)
    return described


async def discover_providers() -> list[dict]:
    """Discover installed providers without runtime or remote probes."""
    described = await registry.discover_all()
    enabled = activation.enabled_map([entry.get("id", "") for entry in described])
    for entry in described:
        entry["enabled"] = enabled.get(str(entry.get("id") or ""), True)
    return described


__all__ = [
    "CAPABILITY_NAMES",
    "Capabilities",
    "DeclaredStreamingProvider",
    "ProviderNotImplemented",
    "ProviderRegistry",
    "SPOTIFY_PREARM_SAMPLE_RATE_HZ",
    "SpotifyProvider",
    "StreamingProvider",
    "activation",
    "describe_providers",
    "discover_providers",
    "get_provider",
    "registry",
]
