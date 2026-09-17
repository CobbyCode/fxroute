# SPDX-License-Identifier: AGPL-3.0-only
"""One-at-a-time Speaker Align application jobs without hardware ownership.

This service composes the shipped measurement boundaries — frozen
``SpeakerAlignment`` planning, serial way acquisition, ``propose``, and the
trial/commit session — behind a small async job lifecycle. It never touches
hardware itself: acquisition, runtime staging and persistence arrive through
injected callables, so the whole flow is testable with fast doubles and the
composition root wires the real store and session later. No HTTP lives here.

Only one job may be active at a time (the measurement graph is
single-owner). Request validation raises synchronously before any job
record exists. Results are JSON-safe summaries: arrival/delay evidence and
overlap checks as plain floats, never IR waveforms or numpy values.
"""

from __future__ import annotations

import asyncio
import math
import threading
from copy import deepcopy
from typing import Any, Callable
from uuid import uuid4

from measurement.speaker_align import SpeakerAlignment
from measurement.speaker_apply import apply_and_confirm

TERMINAL_STATUSES = ("committed", "trial-done", "unconfirmed", "failed", "cancelled")


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
    checks = []
    for check in proposal.get("overlap_checks", []):
        checks.append({
            "roles": list(check.get("roles", [])),
            "lower_hz": float(check.get("lower_hz", 0.0)),
            "upper_hz": float(check.get("upper_hz", 0.0)),
            "phase_rms_degrees": float(check.get("phase_rms_degrees", 0.0)),
            "residual_delay_ms": float(check.get("residual_delay_ms", 0.0)),
            "before_sum_db": float(check.get("before_sum_db", 0.0)),
            "after_sum_db": float(check.get("after_sum_db", 0.0)),
        })
    return _jsonable({
        "start_revision": proposal.get("start_revision"),
        "processing_fingerprint": proposal.get("processing_fingerprint"),
        "arrival_ms": dict(proposal.get("arrival_ms", {})),
        "added_delay_ms": dict(proposal.get("added_delay_ms", {})),
        "overlap_checks": checks,
    })


def _summarize_check(check: dict[str, Any]) -> dict[str, Any]:
    pairs = []
    for pair in check.get("pairs", []):
        pairs.append({
            "roles": list(pair.get("roles", [])),
            "baseline_after_sum_db": float(pair.get("baseline_after_sum_db", 0.0)),
            "confirmation_after_sum_db": float(pair.get("confirmation_after_sum_db", 0.0)),
            "regression_db": float(pair.get("regression_db", 0.0)),
        })
    return _jsonable({
        "confirmed": bool(check.get("confirmed")),
        "reasons": [str(reason) for reason in check.get("reasons", [])],
        "max_residual_ms": float(check.get("max_residual_ms", 0.0)),
        "max_regression_db": float(check.get("max_regression_db", 0.0)),
        "min_confirmation_sum_db": float(check.get("min_confirmation_sum_db", 0.0)),
        "pairs": pairs,
    })


def _session_identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"Speaker Align service requires a {label} identity")
    return value


class SpeakerAlignService:
    """Own Speaker Align job records, worker tasks and single-active ownership."""

    def __init__(self, *, get_state: Callable[[], dict],
                 describe: Callable[[dict], dict],
                 acquire: Callable[..., Any],
                 create_session: Callable[..., Any],
                 freeze_live: Callable[..., dict]):
        for name, bound in (("get_state", get_state), ("describe", describe),
                            ("acquire", acquire), ("create_session", create_session),
                            ("freeze_live", freeze_live)):
            if not callable(bound):
                raise ValueError(f"Speaker Align service requires a {name} boundary")
        self._get_state = get_state
        self._describe = describe
        self._acquire = acquire
        self._create_session = create_session
        self._freeze_live = freeze_live
        self._guard = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._active_id: str | None = None

    def jobs(self) -> list[dict[str, Any]]:
        """Return detached public copies of every known job, newest last."""
        with self._guard:
            return [deepcopy(job) for job in self._jobs.values()]

    def status(self, job_id: str) -> dict[str, Any]:
        """Return a detached public copy of one job; unknown ids raise KeyError."""
        with self._guard:
            return deepcopy(self._jobs[job_id])

    def cancel(self, job_id: str) -> dict[str, Any]:
        """Request cancellation; terminal jobs are unaffected. Unknown ids raise KeyError."""
        with self._guard:
            job = self._jobs[job_id]
            if job["status"] in TERMINAL_STATUSES:
                return deepcopy(job)
            job["cancel_requested"] = True
            if job["status"] != "cancelling":
                job["status"] = "cancelling"
                job["message"] = "Cancelling speaker alignment…"
            task = self._tasks.get(job_id)
        if task is not None:
            task.cancel()
        with self._guard:
            return deepcopy(self._jobs[job_id])

    def start(self, side: str, *, input_id: str, mic_input_channel: str | int | None = "1",
              reference_input_channel: str | int | None,
              reference_id: str, microphone_position_id: str,
              sweep_profile: dict[str, float] | None = None,
              dry_run: bool = False) -> str:
        """Validate a request synchronously and launch its worker; return the job id."""
        if side not in ("left", "right"):
            raise ValueError("Speaker Align side must be left or right")
        if reference_input_channel is None or not str(reference_input_channel).strip():
            raise ValueError("Speaker Align service requires an electrical reference input channel")
        reference_id = _session_identity(reference_id, "upstream reference")
        microphone_position_id = _session_identity(microphone_position_id, "microphone position")
        state = deepcopy(self._get_state())
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
                "reference_id": reference_id, "microphone_position_id": microphone_position_id,
                "sweep_profile": deepcopy(sweep_profile) if sweep_profile else None,
                "output_key": context["output_key"], "channels": context["channels"],
                "sample_rate_hz": context["sample_rate_hz"],
            },
            "result": None, "error": None,
        }
        with self._guard:
            if self._active_id is not None:
                active = self._jobs.get(self._active_id)
                if active is not None and active["status"] not in TERMINAL_STATUSES:
                    raise RuntimeError("A speaker alignment is already running")
            self._jobs[job_id] = job
            self._active_id = job_id
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
            raise ValueError(
                "Speaker Align live target is stale; revision and processing must "
                "match the frozen requests before acquiring")

    def _note(self, job_id: str, status: str, message: str) -> None:
        with self._guard:
            job = self._jobs[job_id]
            if job["status"] in TERMINAL_STATUSES:
                return
            job["status"] = status
            job["message"] = message

    def _finish(self, job_id: str, status: str, message: str, *,
                result: dict | None = None, error: str | None = None) -> None:
        with self._guard:
            job = self._jobs[job_id]
            job["status"] = status
            job["message"] = message
            job["result"] = result
            job["error"] = error
            if self._active_id == job_id:
                self._active_id = None

    async def _run(self, job_id: str) -> None:
        with self._guard:
            job = self._jobs[job_id]
            params = deepcopy(job["params"])
            dry_run = job["dry_run"]
        probe = lambda: bool(self._jobs[job_id]["cancel_requested"])
        try:
            state = deepcopy(self._get_state())
            context = self._describe(state)
            alignment = SpeakerAlignment(
                state, side=job["side"], output_key=context["output_key"],
                channels=context["channels"], sample_rate_hz=context["sample_rate_hz"],
                fingerprint=context["fingerprint"],
                reference_id=params["reference_id"],
                microphone_position_id=params["microphone_position_id"])
            live_target = self._freeze_live(
                state, output_key=context["output_key"], channels=context["channels"],
                sample_rate_hz=context["sample_rate_hz"], fingerprint=context["fingerprint"])
            self._require_fresh_live(alignment, live_target)

            self._note(job_id, "acquiring", "Acquiring speaker ways…")
            first = await self._acquire(
                alignment, input_id=params["input_id"],
                mic_input_channel=params["mic_input_channel"],
                reference_input_channel=params["reference_input_channel"],
                reference_id=params["reference_id"],
                microphone_position_id=params["microphone_position_id"],
                sweep_profile=deepcopy(params["sweep_profile"]),
                cancel_requested=probe)
            proposal = alignment.propose(first["captures"], live_target=live_target)
            session = self._create_session(
                state, output_key=context["output_key"], channels=context["channels"],
                sample_rate_hz=context["sample_rate_hz"])

            self._note(job_id, "confirming", "Confirming alignment acoustically…")

            async def reacquire() -> list[dict]:
                second = await self._acquire(
                    alignment, input_id=params["input_id"],
                    mic_input_channel=params["mic_input_channel"],
                    reference_input_channel=params["reference_input_channel"],
                    reference_id=params["reference_id"],
                    microphone_position_id=params["microphone_position_id"],
                    sweep_profile=deepcopy(params["sweep_profile"]),
                    cancel_requested=probe)
                return second["captures"]

            if dry_run:
                outcome = await apply_and_confirm(
                    stage=session.stage_candidate, restore=session.restore_start,
                    acquire=reacquire, alignment=alignment, proposal=proposal,
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
                acquire=reacquire, alignment=alignment, proposal=proposal,
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
                        "committed_revision": committed.get("revision"), "dry_run": False})
        except asyncio.CancelledError:
            self._finish(job_id, "cancelled", "Speaker alignment cancelled.")
            raise
        except Exception as exc:
            self._finish(job_id, "failed", f"Speaker alignment failed: {exc}",
                         error=str(exc) or type(exc).__name__)

    async def wait_for(self, job_id: str, *, timeout_seconds: float = 60.0) -> dict[str, Any]:
        """Await a terminal status; primarily a test and UI-polling helper."""
        async with asyncio.timeout(timeout_seconds):
            while self.status(job_id)["status"] not in TERMINAL_STATUSES:
                await asyncio.sleep(0)
        return self.status(job_id)
