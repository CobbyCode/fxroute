# SPDX-License-Identifier: AGPL-3.0-only
"""Level Matching HTTP API: measure speakers, store raw levels, propose slot values.

Each speaker is one ordinary active-chain sweep over a frozen target narrowed
to that speaker's roles (see ``speaker_sweep``). The job returns the Level
Calibration slots derived from the stored raw levels; the client commits them
through the output-state ``set_level_calibration`` mutation, so the revision
check and the plan sync stay with that one route.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Form, HTTPException

from audio.output_topology import SUB_ROLES, surround_group
from http_errors import bad_request
from measurement import level_match, speaker_sweep

router = APIRouter()
logger = logging.getLogger(__name__)
LABEL = "Level Matching"


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
_slot = speaker_sweep.JobSlot(LABEL)


def configure_level_match(deps: LevelMatchDeps | None) -> None:
    global _deps
    _deps = deps
    _slot.reset()


def _dependencies() -> LevelMatchDeps:
    if _deps is None:
        raise RuntimeError("Level Matching is not configured")
    return _deps


def _resolve_scope(bank_id: str) -> tuple[dict, dict]:
    """Freeze the area's target and plan what the run measures."""
    target = speaker_sweep.freeze_area_target(_dependencies().get_store(), bank_id)
    try:
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
                raise asyncio.CancelledError(f"{LABEL} was cancelled")
            job["progress"] = {"step": step, "total": len(scope["units"]), "label": unit["label"]}
            take = await speaker_sweep.measure_unit(
                dependencies.get_store(), dependencies.get_session(), job, target, unit, LABEL)
            capture_volume, system_volume = await asyncio.to_thread(_observed_volumes, take["mic_node"])
            take_fingerprint = level_match.make_fingerprint(
                device_key=target["device_key"], sample_rate_hz=target["sample_rate_hz"],
                mic_node=take["mic_node"], mic_channel=take["mic_channel"],
                capture_volume_percent=capture_volume, system_volume_percent=system_volume)
            if fingerprint is None:
                fingerprint = take_fingerprint
            elif not level_match.fingerprints_match(fingerprint, take_fingerprint):
                raise RuntimeError("Microphone or output gain changed between the takes; run Level Matching again")
            raw_db = level_match.raw_level_db(
                take["analysis"].get("normalized_by_db"),
                _own_slots(state, mode, unit["roles"], scope["slot"]))
            for role in unit["roles"]:
                fresh[level_match.entry_key(role, scope["slot"])] = level_match.make_entry(
                    role=role, raw_db=raw_db, slot=scope["slot"], family=scope["family"],
                    fingerprint=fingerprint, measured_at=speaker_sweep.now())
        if scope["requires_reference"]:
            front = _front_roles(target)
            reference = stored.get(level_match.entry_key(front[0], level_match.SURROUND_GROUP_SLOT["front"])) if front else None
            if reference is None or not level_match.fingerprints_match(reference["fingerprint"], fingerprint):
                raise RuntimeError(
                    "Microphone or output setup changed since Front L/R was measured; measure Front L/R again")
        merged = {**stored, **fresh}
        derived = level_match.derive_slot_values(merged)
        table.save_entries(mode, merged)
        await speaker_sweep.wait_for_idle_session(dependencies.get_session())
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
        job["finished_at"] = speaker_sweep.now()


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
            "job": speaker_sweep.public_job(_slot.job)}


@router.post("/api/measurements/level-match/start")
async def start_level_match(
    input_id: str = Form(...),
    input_key: str = Form(""),
    mic_input_channel: str = Form("1"),
    calibration_ref: str = Form(""),
    bank_id: str = Form(...),
):
    dependencies = _dependencies()
    _slot.ensure_startable(auto_sub_active=dependencies.auto_sub_active())
    target, scope = _resolve_scope(bank_id)
    if not _reference_ready(target, scope, dependencies.get_table().entries(target["mode"])):
        raise HTTPException(status_code=409, detail="Measure Front L/R first to set the level reference")
    job = speaker_sweep.new_job(
        "level-match", mode=target["mode"], bank_id=bank_id, units=scope["units"], slot=scope["slot"],
        params={"input_id": input_id, "input_key": input_key,
                "mic_input_channel": mic_input_channel, "calibration_ref": calibration_ref})
    _slot.start(job, _run(job, target, scope))
    return {"status": "ok", "job": speaker_sweep.public_job(job)}


@router.get("/api/measurements/level-match/jobs/{job_id}")
async def get_level_match_job(job_id: str):
    return {"status": "ok", "job": speaker_sweep.public_job(_slot.get(job_id))}


@router.post("/api/measurements/level-match/jobs/{job_id}/cancel")
async def cancel_level_match_job(job_id: str):
    job = _slot.cancel(job_id, _dependencies().get_store())
    return {"status": "ok", "job": speaker_sweep.public_job(job)}


async def shutdown() -> None:
    await _slot.shutdown()
