# SPDX-License-Identifier: AGPL-3.0-only

"""AutoSub dependency injection and shared job/task state."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)

_AUTO_SUB_JOBS: dict[str, dict[str, Any]] = {}
_auto_sub_lock: asyncio.Lock = asyncio.Lock()
_AUTO_SUB_WORKER_TASKS: set[asyncio.Task[Any]] = set()
_AUTO_SUB_CLEANUP_TASKS: set[asyncio.Task[Any]] = set()
# Owner objects are keyed by job id and never stored inside the job dicts.
_AUTO_SUB_CANDIDATE_OWNERS: dict[str, Any] = {}

_autosub_deps: AutoSubDependencies | None = None


@dataclass(frozen=True)
class AutoSubDependencies:
    """Application services injected from main.py.

    The DSP runtime accessor is lifecycle-owned; the measurement store,
    measurement sample-rate session and DSP manager are persistent
    module-scope singletons resolved late-bound at call time.
    """

    get_dsp_runtime: Callable[[], Any]
    get_measurement_store: Callable[[], Any]
    get_measurement_session: Callable[[], Any]
    get_dsp_manager: Callable[[], Any]
    # Fail-closed optionals for consumers without output-state composition.
    get_output_service: Callable[[], Any] | None = None
    create_candidate_session: Callable[..., Any] | None = None

def configure_dependencies(deps: AutoSubDependencies) -> None:
    """Bind the application services used by AutoSub."""
    global _autosub_deps
    _autosub_deps = deps

def _autosub_dependencies() -> AutoSubDependencies:
    if _autosub_deps is None:
        raise RuntimeError("AutoSub dependencies are not configured")
    return _autosub_deps

def _dsp_runtime() -> Any:
    """Resolve the native DSP runtime late-bound through the injected accessor."""
    return _autosub_dependencies().get_dsp_runtime()

def _measurement_store() -> Any:
    """Resolve the measurement store late-bound through the injected accessor."""
    return _autosub_dependencies().get_measurement_store()

def _measurement_session() -> Any:
    """Resolve the measurement sample-rate session late-bound through the injected accessor."""
    return _autosub_dependencies().get_measurement_session()

def _dsp_manager() -> Any:
    """Resolve the DSP manager late-bound through the injected accessor."""
    return _autosub_dependencies().get_dsp_manager()

def _output_service() -> Any:
    """Resolve the authoritative output service late-bound through the injected accessor."""
    accessor = _autosub_dependencies().get_output_service
    if accessor is None:
        raise RuntimeError("AutoSub output service dependency is not configured")
    return accessor()

def create_candidate_session(*args: Any, **kwargs: Any) -> Any:
    """Create an AutoSub candidate session through the injected late-bound factory."""
    factory = _autosub_dependencies().create_candidate_session
    if factory is None:
        raise RuntimeError("AutoSub candidate session factory is not configured")
    return factory(*args, **kwargs)

def register_candidate_owner(job_id: str, owner: Any) -> None:
    """Attach a candidate session owner to a job, outside the job dict."""
    _AUTO_SUB_CANDIDATE_OWNERS[job_id] = owner

def _candidate_owner(job_id: str) -> Any:
    """Return the registered owner; a missing owner is a programming error."""
    owner = _AUTO_SUB_CANDIDATE_OWNERS.get(job_id)
    if owner is None:
        raise RuntimeError(f"No AutoSub candidate owner is registered for job {job_id!r}")
    return owner

def drop_candidate_owner(job_id: str) -> None:
    """Forget a job's candidate owner; dropping twice stays idempotent."""
    _AUTO_SUB_CANDIDATE_OWNERS.pop(job_id, None)

def activate_candidate_owner(job: dict[str, Any], session: Any) -> int:
    """Bind the frozen start only after guarded measurement entry owns the rate."""
    job_id = job["id"]
    if session is None or session.active_auto_sub_job_id != job_id:
        raise RuntimeError("AutoSub candidate activation requires measurement session ownership")
    rate = session.measurement_rate
    _candidate_owner(job_id).activate(rate)
    return rate

def is_optimization_active() -> bool:
    return bool(_auto_sub_lock and _auto_sub_lock.locked())

def _cleanup_stale_autosub_cancelling_jobs() -> None:
    """Promote stale cancelling AutoSub jobs when the lock is not held.

    If the lock can be acquired immediately, no worker is actively running,
    so any cancelling job is stale and can be marked cancelled.
    """
    try:
        acquired = _auto_sub_lock and _auto_sub_lock.locked()
    except RuntimeError:
        acquired = False
    if acquired:
        return  # Lock held, worker still active

    for job_id, job in list(_AUTO_SUB_JOBS.items()):
        if str(job.get("status") or "").lower() != "cancelling":
            continue
        logger.warning(
            "AUTOSUB stale running state recovered: job_id=%s status=cancelling->cancelled",
            job_id,
        )
        job["status"] = "cancelled"
        job["message"] = "Auto Sub Optimize cancelled."
        job["cancel_requested"] = True
        job["cancelled_at"] = job.get("cancelled_at") or datetime.now(timezone.utc).isoformat()

def _auto_sub_cancel_requested(job: dict[str, Any]) -> bool:
    status = str(job.get("status") or "").lower()
    return bool(job.get("cancel_requested")) or status in ("cancelled", "cancelling")

def _start_auto_sub_worker(coro) -> None:
    task = asyncio.create_task(coro)
    _AUTO_SUB_WORKER_TASKS.add(task)
    task.add_done_callback(_AUTO_SUB_WORKER_TASKS.discard)

async def shutdown() -> None:
    """Cooperatively cancel and drain all AutoSub-owned tasks."""
    measurement_store = _measurement_store()

    for job in _AUTO_SUB_JOBS.values():
        if str(job.get("status") or "") not in {"completed", "failed", "cancelled"}:
            job["status"] = "cancelling"
            job["cancel_requested"] = True
            sweep_id = str(job.get("current_sweep_id") or "")
            if sweep_id and measurement_store is not None:
                try:
                    measurement_store.cancel_job(sweep_id)
                except (KeyError, RuntimeError):
                    pass
    if _AUTO_SUB_WORKER_TASKS:
        await asyncio.gather(*list(_AUTO_SUB_WORKER_TASKS), return_exceptions=True)
    cleanup_tasks = list(_AUTO_SUB_CLEANUP_TASKS)
    for task in cleanup_tasks:
        task.cancel()
    if cleanup_tasks:
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)
