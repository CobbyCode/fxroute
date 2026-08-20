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


# Canonical empty-state copy for the pairing/connectivity states, consumed by
# the shared provider-neutral player card (streaming.js reads
# ``empty_title``/``empty_message``; it never branches on the provider id).
_CONNECT_STATE_EMPTY_COPY = {
    "ready": (
        "Spotify Connect ready",
        "Select “FXRoute” in the Spotify app to start playback.",
    ),
    "connected": (
        "Spotify connected",
        "Playback is currently on another device. Select “FXRoute” in the Spotify app to take it over.",
    ),
    "offline": (
        "Spotify is not running",
        "Start spotifyd or Spotify Desktop to control Spotify.",
    ),
}


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
        player = mpris.player_name(await self.backend())
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
        }

        if not result["available"]:
            result["connect_state"] = "unavailable"
            return result

        # Resolve the backend once and reuse it for every playerctl read so a
        # single status call runs one discovery subprocess, not one per field.
        backend = await self.backend()
        if backend:
            result["backend"] = backend
        player = mpris.player_name(backend)

        async def run(*args: str, timeout: float = 4.0) -> str | None:
            return await mpris._run(f"--player={player}", *args, timeout=timeout)

        meta = await run(
            "metadata", "--format",
            "{{status}}|{{artist}}|{{title}}|{{album}}|{{mpris:length}}|{{mpris:trackid}}",
        )
        if meta is not None:
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

        result["connect_state"] = await self._connect_state(backend, result)
        result["ready"] = result["connect_state"] == "ready"
        state_copy = _CONNECT_STATE_EMPTY_COPY.get(result["connect_state"])
        if state_copy is not None:
            result["empty_title"], result["empty_message"] = state_copy
        return result

    async def _connect_state(self, backend: str | None, status: dict) -> str:
        """Derive a stable, closed-set connect state for the UI.

        States:
        * ``unavailable`` — no usable Spotify client
        * ``idle``        — desktop installed but not playing
        * ``ready``       — spotifyd running, not yet paired (Connect ready)
        * ``offline``     — spotifyd installed but the daemon is not running
        * ``connected``   — spotifyd connected, playback on another device
        * ``playing``/``paused`` — active Spotify renderer
        """
        if backend is None:
            return "unavailable"
        if backend == "desktop":
            playback = status.get("status")
            if playback in ("Playing", "Paused"):
                return playback.lower()
            # Desktop selected by the install fallback, but not running.
            return "idle"
        # spotifyd's Controls name is the pairing/session truth. MPRIS only
        # appears once spotifyd is the active playback device.
        playback = status.get("status")
        mpris_present = await mpris.detect_running_backend() == "spotifyd"
        if playback in ("Playing", "Paused") and mpris_present:
            return playback.lower()
        if await mpris.spotifyd_control_names():
            return "connected"
        if await mpris.spotifyd_process_running():
            return "ready"
        if mpris_present:
            # Keep active MPRIS evidence authoritative even if the process
            # probe races with a restart.
            return "paused"
        return "offline"

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

    async def transfer_playback(self) -> bool:
        """Request that Spotify playback move to an already-connected spotifyd.

        Only meaningful for the ``spotifyd`` backend.  Deliberately not
        reachable via the generic transport dispatch: this is explicit
        user-intent functionality surfaced through the dedicated start path.
        """
        return await mpris.transfer_playback()


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


async def transfer_playback() -> bool:
    return await _default().transfer_playback()
