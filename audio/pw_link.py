# SPDX-License-Identifier: AGPL-3.0-only
"""Low-level PipeWire ``pw-link`` command and child-reaping primitives.

Stateless helpers moved out of ``main.py``.  No imports from ``main``;
the shared stop lifecycle lives in the neutral ``common.process_stop``
module, so every consumer (transition orchestration, Bluetooth/
external-input routing, silent-active diagnosis, the update lifecycle)
uses the same bounded command path.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from common.process_stop import (
    run_stop_shielded,
    stop_command_child,
    stop_command_child_cancellation_safe,
)
logger = logging.getLogger(__name__)

PW_LINK_COMMAND_TIMEOUT_SECONDS = 10
PW_LINK_TERMINATE_GRACE_SECONDS = 3




async def stop_process_group(proc, communicate_task, *, grace_seconds: float) -> None:
    """Terminally stop a process group and drain its pipe task.

    SIGTERM to the whole group -> fixed grace period -> SIGKILL to the
    whole group -> drain the single communicate() task -> reap the shell.

    The group SIGKILL runs unconditionally after the grace period, even
    when the shell itself already exited: a descendant that ignores
    SIGTERM can outlive its parent while still belonging to the group
    (proven by the update lifecycle diagnosis).  ProcessLookupError from
    killpg simply means the group is already completely gone.
    """
    if proc is None:
        return
    pgid = proc.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    await asyncio.sleep(grace_seconds)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(communicate_task, timeout=grace_seconds)
    except asyncio.TimeoutError:
        logger.warning("Process-group pipes did not close after SIGKILL; reaping shell directly")
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
        except asyncio.TimeoutError:
            pass


async def stop_process_group_cancellation_safe(proc, communicate_task, *, grace_seconds: float) -> bool:
    """Stop a process group and drain it shielded from caller cancellation.

    Same contract as ``stop_process_group``; the stop sequence runs in its
    own task behind ``asyncio.shield`` (see ``run_stop_shielded``).
    """
    if proc is None:
        return False
    return await run_stop_shielded(
        stop_process_group(proc, communicate_task, grace_seconds=grace_seconds),
        cleanup_log="FXRoute update process-group cleanup failed",
    )


async def stop_pw_link_process(proc) -> None:
    """Terminate and fully reap a pw-link child (no zombie/pipe left behind)."""
    await stop_command_child(proc, PW_LINK_TERMINATE_GRACE_SECONDS)


async def run_pw_link_command(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "pw-link", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=PW_LINK_COMMAND_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        # A hanging PipeWire registry must not block a request forever.
        # Report the timeout as a controlled command failure, exactly like
        # the nonzero-exit path below.
        await stop_pw_link_process(proc)
        raise RuntimeError(
            f"pw-link {' '.join(args)} timed out after {PW_LINK_COMMAND_TIMEOUT_SECONDS}s"
        )
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode(errors="ignore").strip() or f"pw-link {' '.join(args)} failed")
    return stdout.decode(errors="ignore").strip()


async def disconnect_ports(source_ports: tuple[str, ...], sink_port: str) -> None:
    for source_port in source_ports:
        try:
            await run_pw_link_command("-d", source_port, sink_port)
            return
        except (RuntimeError, OSError) as exc:
            logger.debug("pw-link disconnect failed for %s: %s", source_port, exc)
            continue


async def connect_ports(source_ports: tuple[str, ...], sink_port: str) -> None:
    last_exc: Exception | None = None
    for source_port in source_ports:
        try:
            await run_pw_link_command(source_port, sink_port)
            return
        except Exception as exc:
            message = str(exc).lower()
            if "file exists" in message or "exists" in message or "already linked" in message:
                return
            last_exc = exc
    if last_exc:
        raise last_exc
