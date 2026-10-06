# SPDX-License-Identifier: AGPL-3.0-only
"""Shared run machinery of the calibration sweeps that measure one speaker at a time.

Level Matching and Polarity Check both play an ordinary active-chain sweep per
speaker over a frozen target narrowed to that speaker's roles (the rest muted
at the output stage), so the microphone path and the sweep are exactly those of
a manual measurement. This module runs one take and holds the single job a
calibration API runs at a time; ``speaker_scope`` plans what an area measures.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from http_errors import bad_request
from measurement import session as measurement_session
from measurement.constants import TERMINAL_JOB_STATUSES
from measurement.target import narrow_measured_target

POLL_SECONDS = 0.25
# The output state stays locked while the measurement session owns a job; the
# session releases a finished take a moment after the job completes.
SESSION_IDLE_POLLS = 40


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def freeze_area_target(store: Any, bank_id: str) -> dict:
    """Freeze the output target of one area for a run."""
    provider = getattr(store, "measurement_target_provider", None)
    if store is None or not callable(provider):
        raise HTTPException(status_code=503, detail="Measurement store not available")
    rate = measurement_session._resolve_measurement_start_sample_rate()
    try:
        return provider(bank_id, rate)
    except ValueError as exc:
        raise bad_request(exc) from exc


async def _wait_for_take(store: Any, measurement_job_id: str, job: dict[str, Any], label: str) -> dict[str, Any]:
    cancelled = False
    while True:
        if job["cancel_requested"] and not cancelled:
            cancelled = True
            store.cancel_job(measurement_job_id)
        finished = store.get_job(measurement_job_id)
        if finished.get("status") in TERMINAL_JOB_STATUSES:
            break
        await asyncio.sleep(POLL_SECONDS)
    if cancelled or finished.get("status") == "cancelled":
        raise asyncio.CancelledError(f"{label} was cancelled")
    if finished.get("status") != "completed":
        error = finished.get("error") if isinstance(finished.get("error"), dict) else {}
        detail = error.get("detail") or finished.get("message") or finished.get("status")
        raise RuntimeError(f"{label} measurement did not complete: {detail}")
    return finished


async def wait_for_idle_session(session: Any) -> None:
    for _ in range(SESSION_IDLE_POLLS):
        if session is None or not session.has_active_jobs:
            return
        await asyncio.sleep(POLL_SECONDS / 2)


def has_direct_arrival(analysis: dict[str, Any]) -> bool:
    """Whether the analyzer found the speaker's direct sound in the take.

    A capture the sweep never reached (amplifier off, wrong or unplugged
    microphone, a loud room) has none: its level and impulse response are noise.
    """
    impulse_response = analysis.get("impulse_response")
    return isinstance(impulse_response, dict) and impulse_response.get("timing_status") == "valid"


async def measure_unit(store: Any, session: Any, job: dict[str, Any], target: dict, unit: dict,
                       label: str, *, require_arrival: bool = False) -> dict[str, Any]:
    """Run one speaker's sweep; return its ``analysis`` and the observed capture chain.

    ``label`` names the calling flow in error messages. The job carries
    ``_params`` (the microphone selection) and records the running take in
    ``_measurement_job_id`` so a cancel can reach it. With ``require_arrival`` a
    take without a direct arrival is refused instead of returned.
    """
    params = job["_params"]
    rate = target["sample_rate_hz"]
    narrowed = narrow_measured_target(target, roles=unit["roles"])
    entry_epoch = session.capture_entry_epoch() if session is not None else None
    started = await measurement_session._start_registered_manual_measurement(
        rate,
        lambda: store.start_measurement(
            input_id=params["input_id"], input_key=params["input_key"], channel=unit["channel"],
            mic_input_channel=params["mic_input_channel"], calibration_ref=params["calibration_ref"],
            frozen_target=narrowed,
            **measurement_session._stage_bank_v2_context(
                measurement_bank=target["bank_id"], measurement_rate_hz=rate, channel=unit["channel"])),
        entry_epoch=entry_epoch)
    job["_measurement_job_id"] = str(started["id"])
    finished = await _wait_for_take(store, str(started["id"]), job, label)
    result = finished.get("result") if isinstance(finished.get("result"), dict) else {}
    analysis = result.get("analysis") if isinstance(result.get("analysis"), dict) else {}
    capture = result.get("capture") if isinstance(result.get("capture"), dict) else {}
    if analysis.get("sample_rate") != rate:
        raise RuntimeError(f"{label} capture sample rate changed")
    if require_arrival and not has_direct_arrival(analysis):
        raise RuntimeError(
            f"The sweep of {unit['label']} did not reach the microphone, so the take cannot be used. "
            f"Check the amplifier, the speaker and the microphone. Nothing was changed.")
    mic_node = str(capture.get("microphone_node") or "")
    if not mic_node:
        raise RuntimeError(f"{label} capture did not report its microphone")
    return {"analysis": analysis, "mic_node": mic_node, "mic_channel": capture.get("mic_input_channel")}


def new_job(kind: str, *, mode: str, bank_id: str, units: Sequence[dict], params: dict, **fields: Any) -> dict:
    return {"id": f"{kind}-{uuid4().hex[:12]}", "status": "running", "mode": mode, "bank_id": bank_id,
            "started_at": now(), "finished_at": None,
            "progress": {"step": 0, "total": len(units), "label": ""},
            "result": None, "error": None, "cancel_requested": False,
            "_measurement_job_id": "", "_params": dict(params), **fields}


def public_job(job: dict[str, Any] | None) -> dict[str, Any] | None:
    if job is None:
        return None
    return {key: copy.deepcopy(value) for key, value in job.items() if not key.startswith("_")}


class JobSlot:
    """The one job a calibration API runs at a time, with its worker task."""

    def __init__(self, label: str):
        self.label = label
        self.job: dict[str, Any] | None = None
        self.task: asyncio.Task | None = None

    def reset(self) -> None:
        self.job, self.task = None, None

    def ensure_startable(self, *, auto_sub_active: bool) -> None:
        if auto_sub_active:
            raise HTTPException(status_code=423, detail="Auto Sub Optimize is in progress")
        if self.job is not None and self.job["status"] == "running":
            raise HTTPException(status_code=409, detail=f"{self.label} is already running")

    def start(self, job: dict[str, Any], run: Any) -> None:
        self.job = job
        self.task = asyncio.create_task(run)

    def get(self, job_id: str) -> dict[str, Any]:
        if self.job is None or self.job["id"] != job_id:
            raise HTTPException(status_code=404, detail=f"{self.label} job not found")
        return self.job

    def cancel(self, job_id: str, store: Any) -> dict[str, Any]:
        job = self.get(job_id)
        if job["status"] == "running":
            job["cancel_requested"] = True
            measurement_job_id = job["_measurement_job_id"]
            if measurement_job_id and store is not None:
                try:
                    store.cancel_job(measurement_job_id)
                except KeyError:
                    pass
        return job

    async def shutdown(self) -> None:
        """Cancel a running job and wait for its worker to unwind."""
        if self.job is not None and self.job["status"] == "running":
            self.job["cancel_requested"] = True
        if self.task is not None and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
