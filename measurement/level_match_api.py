# SPDX-License-Identifier: AGPL-3.0-only
"""Level Matching HTTP API: measure speakers, store raw levels, propose slot values.

Each speaker is one ordinary active-chain sweep over a frozen target narrowed
to that speaker's roles (the rest muted at the output stage), so the microphone
path and the sweep are exactly those of a manual measurement. The job returns
the Level Calibration slots derived from the stored raw levels; the client
commits them through the output-state ``set_level_calibration`` mutation, so
the revision check and the plan sync stay with that one route.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Form, HTTPException

from audio.output_topology import SUB_ROLES, surround_group
from http_errors import bad_request
from measurement import level_match
from measurement import session as measurement_session
from measurement.constants import TERMINAL_JOB_STATUSES
from measurement.target import narrow_measured_target

router = APIRouter()
logger = logging.getLogger(__name__)
POLL_SECONDS = 0.25
# The output state stays locked while the measurement session owns a job; the
# session releases a finished take a moment after the job completes.
SESSION_IDLE_POLLS = 40


@dataclass(frozen=True)
class LevelMatchDeps:
    """Accessors injected by the composition root (``main.py``)."""

    get_table: Callable[[], level_match.LevelMatchStore]
    get_store: Callable[[], Any]
    get_session: Callable[[], Any]
    auto_sub_active: Callable[[], bool]
    get_output_state: Callable[[], dict]
    get_output_volume: Callable[[], int]
    get_node_volume: Callable[[str], int]


_deps: LevelMatchDeps | None = None
_job: dict[str, Any] | None = None
_task: asyncio.Task | None = None


def configure_level_match(deps: LevelMatchDeps | None) -> None:
    global _deps, _job, _task
    _deps, _job, _task = deps, None, None


def _dependencies() -> LevelMatchDeps:
    if _deps is None:
        raise RuntimeError("Level Matching is not configured")
    return _deps


def _public_job(job: dict[str, Any] | None) -> dict[str, Any] | None:
    if job is None:
        return None
    return {key: copy.deepcopy(value) for key, value in job.items() if not key.startswith("_")}


def _resolve_scope(bank_id: str) -> tuple[dict, dict]:
    """Freeze the area's target and plan what the run measures."""
    store = _dependencies().get_store()
    provider = getattr(store, "measurement_target_provider", None)
    if store is None or not callable(provider):
        raise HTTPException(status_code=503, detail="Measurement store not available")
    rate = measurement_session._resolve_measurement_start_sample_rate()
    try:
        target = provider(bank_id, rate)
        scope = level_match.plan_scope(target["mode"], bank_id, target["roles"])
    except ValueError as exc:
        raise bad_request(exc) from exc
    return target, scope


def _front_roles(target: dict) -> list[str]:
    if target["mode"] != "surround":
        return []
    return [role for role in target["roles"]
            if role not in SUB_ROLES and surround_group(role) == "front"]


def _reference_ready(target: dict, scope: dict, entries: dict[str, dict]) -> bool:
    """Surround speakers need a comparable Front L/R reference from this setup."""
    if not scope["requires_reference"]:
        return True
    front = _front_roles(target)
    if not level_match.has_front_reference(entries, front):
        return False
    fingerprint = entries[level_match.entry_key(front[0], level_match.SURROUND_GROUP_SLOT["front"])]["fingerprint"]
    return (fingerprint.get("device_key") == target["device_key"]
            and fingerprint.get("sample_rate_hz") == target["sample_rate_hz"])


def _own_slots(state: dict, mode: str, roles: list[str], slot: str) -> list[float]:
    processing = state["modes"][mode]["processing"]
    return [processing[role]["level_cal_db"][slot] for role in roles]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _wait_for_take(store: Any, measurement_job_id: str, job: dict[str, Any]) -> dict[str, Any]:
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
        raise asyncio.CancelledError("Level Matching was cancelled")
    if finished.get("status") != "completed":
        error = finished.get("error") if isinstance(finished.get("error"), dict) else {}
        detail = error.get("detail") or finished.get("message") or finished.get("status")
        raise RuntimeError(f"Level Matching measurement did not complete: {detail}")
    return finished


async def _wait_for_idle_session() -> None:
    session = _dependencies().get_session()
    for _ in range(SESSION_IDLE_POLLS):
        if session is None or not session.has_active_jobs:
            return
        await asyncio.sleep(POLL_SECONDS / 2)


async def _measure_unit(job: dict[str, Any], target: dict, unit: dict) -> tuple[float, dict]:
    """Run one speaker's sweep; return its level reference and the observed capture chain."""
    dependencies = _dependencies()
    store = dependencies.get_store()
    params = job["_params"]
    rate = target["sample_rate_hz"]
    narrowed = narrow_measured_target(target, roles=unit["roles"])
    session = dependencies.get_session()
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
    finished = await _wait_for_take(store, str(started["id"]), job)
    result = finished.get("result") if isinstance(finished.get("result"), dict) else {}
    analysis = result.get("analysis") if isinstance(result.get("analysis"), dict) else {}
    capture = result.get("capture") if isinstance(result.get("capture"), dict) else {}
    if analysis.get("sample_rate") != rate:
        raise RuntimeError("Level Matching capture sample rate changed")
    mic_node = str(capture.get("microphone_node") or "")
    if not mic_node:
        raise RuntimeError("Level Matching capture did not report its microphone")
    return analysis.get("normalized_by_db"), {"mic_node": mic_node,
                                              "mic_channel": capture.get("mic_input_channel")}


def _observed_volumes(mic_node: str) -> tuple[float | None, float | None]:
    dependencies = _dependencies()
    try:
        capture_volume = float(dependencies.get_node_volume(mic_node))
    except Exception:
        capture_volume = None
    try:
        system_volume = float(dependencies.get_output_volume())
    except Exception:
        system_volume = None
    return capture_volume, system_volume


async def _run(job: dict[str, Any], target: dict, scope: dict) -> None:
    dependencies = _dependencies()
    mode = target["mode"]
    table = dependencies.get_table()
    try:
        state = dependencies.get_output_state()
        stored = table.entries(mode)
        fresh: dict[str, dict] = {}
        fingerprint: dict | None = None
        for step, unit in enumerate(scope["units"], start=1):
            if job["cancel_requested"]:
                raise asyncio.CancelledError("Level Matching was cancelled")
            job["progress"] = {"step": step, "total": len(scope["units"]), "label": unit["label"]}
            normalized_by_db, chain = await _measure_unit(job, target, unit)
            capture_volume, system_volume = await asyncio.to_thread(_observed_volumes, chain["mic_node"])
            take_fingerprint = level_match.make_fingerprint(
                device_key=target["device_key"], sample_rate_hz=target["sample_rate_hz"],
                mic_node=chain["mic_node"], mic_channel=chain["mic_channel"],
                capture_volume_percent=capture_volume, system_volume_percent=system_volume)
            if fingerprint is None:
                fingerprint = take_fingerprint
            elif not level_match.fingerprints_match(fingerprint, take_fingerprint):
                raise RuntimeError("Microphone or output gain changed between the takes; run Level Matching again")
            raw_db = level_match.raw_level_db(
                normalized_by_db, _own_slots(state, mode, unit["roles"], scope["slot"]))
            for role in unit["roles"]:
                fresh[level_match.entry_key(role, scope["slot"])] = level_match.make_entry(
                    role=role, raw_db=raw_db, slot=scope["slot"], family=scope["family"],
                    fingerprint=fingerprint, measured_at=_now())
        if scope["requires_reference"]:
            front = _front_roles(target)
            reference = stored.get(level_match.entry_key(front[0], level_match.SURROUND_GROUP_SLOT["front"])) if front else None
            if reference is None or not level_match.fingerprints_match(reference["fingerprint"], fingerprint):
                raise RuntimeError(
                    "Microphone or output setup changed since Front L/R was measured; measure Front L/R again")
        merged = {**stored, **fresh}
        derived = level_match.derive_slot_values(merged)
        table.save_entries(mode, merged)
        await _wait_for_idle_session()
        job["result"] = {"mode": mode, "values": derived,
                         "measured_roles": sorted({entry["role"] for entry in fresh.values()}),
                         "state_revision": state["revision"]}
        job["status"] = "completed"
    except asyncio.CancelledError:
        job["status"] = "cancelled"
    except Exception as exc:
        logger.warning("Level Matching failed: %s", exc)
        job["status"] = "failed"
        job["error"] = str(exc)
    finally:
        job["finished_at"] = _now()


@router.get("/api/measurements/level-match")
async def get_level_match(bank_id: str):
    target, scope = _resolve_scope(bank_id)
    entries = _dependencies().get_table().entries(target["mode"])
    valid = level_match.valid_entries(entries)
    try:
        trims = level_match.derive_slot_values(entries)
    except ValueError:
        trims = {}
    rows = [{"role": entry["role"], "slot": entry["slot"], "raw_db": entry["raw_db"], "valid": key in valid,
             "trim_db": trims.get(entry["role"], {}).get(entry["slot"]), "measured_at": entry["measured_at"]}
            for key, entry in sorted(entries.items()) if entry["family"] == scope["family"]]
    return {"status": "ok", "mode": target["mode"], "bank_id": bank_id, "slot": scope["slot"],
            "family": scope["family"],
            "requires_reference": scope["requires_reference"],
            "reference_ready": _reference_ready(target, scope, entries),
            "units": [unit["label"] for unit in scope["units"]], "entries": rows,
            "job": _public_job(_job)}


@router.post("/api/measurements/level-match/start")
async def start_level_match(
    input_id: str = Form(...),
    input_key: str = Form(""),
    mic_input_channel: str = Form("1"),
    calibration_ref: str = Form(""),
    bank_id: str = Form(...),
):
    global _job, _task
    dependencies = _dependencies()
    if dependencies.auto_sub_active():
        raise HTTPException(status_code=423, detail="Auto Sub Optimize is in progress")
    if _job is not None and _job["status"] == "running":
        raise HTTPException(status_code=409, detail="Level Matching is already running")
    target, scope = _resolve_scope(bank_id)
    if not _reference_ready(target, scope, dependencies.get_table().entries(target["mode"])):
        raise HTTPException(status_code=409, detail="Measure Front L/R first to set the level reference")
    _job = {"id": f"level-match-{uuid4().hex[:12]}", "status": "running", "mode": target["mode"],
            "bank_id": bank_id, "slot": scope["slot"], "started_at": _now(), "finished_at": None,
            "progress": {"step": 0, "total": len(scope["units"]), "label": ""},
            "result": None, "error": None, "cancel_requested": False,
            "_measurement_job_id": "",
            "_params": {"input_id": input_id, "input_key": input_key,
                        "mic_input_channel": mic_input_channel, "calibration_ref": calibration_ref}}
    _task = asyncio.create_task(_run(_job, target, scope))
    return {"status": "ok", "job": _public_job(_job)}


@router.get("/api/measurements/level-match/jobs/{job_id}")
async def get_level_match_job(job_id: str):
    if _job is None or _job["id"] != job_id:
        raise HTTPException(status_code=404, detail="Level Matching job not found")
    return {"status": "ok", "job": _public_job(_job)}


@router.post("/api/measurements/level-match/jobs/{job_id}/cancel")
async def cancel_level_match_job(job_id: str):
    if _job is None or _job["id"] != job_id:
        raise HTTPException(status_code=404, detail="Level Matching job not found")
    if _job["status"] == "running":
        _job["cancel_requested"] = True
        measurement_job_id = _job["_measurement_job_id"]
        store = _dependencies().get_store()
        if measurement_job_id and store is not None:
            try:
                store.cancel_job(measurement_job_id)
            except KeyError:
                pass
    return {"status": "ok", "job": _public_job(_job)}


async def shutdown() -> None:
    """Cancel a running job and wait for its worker to unwind."""
    if _job is not None and _job["status"] == "running":
        _job["cancel_requested"] = True
    if _task is not None and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
