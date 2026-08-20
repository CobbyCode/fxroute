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
# publishes ``spotify``; spotifyd 0.4.x publishes an instance-suffixed name
# ``spotifyd.instance<PID>`` (the PID part changes across service restarts),
# so spotifyd is matched by prefix and never by an exact static name.
SPOTIFY_DESKTOP_PLAYER = "spotify"
SPOTIFYD_PLAYER = "spotifyd"
SPOTIFYD_MPRIS_INSTANCE_PREFIX = "spotifyd."

# spotifyd publishes its Controls D-Bus name after pairing. It embeds the
# spotifyd PID: the name is ``rs.spotifyd.instance<PID>`` and the control
# interface lives at ``/rs/spotifyd/Controls`` (interface
# ``rs.spotifyd.Controls``, method ``TransferPlayback``).
SPOTIFYD_CONTROLS_PREFIX = "rs.spotifyd.instance"
SPOTIFYD_CONTROLS_PATH = "/rs/spotifyd/Controls"
SPOTIFYD_CONTROLS_INTERFACE = "rs.spotifyd.Controls"

# Bounded timeout for the running-player discovery subprocess; a stuck
# playerctl must not stall a status read.
PLAYER_LIST_TIMEOUT_SECONDS = 2.0

# Bounded timeout for session-bus (busctl/gdbus) spotifyd subprocesses.
SPOTIFYD_BUS_TIMEOUT_SECONDS = 2.0

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


def is_spotifyd_player(name: str) -> bool:
    """Return whether a playerctl ``-l`` entry belongs to spotifyd.

    Spotify Desktop uses the well-known ``spotify`` name.  spotifyd 0.4.x
    registers an instance-suffixed name ``spotifyd.instance<PID>`` that
    changes on each restart, so both the bare name (test fixtures and older
    builds) and the ``spotifyd.*`` instance form are accepted.
    """
    return name == SPOTIFYD_PLAYER or name.startswith(SPOTIFYD_MPRIS_INSTANCE_PREFIX)


def spotify_installed() -> bool:
    """Return whether any supported Spotify backend is installed.

    Desktop and spotifyd both implement the same ``spotify`` provider, so
    installation is the union of the two backends.
    """
    return _spotify_desktop_installed() or _spotifyd_installed()


def player_name(backend: str | None) -> str:
    """Map a backend name to the playerctl MPRIS player name."""
    return SPOTIFYD_PLAYER if backend == "spotifyd" else SPOTIFY_DESKTOP_PLAYER


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


async def detect_running_backend(timeout: float = PLAYER_LIST_TIMEOUT_SECONDS) -> str | None:
    """Return the Spotify backend whose MPRIS player is actually running.

    * exactly one running player -> that backend;
    * both running -> desktop wins deterministically (documented, stable);
    * none running -> None.

    spotifyd is matched by prefix (``spotifyd.instance<PID>``) so a PID
    change after a service restart is picked up automatically and no old bus
    name is ever cached.
    """
    players = await list_players(timeout)
    if SPOTIFY_DESKTOP_PLAYER in players:
        return "desktop"
    if any(is_spotifyd_player(player) for player in players):
        return "spotifyd"
    return None


async def detect_backend(timeout: float = PLAYER_LIST_TIMEOUT_SECONDS) -> str | None:
    """Return the Spotify backend to target.

    The running MPRIS player is authoritative (Desktop wins while both are
    running).  When nothing is running, prefer a backend whose daemon is
    actually up: spotifyd runs headless and is Connect-ready even before the
    first pairing, so it beats a Desktop that is merely installed.  The
    install profile remains the final fallback so an idle desktop or an
    offline spotifyd still reports its own state instead of ``None``.
    """
    running = await detect_running_backend(timeout)
    if running is not None:
        return running
    if await spotifyd_control_names():
        return "spotifyd"
    if await spotifyd_process_running():
        return "spotifyd"
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


async def _run_checked(*args: str, timeout: float = SPOTIFYD_BUS_TIMEOUT_SECONDS) -> str | None:
    """Run an external command, return stdout on exit code 0, else None.

    Used for the session-bus helpers (busctl/gdbus).  Every failure path is
    bounded and the child is always reaped, mirroring ``_run``.
    """
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode == 0:
            return stdout.decode(errors="ignore")
        return None
    except (asyncio.TimeoutError, OSError) as exc:
        logger.debug("%s failed: %s", args[0] if args else "<command>", exc)
        return None
    finally:
        await _stop_process(proc)


async def _list_session_bus_names(timeout: float = SPOTIFYD_BUS_TIMEOUT_SECONDS) -> list[str]:
    """Return the names present on the session bus (busctl ``--user list``)."""
    output = await _run_checked("busctl", "--user", "list", "--no-legend", timeout=timeout)
    if not output:
        return []
    names: list[str] = []
    for raw_line in output.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("NAME"):
            # Header line on systemd versions without --no-legend support.
            continue
        name = stripped.split(None, 1)[0]
        if name:
            names.append(name)
    return names


async def spotifyd_control_names(timeout: float = SPOTIFYD_BUS_TIMEOUT_SECONDS) -> list[str]:
    """Return the ``rs.spotifyd.instance<PID>`` names owned on the session bus.

    spotifyd owns this name (and its MPRIS name) only after a Spotify session
    is established — i.e. once paired/connected.  Before the first pairing
    there are no spotifyd bus names at all, so running-status is detected via
    process presence (:func:`spotifyd_process_running`), never via this
    helper.
    """
    names = await _list_session_bus_names(timeout)
    return [name for name in names if name.startswith(SPOTIFYD_CONTROLS_PREFIX)]


def spotifyd_pid_from_name(name: str) -> int | None:
    """Extract the spotifyd PID embedded in a controls bus name."""
    if not name or not name.startswith(SPOTIFYD_CONTROLS_PREFIX):
        return None
    suffix = name[len(SPOTIFYD_CONTROLS_PREFIX):]
    return int(suffix) if suffix.isdigit() else None


async def spotifyd_process_running(timeout: float = SPOTIFYD_BUS_TIMEOUT_SECONDS) -> bool:
    """Return whether the spotifyd daemon process is running.

    spotifyd publishes no D-Bus name before its first pairing, so the
    "daemon up but never paired" connect state must come from process
    presence.  This only classifies the daemon lifecycle: transport
    capability is still gated on MPRIS presence
    (:func:`detect_running_backend`).
    """
    output = await _run_checked("pgrep", "-x", "spotifyd", timeout=timeout)
    return output is not None and output.strip() != ""


async def transfer_playback(timeout: float = 4.0) -> bool:
    """Transfer Spotify playback to spotifyd via ``rs.spotifyd.Controls``.

    Resolves the live ``rs.spotifyd.instance<PID>`` bus name (never a cached
    one), then calls ``TransferPlayback`` on ``/rs/spotifyd/Controls``.  This
    is an explicit user-intent operation; it is never used from status
    polling.  Returns False when spotifyd is not reachable or the call fails.
    """
    names = await spotifyd_control_names()
    if not names:
        return False
    # Several instances would be unusual; prefer the most recent PID.
    target = sorted(names, key=lambda name: spotifyd_pid_from_name(name) or 0)[-1]
    output = await _run_checked(
        "gdbus", "call", "--session",
        "--dest", target,
        "--object-path", SPOTIFYD_CONTROLS_PATH,
        "--method", f"{SPOTIFYD_CONTROLS_INTERFACE}.TransferPlayback",
        timeout=timeout,
    )
    if output is None:
        logger.warning("spotifyd TransferPlayback failed for bus name %s", target)
        return False
    logger.info("spotifyd TransferPlayback requested on %s", target)
    return True
