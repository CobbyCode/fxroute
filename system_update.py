# SPDX-License-Identifier: AGPL-3.0-only

"""System update orchestration: update-script lifecycle and deferred restart.

Extracted verbatim from main.py (REFACTOR-012). Behavior is identical to
the previous inline implementation. This module owns the process-group
lifecycle of scripts/update_fxroute.sh (own session, exactly one
communicate() drain, TERM -> grace -> group SIGKILL escalation, identical
cleanup on timeout and on caller cancellation, timeout reported through
the established result shape), the exclusive update-operation guard (a
second check/update/restore is rejected with HTTP 409 and the guard is
released on success, timeout, cancellation and ordinary exceptions), the
bounded deferred FXRoute service restart (systemd --user --no-block
enqueue, bounded systemctl client, timeout only logged) and the pure
restart-scheduling decision (stdout markers for update, plain returncode
for restore).

It owns no HTTP routes, no origin policy and no version/service-name
resolution: script path, timeouts, version readers and the restart
scheduler inputs are supplied by the caller (main.py keeps its route
shells, its trusted-origin guard and its timeout constants so existing
``main.*`` patch points keep working). Application services that cannot
be imported cheaply are injected through :class:`SystemUpdateDeps`
(same *Deps pattern as the extracted dsp/streaming routers).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from fastapi import HTTPException

BASE_DIR = Path(__file__).resolve().parent

# Stdout markers proving scripts/update_fxroute.sh actually reconciled the
# deployment. Only then does a successful update need the deferred service
# restart; a no-op check stays restart-free.
UPDATE_OUTPUT_FAST_FORWARD_MARKER = "Pulling updates with fast-forward only."
UPDATE_OUTPUT_RECONCILE_MARKER = (
    "Checkout is current, but the deployment was not completed; retrying reconciliation."
)


@dataclass(frozen=True)
class SystemUpdateDeps:
    """Application services injected from main.py."""

    stop_process_group: Callable[..., Awaitable[bool]]
    stop_command_child: Callable[..., Awaitable[bool]]
    log_warning: Callable[..., None]


def should_schedule_restart(*, returncode: int, stdout: str, restore: bool) -> bool:
    """Decide whether a finished update-script run needs the deferred restart.

    Pure: no I/O, no locks. A restore always reconciles the checkout, so a
    successful run always needs the restart. An update only needs it when
    stdout proves the deployment was reconciled (fast-forward pull or
    retried reconciliation); a current-and-complete checkout stays running.
    """
    ok = returncode == 0
    if restore:
        # The restore script always reconciles the checkout; a successful
        # run therefore always needs the deferred service restart.
        return ok
    return ok and any(
        marker in stdout
        for marker in (
            UPDATE_OUTPUT_FAST_FORWARD_MARKER,
            UPDATE_OUTPUT_RECONCILE_MARKER,
        )
    )


async def run_update_script(
    script_path: Path,
    timeout: float,
    *args: str,
    terminate_grace_seconds: float,
    deps: SystemUpdateDeps,
) -> dict:
    """Run scripts/update_fxroute.sh in its own process group, bounded.

    The script and every git/pip/npm child it spawns live in a dedicated
    session (start_new_session=True), so the whole child tree can be
    signalled as a group via killpg(proc.pid).  stdout/stderr are drained
    by exactly one communicate() task; on timeout the group is
    TERM->grace->KILLed and that same task is drained terminally.  Caller
    cancellation runs the identical cleanup and re-raises CancelledError
    afterwards.  A timeout is reported through the existing result shape
    (returncode -1 plus a stderr note), never as a new exception.
    """
    if not script_path.exists():
        raise HTTPException(status_code=500, detail=f"Update script missing: {script_path}")
    proc = await asyncio.create_subprocess_exec(
        str(script_path),
        *args,
        cwd=str(BASE_DIR),
        start_new_session=True,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    communicate_task = asyncio.create_task(proc.communicate())
    try:
        stdout, stderr = await asyncio.wait_for(
            asyncio.shield(communicate_task), timeout=timeout
        )
    except asyncio.TimeoutError:
        if await deps.stop_process_group(
            proc, communicate_task, grace_seconds=terminate_grace_seconds
        ):
            raise asyncio.CancelledError
        try:
            stdout, stderr = communicate_task.result()
        except Exception:
            stdout, stderr = b"", b""
        return {
            "returncode": -1,
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace")
            + f"\nUpdate command timed out after {int(timeout)} seconds",
        }
    except asyncio.CancelledError:
        await deps.stop_process_group(
            proc, communicate_task, grace_seconds=terminate_grace_seconds
        )
        raise
    return {
        "returncode": proc.returncode,
        "stdout": stdout.decode(errors="replace"),
        "stderr": stderr.decode(errors="replace"),
    }


_update_operation_lock: asyncio.Lock | None = None


def get_update_operation_lock() -> asyncio.Lock:
    global _update_operation_lock
    if _update_operation_lock is None:
        _update_operation_lock = asyncio.Lock()
    return _update_operation_lock


async def run_update_operation(
    timeout: float,
    *args: str,
    script_path: Path,
    terminate_grace_seconds: float,
    deps: SystemUpdateDeps,
) -> dict:
    """Run an update-script invocation under the exclusive update guard.

    Only one update/check/restore may use update_fxroute.sh at a time; a
    second operation is rejected immediately with HTTP 409 instead of
    silently waiting behind the first one.  The guard is released in a
    finally, so success, timeout, cancellation and ordinary exceptions all
    free it again.
    """
    lock = get_update_operation_lock()
    if lock.locked():
        raise HTTPException(
            status_code=409, detail="An update operation is already in progress"
        )
    async with lock:
        return await run_update_script(
            script_path,
            timeout,
            *args,
            terminate_grace_seconds=terminate_grace_seconds,
            deps=deps,
        )


async def restart_service_after_response(
    service_name: str,
    *,
    restart_timeout_seconds: float,
    restart_terminate_grace_seconds: float,
    deps: SystemUpdateDeps,
) -> None:
    """Hand the FXRoute service restart to systemd, bounded.

    The restart job itself is executed by systemd --user; this process only
    enqueues it (--no-block) and must not wait for its own stop+start,
    because the response was already sent and this process is expected to be
    stopped by systemd as part of the job.  The systemctl client is bounded:
    a timeout means "restart outcome unknown" and is only logged; the
    already sent update/restore response stays unchanged.  Cancellation runs
    the same shielded terminal cleanup (no orphan even under a second
    cancellation) and re-raises CancelledError afterwards.  A nonzero
    systemctl exit already proves the job enqueue failed and is logged.
    """
    await asyncio.sleep(0.8)
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl",
            "--user",
            "--no-block",
            "restart",
            f"{service_name}.service",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception as exc:
        deps.log_warning("Deferred FXRoute service restart failed: %s", exc)
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=restart_timeout_seconds)
    except asyncio.TimeoutError:
        if await deps.stop_command_child(
            proc, restart_terminate_grace_seconds
        ):
            raise asyncio.CancelledError
        deps.log_warning(
            "Deferred FXRoute service restart timed out after %s s; restart outcome unknown",
            restart_timeout_seconds,
        )
    except asyncio.CancelledError:
        await deps.stop_command_child(
            proc, restart_terminate_grace_seconds
        )
        raise
    else:
        if proc.returncode != 0:
            deps.log_warning(
                "Deferred FXRoute service restart exited with code %s",
                proc.returncode,
            )
