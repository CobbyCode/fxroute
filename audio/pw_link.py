# SPDX-License-Identifier: AGPL-3.0-only
"""Low-level PipeWire ``pw-link`` command and child-reaping primitives.

Stateless helpers moved out of ``main.py``.  No imports from ``main`` or
other project modules (stdlib only), so every consumer (transition
orchestration, Bluetooth/external-input routing, silent-active diagnosis)
uses the same bounded command path.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

PW_LINK_COMMAND_TIMEOUT_SECONDS = 10
PW_LINK_TERMINATE_GRACE_SECONDS = 3


async def stop_command_child(proc, grace_seconds: float) -> None:
    """Terminate a still-running command child and drain it terminally.

    terminate -> bounded communicate() (drains stdout+stderr) -> if the
    child ignores SIGTERM: kill -> bounded communicate().  Process and both
    pipes are thereby always worked off terminally; a final wait() guards
    against a pathological case where even the killed child's pipes never
    close.  Already-exited processes are handled cheaply.
    """
    if proc is None or proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.communicate(), timeout=grace_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await asyncio.wait_for(proc.communicate(), timeout=grace_seconds)
        except asyncio.TimeoutError:
            await proc.wait()


async def stop_command_child_cancellation_safe(proc, grace_seconds: float) -> bool:
    """Stop and drain a command child shielded from caller cancellation.

    Runs the actual stop in its own task behind ``asyncio.shield``: even a
    second cancellation during the grace period cannot interrupt the
    terminate/grace/kill/pipe-drain sequence, so no child can be orphaned by
    caller cancellation.  Returns True when the caller was cancelled while
    draining; the caller must then propagate CancelledError (it wins over
    any timeout failure).  Cleanup errors are best-effort and swallowed.
    """
    if proc is None or proc.returncode is not None:
        return False
    cleanup_task = asyncio.create_task(stop_command_child(proc, grace_seconds))
    cancelled = False
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            cancelled = True
    try:
        cleanup_task.result()
    except Exception:
        logger.debug("FXRoute command child cleanup failed", exc_info=True)
    return cancelled


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
        except Exception:
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
