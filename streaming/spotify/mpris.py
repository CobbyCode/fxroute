# SPDX-License-Identifier: AGPL-3.0-only

"""MPRIS/playerctl backend for the Spotify provider.

Owns playerctl discovery, subprocess execution and desktop/spotifyd backend
detection. This is the only module that talks to playerctl; the provider and
the rest of the application see a small helper surface.

Spotify Desktop and spotifyd both implement the single ``spotify`` provider:
the MPRIS player name is selected here from the players that are actually
running (with an install-profile fallback), and never leaks into the UI/API.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# MPRIS player names for the two supported Spotify backends. Spotify Desktop
# publishes ``spotify``; spotifyd 0.4.x publishes ``spotifyd.instance<PID>``.
SPOTIFY_DESKTOP_PLAYER = "spotify"
SPOTIFYD_PLAYER = "spotifyd"
SPOTIFYD_MPRIS_INSTANCE_PREFIX = "spotifyd."

# spotifyd 0.4.x registers its controls interface under
# ``rs.spotifyd.instance<PID>`` for the whole daemon lifetime, while the MPRIS
# player name only exists while a Spotify Connect session is active. The
# controls name therefore detects an idle (standby) spotifyd daemon.
SPOTIFYD_DBUS_NAME_PREFIX = "rs.spotifyd.instance"

# Bounded timeout for the running-player discovery subprocess; a stuck
# playerctl must not stall a status read.
PLAYER_LIST_TIMEOUT_SECONDS = 2.0

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


def is_spotifyd_player(name: str) -> bool:
    return name == SPOTIFYD_PLAYER or name.startswith(SPOTIFYD_MPRIS_INSTANCE_PREFIX)


def player_name(backend: str | None) -> str:
    """Map a backend name to its static playerctl MPRIS player name."""
    return SPOTIFYD_PLAYER if backend == "spotifyd" else SPOTIFY_DESKTOP_PLAYER


async def resolve_player_name(backend: str | None) -> str:
    """Resolve the concrete MPRIS player name to target.

    Spotify Desktop publishes ``spotify``.  spotifyd 0.4.x publishes
    ``spotifyd.instance<PID>``, so the bare ``spotifyd`` name is never the
    live player; this returns the actually running instance name when one is
    visible and falls back to the static name otherwise.
    """
    if backend != "spotifyd":
        return SPOTIFY_DESKTOP_PLAYER
    players = await list_players()
    for player in players:
        if is_spotifyd_player(player):
            return player
    return SPOTIFYD_PLAYER


async def list_players(timeout: float = PLAYER_LIST_TIMEOUT_SECONDS) -> list[str]:
    """Return the MPRIS player names currently visible to playerctl.

    Discovery is the single source of truth for "which player is running".
    A missing playerctl, a non-zero exit or a timeout yields an empty list.
    """
    cmd = _find_playerctl()
    if cmd is None:
        return []
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            cmd,
            "-l",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            return []
        return [line.strip() for line in stdout.decode(errors="ignore").splitlines() if line.strip()]
    except (asyncio.TimeoutError, OSError) as exc:
        logger.debug("playerctl -l failed: %s", exc)
        return []
    finally:
        await _stop_process(proc)


_dbus_send_path: str | None = None


def _find_dbus_send() -> str | None:
    global _dbus_send_path
    if _dbus_send_path is not None:
        return _dbus_send_path
    _dbus_send_path = shutil.which("dbus-send")
    return _dbus_send_path


async def spotifyd_standby(timeout: float = PLAYER_LIST_TIMEOUT_SECONDS) -> bool:
    """Return whether an idle spotifyd daemon is visible on the session bus.

    spotifyd 0.4.x keeps its controls interface name registered for the whole
    daemon lifetime but only exposes MPRIS while a Connect session is active,
    so this detects the daemon even when playerctl sees no Spotify player.
    """
    cmd = _find_dbus_send()
    if cmd is None:
        return False
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            cmd,
            "--session",
            "--print-reply",
            "--dest=org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus.ListNames",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            return False
        return SPOTIFYD_DBUS_NAME_PREFIX in stdout.decode(errors="ignore")
    except (asyncio.TimeoutError, OSError) as exc:
        logger.debug("dbus-send ListNames failed: %s", exc)
        return False
    finally:
        await _stop_process(proc)


async def detect_running_backend(timeout: float = PLAYER_LIST_TIMEOUT_SECONDS) -> str | None:
    """Return the Spotify backend whose MPRIS player is actually running.

    * exactly one running player -> that backend;
    * both running -> desktop wins deterministically (documented, stable);
    * none running -> None.

    spotifyd 0.4.x publishes ``spotifyd.instance<PID>`` so prefix match.
    """
    players = await list_players(timeout)
    if SPOTIFY_DESKTOP_PLAYER in players:
        return "desktop"
    if any(is_spotifyd_player(player) for player in players):
        return "spotifyd"
    return None


async def detect_backend(timeout: float = PLAYER_LIST_TIMEOUT_SECONDS) -> str | None:
    """Return the Spotify backend to target.

    The running MPRIS player is authoritative. When nothing is running, fall
    back to the install profile so a desktop client that is installed but not
    running still reports ``Stopped`` instead of unavailable (preserving the
    existing desktop behavior).
    """
    running = await detect_running_backend(timeout)
    if running is not None:
        return running
    if _spotify_desktop_installed():
        return "desktop"
    if _spotifyd_installed():
        return "spotifyd"
    return None


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
