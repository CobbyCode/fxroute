# SPDX-License-Identifier: AGPL-3.0-only

"""Spotify control via playerctl / MPRIS.

Requires: playerctl (system package)
Controls the already-running local Spotify desktop client only.
No Spotify Web API / OAuth / secondary client.

Phase 2 note: spotifycli may be added later as an optional extension
for URI-based actions only. The app must work fully without it.
"""

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------

_playerctl_path: str | None = None


def _find_playerctl() -> str | None:
    global _playerctl_path
    if _playerctl_path is not None:
        return _playerctl_path
    _playerctl_path = shutil.which("playerctl")
    return _playerctl_path


def playerctl_available() -> bool:
    return _find_playerctl() is not None


def spotify_installed() -> bool:
    if shutil.which("spotify") is not None:
        return True

    flatpak_markers = [
        Path.home() / ".local/share/flatpak/app/com.spotify.Client",
        Path("/var/lib/flatpak/app/com.spotify.Client"),
        Path.home() / ".local/share/flatpak/exports/share/applications/com.spotify.Client.desktop",
        Path("/var/lib/flatpak/exports/share/applications/com.spotify.Client.desktop"),
        Path.home() / ".var/app/com.spotify.Client",
    ]
    return any(marker.exists() for marker in flatpak_markers)

# ---------------------------------------------------------------------------
# Source capability flags (source-agnostic model)
# ---------------------------------------------------------------------------

SPOTIFY_CAPABILITIES = {
    "transport": True,
    "shuffle": True,
    "loop": True,
    "seek": True,
    "progress": True,
    "volume": True,
}

# ---------------------------------------------------------------------------
# Low-level runner
# ---------------------------------------------------------------------------

async def _run(*args: str, timeout: float = 4.0) -> str | None:
    """Run a playerctl command, return stdout or None on failure."""
    cmd = _find_playerctl()
    if cmd is None:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            cmd, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode == 0:
            return stdout.decode().strip() or None
        return None
    except (asyncio.TimeoutError, OSError) as exc:
        logger.debug("playerctl %s failed: %s", args, exc)
        return None

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

async def get_status() -> dict[str, Any]:
    """Return structured status dict (source-agnostic shape)."""
    result: dict[str, Any] = {
        "available": playerctl_available(),
        "installed": spotify_installed(),
        "source": "spotify",
        "capabilities": SPOTIFY_CAPABILITIES,
        "status": "Stopped",
        "artist": "",
        "title": "",
        "album": "",
        "artUrl": "",
        "shuffle": False,
        "loop": "none",
        "position": 0.0,
        "duration": 0.0,
        "volume": 100,
    }

    if not result["available"]:
        return result

    meta = await _run("--player=spotify", "metadata", "--format",
                       "{{status}}|{{artist}}|{{title}}|{{album}}|{{mpris:length}}")
    if meta is None:
        return result

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

    art = await _run("--player=spotify", "metadata", "mpris:artUrl")
    if art:
        result["artUrl"] = art

    shuffle_val = await _run("--player=spotify", "shuffle")
    result["shuffle"] = shuffle_val == "On"

    loop_val = await _run("--player=spotify", "loop")
    if loop_val in ("Track", "Playlist"):
        result["loop"] = loop_val.lower()
    else:
        result["loop"] = "none"

    pos_str = await _run("--player=spotify", "position")
    if pos_str:
        try:
            result["position"] = float(pos_str)
        except (ValueError, TypeError):
            pass

    volume_str = await _run("--player=spotify", "volume")
    if volume_str:
        try:
            result["volume"] = max(0, min(100, round(float(volume_str) * 100)))
        except (ValueError, TypeError):
            pass

    return result


async def _run_and_refresh(*args: str, delay: float = 0.45) -> dict[str, Any]:
    """Run a playerctl command, wait for Spotify to settle, then return status."""
    await _run(*args)
    await asyncio.sleep(delay)
    return await get_status()


async def play() -> dict[str, Any]:
    return await _run_and_refresh("--player=spotify", "play")


async def pause() -> dict[str, Any]:
    return await _run_and_refresh("--player=spotify", "pause")


async def toggle() -> dict[str, Any]:
    return await _run_and_refresh("--player=spotify", "play-pause")


async def next_track() -> dict[str, Any]:
    return await _run_and_refresh("--player=spotify", "next")


async def previous() -> dict[str, Any]:
    return await _run_and_refresh("--player=spotify", "previous")


async def shuffle_toggle() -> dict[str, Any]:
    return await _run_and_refresh("--player=spotify", "shuffle", "Toggle")


async def loop_cycle() -> dict[str, Any]:
    current = await _run("--player=spotify", "loop")
    if current == "None":
        await _run("--player=spotify", "loop", "Track")
    elif current == "Track":
        await _run("--player=spotify", "loop", "Playlist")
    else:
        await _run("--player=spotify", "loop", "None")
    await asyncio.sleep(0.3)
    return await get_status()


async def seek_to(position_sec: float) -> dict[str, Any]:
    return await _run_and_refresh("--player=spotify", "position", str(position_sec), delay=0.55)


async def set_volume(percent: float) -> dict[str, Any]:
    normalized = max(0.0, min(1.0, percent / 100.0))
    return await _run_and_refresh("--player=spotify", "volume", f"{normalized:.4f}", delay=0.2)
