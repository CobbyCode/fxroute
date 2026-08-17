# SPDX-License-Identifier: AGPL-3.0-only

"""MPRIS/playerctl backend for the Spotify provider.

Owns playerctl discovery, subprocess execution and desktop/spotifyd backend
detection. This is the only module that talks to playerctl; the provider and
the rest of the application see a small helper surface.

Spotify Desktop and spotifyd both implement the single ``spotify`` provider:
the MPRIS player name is selected here, based on which backend is installed,
and never leaks into the UI/API.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# MPRIS player names for the two supported Spotify backends. Spotify Desktop
# publishes ``spotify``; spotifyd publishes ``spotifyd``.
SPOTIFY_DESKTOP_PLAYER = "spotify"
SPOTIFYD_PLAYER = "spotifyd"

_playerctl_path: str | None = None


def _find_playerctl() -> str | None:
    global _playerctl_path
    if _playerctl_path is not None:
        return _playerctl_path
    _playerctl_path = shutil.which("playerctl")
    return _playerctl_path


def playerctl_available() -> bool:
    """Return whether the playerctl control tool is installed."""
    return _find_playerctl() is not None


def _spotify_desktop_installed() -> bool:
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


def _spotifyd_installed() -> bool:
    return shutil.which("spotifyd") is not None


def spotify_installed() -> bool:
    """Return whether any supported Spotify backend is installed.

    Desktop and spotifyd both implement the same ``spotify`` provider, so
    installation is the union of the two backends.
    """
    return _spotify_desktop_installed() or _spotifyd_installed()


def detect_backend() -> str | None:
    """Return the Spotify backend to target, or None when none is installed.

    Desktop is preferred on desktop systems; spotifyd is used when it is the
    only installed backend (headless/ARM). Dynamic detection of the running
    MPRIS player is intentionally left out: the two backends are exclusive by
    install profile, and a sync subprocess must not run on the hot status path.
    """
    if _spotify_desktop_installed():
        return "desktop"
    if _spotifyd_installed():
        return "spotifyd"
    return None


def player_name(backend: str | None) -> str:
    """Map a backend name to the playerctl MPRIS player name."""
    return SPOTIFYD_PLAYER if backend == "spotifyd" else SPOTIFY_DESKTOP_PLAYER


async def _stop_process(proc: asyncio.subprocess.Process | None) -> None:
    """Terminate a still-running subprocess, escalate to kill, then wait.

    Guarantees the child is reaped (no orphaned/zombie playerctl processes),
    also on timeout or cancellation. Safe to call on already-exited processes.
    """
    if proc is None or proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=1.0)
        return
    except (asyncio.TimeoutError, ProcessLookupError, ChildProcessError):
        pass
    try:
        proc.kill()
    except (ProcessLookupError, ChildProcessError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=1.0)
    except (asyncio.TimeoutError, ProcessLookupError, ChildProcessError):
        pass


async def _run(*args: str, timeout: float = 4.0) -> str | None:
    """Run a playerctl command, return stdout or None on failure."""
    cmd = _find_playerctl()
    if cmd is None:
        return None
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            cmd,
            *args,
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
    finally:
        # Timeout or cancellation leaves the child running; reap it so no
        # playerctl process is orphaned.
        await _stop_process(proc)
