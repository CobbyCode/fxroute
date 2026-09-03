"""Execution and ownership lifecycle for measurement jobs."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import threading
import time
from copy import deepcopy
from typing import Any, Callable

logger = logging.getLogger(__name__)


class MeasurementJobRunner:
    """Own measurement worker tasks, child processes, and terminal cleanup."""

    def __init__(
        self,
        *,
        get_job: Callable[[str], dict[str, Any]],
        persist_job: Callable[[dict[str, Any]], None],
        public_result: Callable[[dict[str, Any]], dict[str, Any]],
        cleanup_job: Callable[[str], None],
        retain_history: Callable[[], None],
        utc_now: Callable[[], str],
        is_terminal: Callable[[Any], bool],
        raw_scope_enter: Callable[[], Any] | None = None,
        raw_scope_exit: Callable[[bool], Any] | None = None,
        effect_bypass_setter: Callable[[bool], Any] | None = None,
        active_scope_enter: Callable[[], Any] | None = None,
        active_scope_exit: Callable[[bool], Any] | None = None,
    ):
        self._get_job = get_job
        self._persist_job = persist_job
        self._public_result = public_result
        self._cleanup_job = cleanup_job
        self._retain_history = retain_history
        self._utc_now = utc_now
        self._is_terminal = is_terminal
        self._raw_scope_enter = raw_scope_enter
        self._raw_scope_exit = raw_scope_exit
        self._effect_bypass_setter = effect_bypass_setter
        self._active_scope_enter = active_scope_enter
        self._active_scope_exit = active_scope_exit
        self.tasks: dict[str, asyncio.Task[Any]] = {}
        self.processes: dict[str, list[subprocess.Popen[str]]] = {}
        self.process_lock = threading.Lock()
        self.cancelled_jobs: set[str] = set()
        self.shutting_down = False

    def start(self, job_id: str, job: dict[str, Any], executor: Callable[[dict[str, Any]], dict[str, Any]]) -> asyncio.Task[Any]:
        task = asyncio.create_task(self.run(job_id, job, executor))
        self.tasks[job_id] = task
        task.add_done_callback(lambda completed: self._task_done(job_id, completed))
        return task

    async def run(self, job_id: str, job: dict[str, Any], executor: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        # Temporary phase instrumentation (no logic impact).
        _rm: dict[str, float] = {"run_start": time.monotonic()}
        with self.process_lock:
            was_cancelling_before = job_id in self.cancelled_jobs
            if not was_cancelling_before:
                job["status"] = "running"
                job["updated_at"] = self._utc_now()
                job["message"] = "Running L/R repeat…" if job.get("job_kind") == "lr-repeat" else "Running sweep…"
                self._persist_job(job)
        previous_effect_bypass = None
        scope_owned = False
        try:
            if was_cancelling_before:
                raise RuntimeError("Measurement cancelled.")
            if job.get("measurement_scope") == "raw_helper":
                enter = self._raw_scope_enter or (
                    (lambda: self._effect_bypass_setter(True))
                    if callable(self._effect_bypass_setter) else None
                )
                if not callable(enter):
                    raise RuntimeError("Native DSP effect-bypass control is unavailable")
                previous_effect_bypass = await enter()
                scope_owned = True
            elif callable(self._active_scope_enter):
                previous_effect_bypass = await self._active_scope_enter()
                scope_owned = True

            _rm["scope_done"] = time.monotonic()
            worker_task = asyncio.create_task(asyncio.to_thread(executor, deepcopy(job)))
            _rm["submitted"] = time.monotonic()
            try:
                result = await asyncio.shield(worker_task)
            except asyncio.CancelledError:
                self.cancel_job(job_id, job)
                while not worker_task.done():
                    try:
                        await asyncio.shield(worker_task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if worker_task.done() and not worker_task.cancelled():
                    try:
                        worker_task.result()
                    except Exception:
                        pass
                raise

            _rm["worker_back"] = time.monotonic()
            with self.process_lock:
                if not self._is_terminal(job.get("status")):
                    if job_id in self.cancelled_jobs:
                        self._set_cancelled(job)
                    else:
                        job["status"] = "completed"
                        job["updated_at"] = self._utc_now()
                        job["message"] = result.get("message") or "Measurement finished."
                        job["result"] = self._public_result(result)
                        if isinstance(result.get("calibration"), dict):
                            job["calibration"] = deepcopy(result["calibration"])
                        job["error"] = None
            _rm["terminal"] = time.monotonic()
        except asyncio.CancelledError:
            with self.process_lock:
                self.cancelled_jobs.add(job_id)
                if not self._is_terminal(job.get("status")):
                    self._set_cancelled(job)
        except Exception as exc:
            with self.process_lock:
                if not self._is_terminal(job.get("status")):
                    if job_id in self.cancelled_jobs:
                        self._set_cancelled(job)
                    else:
                        job["status"] = "failed"
                        job["updated_at"] = self._utc_now()
                        job["message"] = str(exc) or "Measurement failed"
                        job["result"] = None
                        job["error"] = {"detail": str(exc)}
        finally:
            if scope_owned:
                try:
                    if job.get("measurement_scope") == "raw_helper":
                        exit_scope = self._raw_scope_exit or self._effect_bypass_setter
                    else:
                        exit_scope = self._active_scope_exit
                    await exit_scope(bool(previous_effect_bypass))
                except Exception:
                    logger.exception("Failed to restore native DSP effect bypass after measurement")
            try:
                _order = ("run_start", "scope_done", "submitted", "worker_back", "terminal")
                _prev = _rm.get("run_start", 0.0)
                _durs: dict[str, float] = {}
                for _key in _order[1:]:
                    if _key in _rm:
                        _durs[_key] = round((_rm[_key] - _prev) * 1000.0, 1)
                        _prev = _rm[_key]
                logger.info("CAPRUN-PHASES job=%s phases_ms=%s", job_id, json.dumps(_durs, sort_keys=True))
            except Exception:
                pass
            with self.process_lock:
                self.processes.pop(job_id, None)
            try:
                self._persist_job(job)
            except Exception:
                logger.exception("Failed to persist terminal measurement job %s", job_id)
            self._cleanup_job(job_id)
            self._retain_history()

    def cancel_job(self, job_id: str, job: dict[str, Any]) -> dict[str, Any]:
        with self.process_lock:
            if self._is_terminal(job.get("status")):
                return deepcopy(job)
            self.cancelled_jobs.add(job_id)
            processes = list(self.processes.get(job_id, []))
            self._set_cancelled(job, status="cancelling")
        for process in processes:
            try:
                if process.poll() is None:
                    process.terminate()
            except Exception:
                pass
        return deepcopy(job)

    async def shutdown(self, active_job_ids: list[str], jobs: dict[str, dict[str, Any]]) -> None:
        self.shutting_down = True
        for job_id in active_job_ids:
            self.cancel_job(job_id, jobs[job_id])
        with self.process_lock:
            processes = [p for items in self.processes.values() for p in items if p.poll() is None]
        for process in processes:
            try:
                await asyncio.to_thread(process.wait, 3)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, 3)
            except Exception:
                logger.exception("Failed to stop measurement process during shutdown")
        tasks = [task for task in self.tasks.values() if not task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        with self.process_lock:
            remaining = [p for items in self.processes.values() for p in items if p.poll() is None]
        for process in remaining:
            try:
                process.terminate()
                await asyncio.to_thread(process.wait, 3)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, 3)
            except Exception:
                logger.exception("Failed to stop measurement process during shutdown")
        with self.process_lock:
            self.processes.clear()

    def start_process(self, job_id: str, command: list[str]) -> subprocess.Popen[str]:
        with self.process_lock:
            if job_id in self.cancelled_jobs:
                raise RuntimeError("Measurement cancelled.")
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.processes.setdefault(job_id, []).append(process)
            return process

    def _set_cancelled(self, job: dict[str, Any], *, status: str = "cancelled") -> None:
        job["status"] = status
        job["updated_at"] = self._utc_now()
        job["message"] = "Measurement cancelled."
        job["error"] = None
        if status == "cancelled":
            job["result"] = None
        self._persist_job(job)

    def _task_done(self, job_id: str, task: asyncio.Task[Any]) -> None:
        if not task.cancelled():
            return
        try:
            job = self._get_job(job_id)
        except KeyError:
            return
        if self._is_terminal(job.get("status")) or job.get("status") != "queued":
            return
        with self.process_lock:
            self.cancelled_jobs.add(job_id)
            self._set_cancelled(job)
