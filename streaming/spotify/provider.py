# SPDX-License-Identifier: AGPL-3.0-only

"""Spotify streaming provider.

Controls an already-running local Spotify player through playerctl / MPRIS.
Spotify Desktop and spotifyd are both implementations of this single
``spotify`` provider; the backend is selected inside
:mod:`streaming.spotify.mpris` from the players actually running (with an
install-profile fallback) and never reaches the UI/API.

No Spotify Web API / OAuth / secondary client.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, ClassVar

from streaming.base.capabilities import Capabilities
from streaming.base.provider import StreamingProvider
from streaming.spotify import mpris

logger = logging.getLogger(__name__)

# Canonical Spotify stream rate, owned by the provider. The playback
# transition stack uses it as the pre-arm rate for Spotify handoffs.
SPOTIFY_PREARM_SAMPLE_RATE_HZ = 44100


class SpotifyProvider(StreamingProvider):
    """Spotify playback via playerctl/MPRIS (desktop or spotifyd backend)."""

    provider_id: ClassVar[str] = "spotify"
    display_name: ClassVar[str] = "Spotify"

    def capabilities(self) -> Capabilities:
        # Transport-only: playerctl exposes no catalog (search/library/
        # favorites/playlists) without the Spotify Web API, which is out of
        # scope. Cover art is available via mpris:artUrl.
        return Capabilities(
            transport=True,
            seek=True,
            shuffle=True,
            loop=True,
            progress=True,
            volume=True,
            cover=True,
        )

    def is_installed(self) -> bool:
        return mpris.spotify_installed()

    async def is_available(self) -> bool:
        return mpris.playerctl_available()

    async def backend(self) -> str | None:
        return await mpris.detect_backend()

    async def _run_player(self, *args: str, timeout: float = 4.0) -> str | None:
        player = await mpris.resolve_player_name(await self.backend())
        return await mpris._run(f"--player={player}", *args, timeout=timeout)

    async def status(self) -> dict:
        result: dict[str, Any] = {
            "available": await self.is_available(),
            "installed": self.is_installed(),
            "source": self.provider_id,
            "capabilities": self.capabilities().to_dict(),
            "status": "Stopped",
            "artist": "",
            "title": "",
            "album": "",
            "trackId": "",
            "artUrl": "",
            "shuffle": False,
            "loop": "none",
            "position": 0.0,
            "duration": 0.0,
            "volume": 100,
            "connected": False,
        }

        if not result["available"]:
            return result

        # Resolve the backend once and reuse it for every playerctl read so a
        # single status call runs one discovery subprocess, not one per field.
        backend = await self.backend()
        if backend:
            result["backend"] = backend
        player = await mpris.resolve_player_name(backend)

        async def run(*args: str, timeout: float = 4.0) -> str | None:
            return await mpris._run(f"--player={player}", *args, timeout=timeout)

        meta = await run(
            "metadata", "--format",
            "{{status}}|{{artist}}|{{title}}|{{album}}|{{mpris:length}}|{{mpris:trackid}}",
        )
        if meta is None:
            # No visible MPRIS player. spotifyd 0.4.x hides its MPRIS
            # interface without an active Connect session; surface the
            # standby state so the UI can distinguish "backend waiting for
            # Connect" from a plain "nothing playing".
            if await mpris.spotifyd_standby():
                result["spotifyd_standby"] = True
                result["connected"] = True
            return result

        result["connected"] = True
        parts = meta.split("|")
        if len(parts) >= 1:
            result["status"] = parts[0]
        if len(parts) >= 2:
            result["artist"] = parts[1]
        if len(parts) >= 3:
            result["title"] = parts[2]
        if len(parts) >= 4:
            result["album"] = parts[3]
        if len(parts) >= 5:
            try:
                result["duration"] = float(parts[4]) / 1_000_000
            except (ValueError, TypeError):
                pass
        if len(parts) >= 6:
            result["trackId"] = parts[5]

        art = await run("metadata", "mpris:artUrl")
        if art:
            result["artUrl"] = art

        shuffle_val = await run("shuffle")
        result["shuffle"] = shuffle_val == "On"

        loop_val = await run("loop")
        if loop_val in ("Track", "Playlist"):
            result["loop"] = loop_val.lower()
        else:
            result["loop"] = "none"

        pos_str = await run("position")
        if pos_str:
            try:
                result["position"] = float(pos_str)
            except (ValueError, TypeError):
                pass

        volume_str = await run("volume")
        if volume_str:
            try:
                result["volume"] = max(0, min(100, round(float(volume_str) * 100)))
            except (ValueError, TypeError):
                pass

        return result

    async def _run_and_refresh(self, *args: str, delay: float = 0.45) -> dict:
        """Run a playerctl command, wait for the player to settle, then return status."""
        await self._run_player(*args)
        await asyncio.sleep(delay)
        return await self.status()

    async def play(self) -> dict:
        return await self._run_and_refresh("play")

    async def pause(self) -> dict:
        return await self._run_and_refresh("pause")

    async def toggle(self) -> dict:
        return await self._run_and_refresh("play-pause")

    async def next(self) -> dict:
        return await self._run_and_refresh("next")

    async def previous(self) -> dict:
        return await self._run_and_refresh("previous")

    async def shuffle(self) -> dict:
        return await self._run_and_refresh("shuffle", "Toggle")

    async def repeat(self) -> dict:
        current = await self._run_player("loop")
        if current == "None":
            await self._run_player("loop", "Track")
        elif current == "Track":
            await self._run_player("loop", "Playlist")
        else:
            await self._run_player("loop", "None")
        await asyncio.sleep(0.3)
        return await self.status()

    async def seek(self, position_sec: float) -> dict:
        return await self._run_and_refresh("position", str(position_sec), delay=0.55)

    async def set_volume(self, percent: float) -> dict:
        normalized = max(0.0, min(1.0, percent / 100.0))
        return await self._run_and_refresh("volume", f"{normalized:.4f}", delay=0.2)


_provider: SpotifyProvider | None = None


def _default() -> SpotifyProvider:
    global _provider
    if _provider is None:
        _provider = SpotifyProvider()
    return _provider


# Module-level functions keep the existing flat-dict wire contract for the
# playback stack (main.py, playback/runtime). They are thin delegations to the
# provider, not compatibility shims: the provider object owns the behavior.
async def get_status() -> dict:
    return await _default().status()


async def play() -> dict:
    return await _default().play()


async def pause() -> dict:
    return await _default().pause()


async def toggle() -> dict:
    return await _default().toggle()


async def next_track() -> dict:
    return await _default().next()


async def previous() -> dict:
    return await _default().previous()


async def shuffle_toggle() -> dict:
    return await _default().shuffle()


async def loop_cycle() -> dict:
    return await _default().repeat()


async def seek_to(position_sec: float) -> dict:
    return await _default().seek(position_sec)


async def set_volume(percent: float) -> dict:
    return await _default().set_volume(percent)
