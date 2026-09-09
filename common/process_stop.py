# SPDX-License-Identifier: AGPL-3.0-only

"""Canonical subprocess-stop lifecycle: terminate -> grace -> kill -> reap.

Single owner of the bounded command-child stop sequence previously
duplicated in audio/pw_link.py and dsp/peak_monitor.py (REFACTOR-018).
Grace periods stay caller parameters (e.g. 3 s pw-link default versus
1.0 s peak monitor); the best-effort cleanup log message and its
traceback detail are parameters as well, so each caller keeps its
identifiable log line. Stdlib and logging only.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


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


async def run_stop_shielded(
    stop_operation, *, cleanup_log: str, cleanup_log_exc_info: bool = False
) -> bool:
    """Run a stop operation shielded from caller cancellation.

    Runs the actual stop in its own task behind ``asyncio.shield``: even a
    second cancellation during the grace period cannot interrupt the stop
    sequence, so no child can be orphaned by caller cancellation.  Returns
    True when the caller was cancelled while draining; the caller must then
    propagate CancelledError (it wins over any timeout failure).  Cleanup
    errors are best-effort and logged.
    """
    cleanup_task = asyncio.create_task(stop_operation)
    cancelled = False
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            cancelled = True
    try:
        cleanup_task.result()
    except Exception:
        logger.debug(cleanup_log, exc_info=cleanup_log_exc_info)
    return cancelled


async def stop_command_child_cancellation_safe(
    proc,
    grace_seconds: float,
    *,
    cleanup_log: str = "FXRoute command child cleanup failed",
    cleanup_log_exc_info: bool = False,
) -> bool:
    """Stop and drain a command child shielded from caller cancellation.

    Same contract as ``stop_command_child``; the stop sequence runs in its
    own task behind ``asyncio.shield`` (see ``_run_stop_shielded``).
    """
    if proc is None or proc.returncode is not None:
        return False
    return await run_stop_shielded(
        stop_command_child(proc, grace_seconds),
        cleanup_log=cleanup_log,
        cleanup_log_exc_info=cleanup_log_exc_info,
    )
