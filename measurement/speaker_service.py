# SPDX-License-Identifier: AGPL-3.0-only
"""One-at-a-time Speaker Align application jobs without hardware ownership.

This service composes the shipped measurement boundaries — frozen
``SpeakerAlignment`` planning, serial way acquisition, ``propose``, the shared
per-side verification take and the trial/commit session — behind a small async
job lifecycle. It never touches hardware itself: acquisition, verification,
runtime staging and persistence arrive through injected callables, so the
whole flow is testable with fast doubles and the composition root wires the
real store and session later. No HTTP lives here.

Only one job may be active at a time (the measurement graph is
single-owner). Request validation raises synchronously before any job
record exists. Results are JSON-safe summaries: arrival/delay evidence and
overlap checks as plain floats, never IR waveforms or numpy values.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
from copy import deepcopy
from contextlib import nullcontext
from typing import Any, Callable
from uuid import uuid4

from measurement.speaker_acquisition import input_chain_key
from measurement.speaker_align import SpeakerAlignment
from measurement.speaker_apply import apply_and_confirm

TERMINAL_STATUSES = ("committed", "trial-done", "unconfirmed", "failed", "cancelled")

logger = logging.getLogger(__name__)


class SpeakerAlignStaleError(ValueError):
    """Planning and live state disagree; HTTP maps this to 409, not 400."""


class SpeakerAlignBusyError(RuntimeError):
    """Another alignment owns the job slot; HTTP maps this to 409."""


def _jsonable(value: Any) -> Any:
    """Convert evidence summaries to strict JSON values, failing loudly."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Speaker Align result must be finite for JSON")
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    raise TypeError(f"Speaker Align result is not JSON-safe: {type(value).__name__}")


def _summarize_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    """Summarize a real proposal; missing keys raise instead of zero-filling."""
    summary = {
        "start_revision": proposal["start_revision"],
        "processing_fingerprint": proposal["processing_fingerprint"],
        "arrival_ms": dict(proposal["arrival_ms"]),
        "added_delay_ms": dict(proposal["added_delay_ms"]),
        "reference_role": proposal["reference_role"],
    }
    if isinstance(proposal.get("way_levels_db"), dict):
        summary["way_levels_db"] = dict(proposal["way_levels_db"])
    if isinstance(proposal.get("added_gain_db"), dict):
        summary["added_gain_db"] = dict(proposal["added_gain_db"])
    return _jsonable(summary)


def _summarize_check(check: dict[str, Any]) -> dict[str, Any]:
    """Summarize a real confirmation check; missing keys raise loudly."""
    summary = {
        "confirmed": bool(check["confirmed"]),
        "reasons": [str(reason) for reason in check["reasons"]],
        "warnings": [str(warning) for warning in check.get("warnings") or []],
        "max_residual_ms": float(check["max_residual_ms"]),
        "before_spread_ms": float(check["before_spread_ms"]),
        "after_arrival_ms": dict(check["after_arrival_ms"]),
        "tolerance_ms": float(check["tolerance_ms"]),
        "pairs": check["pairs"],
    }
    if check.get("gain_spread_db") is not None:
        summary["gain_spread_db"] = float(check["gain_spread_db"])
    if check.get("before_gain_spread_db") is not None:
        summary["before_gain_spread_db"] = float(check["before_gain_spread_db"])
    if check.get("gain_tolerance_db") is not None:
        summary["gain_tolerance_db"] = float(check["gain_tolerance_db"])
    if isinstance(check.get("after_way_levels_db"), dict):
        summary["after_way_levels_db"] = dict(check["after_way_levels_db"])
    # How far each way's own arrival stood above its neighbours in the shared
    # take: the evidence that the residual could be told apart at all.
    isolation = check.get("way_isolation_db")
    if isinstance(isolation, dict):
        summary["way_isolation_db"] = {
            role: (None if value is None else float(value)) for role, value in isolation.items()}
    if check.get("isolation_margin_db") is not None:
        summary["isolation_margin_db"] = float(check["isolation_margin_db"])
    return _jsonable(summary)


def _session_identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"Speaker Align service requires a {label} identity")
    return value


class SpeakerAlignService:
    """Own Speaker Align job records, worker tasks and single-active ownership.

    ``start`` must be called with a running event loop: the worker task is
    bound to it, and ``cancel`` must be called from the same loop for the
    task cancellation to take effect (the cooperative flag works
    cross-thread). All blocking boundaries are the composition root's
    responsibility to keep fast or thread off.
    """

    def __init__(self, *, get_state: Callable[[], dict],
                 describe: Callable[[dict], dict],
                 acquire: Callable[..., Any],
                 confirm: Callable[..., Any],
                 create_session: Callable[..., Any],
                 freeze_live: Callable[..., dict],
                 on_committed: Callable[[dict], Any] | None = None,
                 job_scope: Callable[[str], Any] | None = None,
                 check_available: Callable[[], None] | None = None,
                 input_keeper: Callable[[str, dict], Any] | None = None):
        for name, bound in (("get_state", get_state), ("describe", describe),
                            ("acquire", acquire), ("confirm", confirm),
                            ("create_session", create_session),
                            ("freeze_live", freeze_live)):
            if not callable(bound):
                raise ValueError(f"Speaker Align service requires a {name} boundary")
        if on_committed is not None and not callable(on_committed):
            raise ValueError("Speaker Align service requires a callable on_committed hook")
        if input_keeper is not None and not callable(input_keeper):
            raise ValueError("Speaker Align service requires a callable input_keeper factory")
        self._get_state = get_state
        self._describe = describe
        self._acquire = acquire
        self._confirm = confirm
        self._create_session = create_session
        self._freeze_live = freeze_live
        self._on_committed = on_committed
        self._job_scope = job_scope or (lambda job_id: nullcontext())
        self._check_available = check_available or (lambda: None)
        self._input_keeper = input_keeper
        self._scopes: dict[str, Any] = {}
        self._guard = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._active_id: str | None = None
        self.max_retained_jobs = 20

    @property
    def active(self) -> bool:
        return self._active_id is not None

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for job_id in list(self._tasks):
            self.cancel(job_id)
        await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _public(job: dict[str, Any]) -> dict[str, Any]:
        """Detached job view: internal snapshots and cancel flags stay inside."""
        return deepcopy({key: job[key] for key in (
            "id", "side", "status", "message", "dry_run", "params", "result", "error")})

    def jobs(self) -> list[dict[str, Any]]:
        """Return detached public copies of every known job, newest last."""
        with self._guard:
            return [self._public(job) for job in self._jobs.values()]

    def status(self, job_id: str) -> dict[str, Any]:
        """Return a detached public copy of one job; unknown ids raise KeyError."""
        with self._guard:
            return self._public(self._jobs[job_id])

    def cancel(self, job_id: str) -> dict[str, Any]:
        """Request cancellation; terminal jobs are unaffected. Unknown ids raise KeyError."""
        with self._guard:
            job = self._jobs[job_id]
            if job["status"] in TERMINAL_STATUSES:
                return self._public(job)
            job["cancel_requested"] = True
            if job["status"] != "cancelling":
                job["status"] = "cancelling"
                job["message"] = "Cancelling speaker alignment…"
            task = self._tasks.get(job_id)
        if task is not None and job.get("worker_started"):
            task.cancel()
        # Cross-thread cancels can race a retention eviction between the two
        # guarded reads: a fully populated retention ring evicts the record
        # the first read just returned. The first read already validated the
        # job; when the second no longer finds it, that record is the honest
        # answer instead of a KeyError surfacing as HTTP 500.
        with self._guard:
            return self._public(self._jobs.get(job_id, job))

    def start(self, side: str, *, input_id: str, mic_input_channel: str | int | None = "1",
              reference_input_channel: str | int | None,
              reference_input_channel_left: str | int | None = None,
              reference_input_channel_right: str | int | None = None,
              reference_id: str, microphone_position_id: str,
              sweep_profile: dict[str, float] | None = None,
              dry_run: bool = False) -> str:
        """Validate a request synchronously and launch its worker; return the job id.

        Must be called with a running event loop (see the class contract).
        Request validation — including a freshness check of the frozen
        planning snapshot — raises before any job record exists. The worker
        reuses that frozen snapshot instead of re-reading live state, so a
        revision bump between ``start`` and the worker fails the job as
        stale instead of silently rebasing it.
        """
        if side not in ("left", "right"):
            raise ValueError("Speaker Align side must be left or right")
        if not isinstance(input_id, str) or not input_id.strip():
            raise ValueError("Speaker Align service requires a capture input id")
        reference_id = _session_identity(reference_id, "upstream reference")
        microphone_position_id = _session_identity(microphone_position_id, "microphone position")
        state = deepcopy(self._get_state())
        if state["modes"][state["active_mode"]]["selected_bank"] != "global":
            raise ValueError("Speaker Align is available only from Global")
        self._check_available()
        context = self._describe(state)
        alignment = SpeakerAlignment(
            state, side=side, output_key=context["output_key"], channels=context["channels"],
            sample_rate_hz=context["sample_rate_hz"], fingerprint=context["fingerprint"],
            reference_id=reference_id, microphone_position_id=microphone_position_id)
        live_target = self._freeze_live(
            state, output_key=context["output_key"], channels=context["channels"],
            sample_rate_hz=context["sample_rate_hz"], fingerprint=context["fingerprint"])
        self._require_fresh_live(alignment, live_target)
        job_id = uuid4().hex
        job = {
            "id": job_id, "side": side, "status": "queued", "message": "Speaker alignment queued.",
            "dry_run": bool(dry_run), "cancel_requested": False,
            "params": {
                "input_id": input_id, "mic_input_channel": mic_input_channel,
                "reference_input_channel": reference_input_channel,
                "reference_input_channel_left": reference_input_channel_left,
                "reference_input_channel_right": reference_input_channel_right,
                "reference_id": reference_id, "microphone_position_id": microphone_position_id,
                "sweep_profile": deepcopy(sweep_profile) if sweep_profile else None,
                "output_key": context["output_key"], "channels": context["channels"],
                "sample_rate_hz": context["sample_rate_hz"],
            },
            "internal": {"start_state": state, "context": context},
            "result": None, "error": None,
        }
        with self._guard:
            if self._active_id is not None:
                active = self._jobs.get(self._active_id)
                if active is not None and active["status"] not in TERMINAL_STATUSES:
                    raise SpeakerAlignBusyError("A speaker alignment is already running")
            self._jobs[job_id] = job
            self._active_id = job_id
            self._scopes[job_id] = self._job_scope(job_id)
            self._tasks[job_id] = asyncio.get_running_loop().create_task(self._run(job_id))
        return job_id

    @staticmethod
    def _require_fresh_live(alignment: SpeakerAlignment, live_target: dict[str, Any]) -> None:
        """Fail fast before any sweep when planning and live state already disagree."""
        requests = alignment.capture_requests()
        if not requests:
            raise ValueError("Speaker Align service found no speaker ways to align")
        frozen = requests[0]["measurement_target"]
        if (not isinstance(live_target, dict)
                or live_target.get("revision") != frozen.get("revision")
                or live_target.get("processing_fingerprint") != frozen.get("processing_fingerprint")):
            raise SpeakerAlignStaleError(
                "Speaker Align live target is stale; revision and processing must "
                "match the frozen requests before acquiring")

    def _is_cancel_requested(self, job_id: str) -> bool:
        with self._guard:
            job = self._jobs.get(job_id)
            return bool(job is not None and job.get("cancel_requested"))

    def _note(self, job_id: str, status: str, message: str) -> None:
        with self._guard:
            job = self._jobs[job_id]
            if job["status"] in TERMINAL_STATUSES:
                return
            job["status"] = status
            job["message"] = message

    def _finish(self, job_id: str, status: str, message: str, *,
                result: dict | None = None, error: str | None = None) -> None:
        self._jobs[job_id]["outcome"] = (status, message, result, error)

    def _publish_finish(self, job_id: str, status: str, message: str, *,
                result: dict | None = None, error: str | None = None) -> None:
        with self._guard:
            job = self._jobs[job_id]
            job["status"] = status
            job["message"] = message
            job["result"] = result
            job["error"] = error
            if self._active_id == job_id:
                self._active_id = None
            self._tasks.pop(job_id, None)
            terminal = [known for known, record in self._jobs.items()
                        if record["status"] in TERMINAL_STATUSES]
            while len(terminal) > max(1, int(self.max_retained_jobs)):
                evicted = terminal.pop(0)
                self._jobs.pop(evicted, None)
                self._tasks.pop(evicted, None)

    async def _run(self, job_id: str) -> None:
        self._jobs[job_id]["worker_started"] = True
        scope = self._scopes.pop(job_id)
        try:
            if self._is_cancel_requested(job_id):
                raise asyncio.CancelledError()
            async with scope:
                keeper = self._keeper_scope(job_id)
                async with keeper:
                    await self._run_job(job_id)
        except asyncio.CancelledError:
            self._finish(job_id, "cancelled", "Speaker alignment cancelled.")
        except Exception as exc:
            self._finish(job_id, "failed", f"Speaker alignment failed: {exc}", error=str(exc))
        finally:
            status, message, result, error = self._jobs[job_id].pop("outcome")
            self._publish_finish(job_id, status, message, result=result, error=error)

    def _keeper_scope(self, job_id: str) -> Any:
        """Hold the capture input open for the whole run, when wired.

        Without a factory the job runs unchanged (backward compatible).
        Factory failures fail the job loudly: an unheld input cannot
        guarantee stable timing.
        """
        if self._input_keeper is None:
            return nullcontext()
        with self._guard:
            params = deepcopy(self._jobs[job_id]["params"])
        return self._input_keeper(job_id, params)

    async def _run_job(self, job_id: str) -> None:
        with self._guard:
            frozen = deepcopy(self._jobs[job_id]["internal"])
            params = deepcopy(self._jobs[job_id]["params"])
            side = str(self._jobs[job_id]["side"])
            dry_run = bool(self._jobs[job_id]["dry_run"])
        probe = lambda: self._is_cancel_requested(job_id)
        try:
            # The worker reuses the frozen start snapshot: the fresh-head
            # drift gate below fails the job as stale instead of silently
            # rebasing the whole run. The frozen-derived live check after
            # it is defense in depth against a lying freeze boundary.
            fresh_state = deepcopy(self._get_state())
            fresh_context = self._describe(fresh_state)
            if (fresh_state.get("revision") != frozen["start_state"].get("revision")
                    or fresh_context.get("fingerprint")
                    != frozen["context"].get("fingerprint")):
                raise SpeakerAlignStaleError(
                    "Speaker Align output state is stale: it moved after the job "
                    "started; no rebase is performed, start a new alignment")
            state = frozen["start_state"]
            context = frozen["context"]
            alignment = SpeakerAlignment(
                deepcopy(state), side=side, output_key=context["output_key"],
                channels=context["channels"], sample_rate_hz=context["sample_rate_hz"],
                fingerprint=context["fingerprint"],
                reference_id=params["reference_id"],
                microphone_position_id=params["microphone_position_id"])
            live_target = self._freeze_live(
                deepcopy(state), output_key=context["output_key"],
                channels=context["channels"],
                sample_rate_hz=context["sample_rate_hz"],
                fingerprint=context["fingerprint"])
            self._require_fresh_live(alignment, live_target)

            session = self._create_session(
                state, output_key=context["output_key"], channels=context["channels"],
                sample_rate_hz=context["sample_rate_hz"])
            await session.stage_candidate(state)
            self._note(job_id, "acquiring", "Measuring speaker ways…")
            first = await self._acquire(
                alignment, input_id=params["input_id"],
                mic_input_channel=params["mic_input_channel"],
                reference_input_channel=params["reference_input_channel"],
                reference_input_channel_left=params.get("reference_input_channel_left"),
                reference_input_channel_right=params.get("reference_input_channel_right"),
                reference_id=params["reference_id"],
                microphone_position_id=params["microphone_position_id"],
                sweep_profile=deepcopy(params["sweep_profile"]),
                cancel_requested=probe,
                expected_native_context=session.measurement_context(),
                on_progress=lambda role, index, count: self._note(
                    job_id, "acquiring", f"Measuring {role.replace('_', ' ')} ({index}/{count})…"))
            proposal = alignment.propose(
                first["captures"], live_target=live_target, cancel_requested=probe)
            self._note(job_id, "confirming", "Confirming alignment acoustically…")

            async def confirm() -> dict:
                """One shared take per side; the residual is a real acoustic offset."""
                verification = await self._confirm(
                    alignment, input_id=params["input_id"],
                    mic_input_channel=params["mic_input_channel"],
                    reference_input_channel=params["reference_input_channel"],
                    reference_input_channel_left=params.get("reference_input_channel_left"),
                    reference_input_channel_right=params.get("reference_input_channel_right"),
                    reference_id=params["reference_id"],
                    microphone_position_id=params["microphone_position_id"],
                    sweep_profile=deepcopy(params["sweep_profile"]),
                    cancel_requested=probe,
                    expected_native_context=session.measurement_context(),
                    on_progress=lambda role, index, count: self._note(
                        job_id, "confirming", f"Verifying {role.replace('_', ' ')} ({index}/{count})…"))
                if input_chain_key(first.get("provenance")) != input_chain_key(
                        (verification or {}).get("provenance")):
                    raise ValueError("Speaker Align input chain changed before verification")
                return verification["confirmation"]

            if dry_run:
                outcome = await apply_and_confirm(
                    stage=session.stage_candidate, restore=session.restore_start,
                    confirm=confirm, proposal=proposal,
                    live_target=live_target, cancel_requested=probe)
                check = _summarize_check(outcome["check"])
                self._finish(
                    job_id, "trial-done",
                    "Trial alignment confirmed without committing."
                    if check["confirmed"] else "Trial alignment did not confirm; nothing changed.",
                    result={"confirmed": check["confirmed"], "check": check,
                            "proposal": _summarize_proposal(proposal),
                            "provenance": _jsonable(first.get("provenance") or {}),
                            "committed_revision": None, "dry_run": True})
                return
            outcome = await session.confirm_and_commit(
                confirm=confirm, proposal=proposal,
                live_target=live_target, cancel_requested=probe)
            check = _summarize_check(outcome["check"])
            if not check["confirmed"]:
                self._finish(
                    job_id, "unconfirmed",
                    "Alignment did not confirm acoustically; the start rendering was retained.",
                    result={"confirmed": False, "check": check,
                            "proposal": _summarize_proposal(proposal),
                            "provenance": _jsonable(first.get("provenance") or {}),
                            "committed_revision": None, "dry_run": False})
                return
            committed = outcome.get("committed") or {}
            self._finish(
                job_id, "committed",
                f"Committed speaker alignment at revision {committed.get('revision')}.",
                result={"confirmed": True, "check": check,
                        "proposal": _summarize_proposal(proposal),
                        "provenance": _jsonable(first.get("provenance") or {}),
                        "committed_revision": _jsonable(committed.get("revision")),
                        "dry_run": False})
            await self._notify_committed(
                output_key=context["output_key"], channels=context["channels"],
                committed_revision=committed.get("revision"), job_id=job_id)
        except asyncio.CancelledError:
            self._finish(job_id, "cancelled", "Speaker alignment cancelled.")
            raise
        except Exception as exc:
            self._finish(job_id, "failed", f"Speaker alignment failed: {exc}",
                         error=str(exc) or type(exc).__name__)

    async def _notify_committed(self, *, output_key: str, channels: int,
                                committed_revision: Any, job_id: str) -> None:
        """Register the committed-plan release adapter; never fail the job.

        Committed jobs only: the hook renders no audio itself. Registration
        failure skips quietly and the release keeps the legacy overview sync;
        the persisted head still keeps the commit for the next transition.
        """
        if self._on_committed is None:
            return
        try:
            result = self._on_committed({
                "output_key": output_key, "channels": channels,
                "committed_revision": committed_revision, "job_id": job_id})
            if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                await result
        except Exception as exc:
            logger.warning(
                "Speaker Align job=%s release adapter registration failed: %s",
                job_id, exc)

    async def wait_for(self, job_id: str, *, timeout_seconds: float = 60.0) -> dict[str, Any]:
        """Await a terminal status; primarily a test and UI-polling helper."""
        async with asyncio.timeout(timeout_seconds):
            while self.status(job_id)["status"] not in TERMINAL_STATUSES:
                await asyncio.sleep(0.01)
        return self.status(job_id)
