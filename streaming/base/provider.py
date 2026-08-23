# SPDX-License-Identifier: AGPL-3.0-only

"""Provider abstraction and registry for the streaming layer.

A provider implements :class:`StreamingProvider`. A provider that is declared
but not yet built (TIDAL before its backend exists) subclasses
:class:`DeclaredStreamingProvider`, which reports its capabilities and an
unavailable status without pretending to work.

Availability and backend are async: they probe runtime state (the running
MPRIS player for Spotify, the qbzd control plane for Qobuz), so they must not
block the event loop. Installation is a cheap sync filesystem/`shutil.which`
check.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from streaming.base.capabilities import Capabilities


class ProviderNotImplemented(RuntimeError):
    """Raised when a provider does not implement an operation."""

    def __init__(self, provider_id: str, operation: str) -> None:
        super().__init__(f"provider {provider_id!r} does not implement {operation!r}")
        self.provider_id = provider_id
        self.operation = operation


class StreamingProvider(ABC):
    """Contract every streaming provider implements.

    Transport methods raise :class:`ProviderNotImplemented` by default so a
    provider only needs to implement the operations it declares via
    :meth:`capabilities`.
    """

    provider_id: ClassVar[str]
    display_name: ClassVar[str]
    implemented: ClassVar[bool] = True

    @abstractmethod
    def capabilities(self) -> Capabilities:
        """Return the provider's declared capability surface."""

    @abstractmethod
    def is_installed(self) -> bool:
        """Return whether any backend for this provider is installed."""

    @abstractmethod
    async def is_available(self) -> bool:
        """Return whether the provider's transport backend is reachable."""

    @abstractmethod
    async def backend(self) -> str | None:
        """Return the active backend name, or None when none is available."""

    @abstractmethod
    async def status(self) -> dict:
        """Return the normalized provider/playback state as a dict."""

    async def is_authenticated(self) -> bool | None:
        """Return whether the provider's account is connected.

        ``None`` means "not applicable" (a provider without an account, e.g.
        Spotify which only controls an external player).  Providers with a
        login (Qobuz, TIDAL) override this to report the real auth state.
        """
        return None

    async def describe(self) -> dict:
        """Serializable provider summary for the generic API/UI layer."""
        return {
            "id": self.provider_id,
            "name": self.display_name,
            "implemented": self.implemented,
            "available": await self.is_available(),
            "installed": self.is_installed(),
            "authenticated": await self.is_authenticated(),
            "backend": await self.backend(),
            "capabilities": self.capabilities().to_dict(),
        }

    async def play(self) -> dict:
        raise ProviderNotImplemented(self.provider_id, "play")

    async def pause(self) -> dict:
        raise ProviderNotImplemented(self.provider_id, "pause")

    async def toggle(self) -> dict:
        raise ProviderNotImplemented(self.provider_id, "toggle")

    async def next(self) -> dict:
        raise ProviderNotImplemented(self.provider_id, "next")

    async def previous(self) -> dict:
        raise ProviderNotImplemented(self.provider_id, "previous")

    async def shuffle(self) -> dict:
        raise ProviderNotImplemented(self.provider_id, "shuffle")

    async def repeat(self) -> dict:
        raise ProviderNotImplemented(self.provider_id, "repeat")

    async def seek(self, position_sec: float) -> dict:
        raise ProviderNotImplemented(self.provider_id, "seek")

    async def set_volume(self, percent: float) -> dict:
        raise ProviderNotImplemented(self.provider_id, "set_volume")


class DeclaredStreamingProvider(StreamingProvider):
    """Base for a provider whose backend is declared but not implemented.

    It never reports itself as available/installed and never claims to
    perform transport actions; it only publishes its intended capabilities.
    """

    implemented: ClassVar[bool] = False

    def is_installed(self) -> bool:
        return False

    async def is_available(self) -> bool:
        return False

    async def backend(self) -> str | None:
        return None

    async def status(self) -> dict:
        return {
            "available": False,
            "installed": False,
            "source": self.provider_id,
            "capabilities": self.capabilities().to_dict(),
            "status": "Stopped",
        }


class ProviderRegistry:
    """Registry of streaming providers, keyed by provider id."""

    def __init__(self) -> None:
        self._providers: dict[str, StreamingProvider] = {}

    def register(self, provider: StreamingProvider) -> None:
        self._providers[provider.provider_id] = provider

    def get(self, provider_id: str) -> StreamingProvider | None:
        return self._providers.get(provider_id)

    def providers(self) -> list[StreamingProvider]:
        return list(self._providers.values())

    async def describe_all(self) -> list[dict]:
        described: list[dict] = []
        for provider in self.providers():
            described.append(await provider.describe())
        return described

    async def discover_all(self) -> list[dict]:
        """Describe installed providers without runtime or remote probes."""
        described: list[dict] = []
        for provider in self.providers():
            described.append({
                "id": provider.provider_id,
                "name": provider.display_name,
                "implemented": provider.implemented,
                "installed": provider.is_installed(),
                "capabilities": provider.capabilities().to_dict(),
            })
        return described
