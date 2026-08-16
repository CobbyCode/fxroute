# SPDX-License-Identifier: AGPL-3.0-only

"""Orphan mpv process discovery and termination for the FXRoute player."""

from __future__ import annotations

import logging
import os
import signal
import time

logger = logging.getLogger(__name__)

# Grace window between SIGTERM and SIGKILL when cleaning up orphan mpv
# processes; the process-listing loop polls inside this window.
_ORPHAN_SIGTERM_GRACE_SECONDS = 2.0


def _is_fxroute_mpv_cmdline(cmdline: str, socket_path: str) -> bool:
    """True only for FXRoute-owned mpv processes.

    FXRoute starts mpv with an exact argument pattern; any other mpv
    invocation (user players, different IPC socket) must never match, so a
    cleanup can never kill an unrelated mpv process.
    """
    marker = f"mpv --idle=yes --input-ipc-server={socket_path} "
    return cmdline.startswith(marker)


def _fxroute_mpv_pids(socket_path: str) -> list[int]:
    """Return PIDs of running FXRoute-owned mpv processes via /proc scan."""
    pids: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return pids
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                raw = handle.read()
        except OSError:
            continue
        cmdline = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        if _is_fxroute_mpv_cmdline(cmdline, socket_path):
            pids.append(int(entry))
    return sorted(pids)


def _stop_orphan_mpv_processes(socket_path: str, own_pid: int | None = None) -> None:
    """Terminate FXRoute-owned mpv processes left behind by killed service runs.

    A hard service kill (or a crashed Python) leaves the mpv child alive;
    on the next start those orphans compete for the same IPC socket and
    PipeWire node name.  Only processes whose cmdline matches the exact
    FXRoute mpv pattern are touched, never unrelated user mpv processes.
    """
    remaining = [pid for pid in _fxroute_mpv_pids(socket_path) if pid != own_pid]
    if not remaining:
        return
    logger.info("Found orphan FXRoute mpv processes (pids: %s), cleaning up", ", ".join(map(str, remaining)))
    for pid in remaining:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.monotonic() + _ORPHAN_SIGTERM_GRACE_SECONDS
    while time.monotonic() < deadline:
        remaining = [pid for pid in _fxroute_mpv_pids(socket_path) if pid != own_pid]
        if not remaining:
            return
        time.sleep(0.1)
    logger.warning("Orphan FXRoute mpv processes ignored SIGTERM (pids: %s), killing", ", ".join(map(str, remaining)))
    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
