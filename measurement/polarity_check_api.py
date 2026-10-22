# SPDX-License-Identifier: AGPL-3.0-only
"""Polarity Check HTTP API: one sweep per speaker, a polarity verdict for each.

Each speaker is one ordinary active-chain sweep (see ``speaker_sweep``); the
analyzer reads the polarity of the signed microphone impulse response. The
check is diagnostic only: it changes no output setting. The newest job is kept
in memory, a verdict is not persisted because the next wiring change makes it
stale. A correct/reversed reading is withheld when the rendered DSP path of the
speaker rotates the measured band or cannot be modelled (``polarity_guard``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Form

from http_errors import bad_request
from measurement import polarity, polarity_guard, speaker_sweep
from measurement.speaker_scope import plan_area

router = APIRouter()
logger = logging.getLogger(__name__)
LABEL = "Polarity Check"


@dataclass(frozen=True)
class PolarityCheckDeps:
    """Accessors injected by the composition root (``main.py``)."""

    get_store: Callable[[], Any]
    get_session: Callable[[], Any]
    auto_sub_active: Callable[[], bool]
    get_output_state: Callable[[], dict]
    # Engine configuration of the rendered plan a frozen target describes; raises
    # ``polarity_guard.PathUnavailable`` when the live plan no longer matches it.
    resolve_dsp_path: Callable[[dict], dict]


_deps: PolarityCheckDeps | None = None
_slot = speaker_sweep.JobSlot(LABEL)


def configure_polarity_check(deps: PolarityCheckDeps | None) -> None:
    global _deps
    _deps = deps
    _slot.reset()


def _dependencies() -> PolarityCheckDeps:
    if _deps is None:
        raise RuntimeError("Polarity Check is not configured")
    return _deps


def _resolve_area(bank_id: str) -> tuple[dict, dict]:
    """Freeze the area's target and plan what the run measures."""
    target = speaker_sweep.freeze_area_target(_dependencies().get_store(), bank_id)
    implicit_stereo = target["mode"] != "surround" and set(target["roles"]) == {"main_l", "main_r"}
    if bank_id == "all" or (bank_id == "global" and not implicit_stereo):
        raise bad_request(ValueError("Select an individual speaker bank to check wiring polarity"))
    try:
        area = plan_area(target["mode"], bank_id, target["roles"])
    except ValueError as exc:
        raise bad_request(exc) from exc
    if not area["units"]:
        raise bad_request(ValueError("This area has no speaker to check"))
    return target, area


def _reading(analysis: dict) -> dict:
    reading = analysis.get("polarity")
    if not isinstance(reading, dict) or reading.get("verdict") not in polarity.VERDICTS:
        raise RuntimeError("The measurement carried no polarity reading")
    return reading


async def _judge_path(dependencies: PolarityCheckDeps, target: dict, unit: dict) -> dict:
    """Guard result of the DSP path this unit's take was measured through."""
    try:
        path = await asyncio.to_thread(dependencies.resolve_dsp_path, target)
        return await asyncio.to_thread(
            polarity_guard.assess_unit, path["config"], unit, sample_rate_hz=target["sample_rate_hz"])
    except polarity_guard.PathUnavailable as exc:
        return polarity_guard.unknown_result(exc.stage)
    except Exception as exc:
        logger.warning("Polarity Check could not resolve the DSP path of %s: %s", unit["label"], exc)
        return polarity_guard.unknown_result("DSP path")


async def _run(job: dict[str, Any], target: dict, area: dict) -> None:
    dependencies = _dependencies()
    mode = target["mode"]
    try:
        processing = dependencies.get_output_state()["modes"][mode]["processing"]
        rows = []
        for step, unit in enumerate(area["units"], start=1):
            if job["cancel_requested"]:
                raise asyncio.CancelledError(f"{LABEL} was cancelled")
            job["progress"] = {"step": step, "total": len(area["units"]), "label": unit["label"]}
            take = await speaker_sweep.measure_unit(
                dependencies.get_store(), dependencies.get_session(), job, target, unit, LABEL)
            reading = _reading(take["analysis"])
            verdict, reason, guard = reading["verdict"], reading.get("reason", ""), None
            if verdict in (polarity.CORRECT, polarity.REVERSED):
                guard = await _judge_path(dependencies, target, unit)
                if guard["state"] != polarity_guard.OK:
                    verdict, reason = polarity.INCONCLUSIVE, polarity_guard.REASONS[guard["state"]]
            rows.append({"label": unit["label"], "roles": list(unit["roles"]),
                         "verdict": verdict, "reason": reason,
                         "phase_cos": reading.get("phase_cos"), "snr_db": reading.get("snr_db"),
                         "guard": guard,
                         "inverted": any(processing[role]["polarity"] == "invert" for role in unit["roles"])})
        await speaker_sweep.wait_for_idle_session(dependencies.get_session())
        job["result"] = {"mode": mode, "rows": rows,
                         "all_reversed": len(rows) > 1 and all(row["verdict"] == polarity.REVERSED for row in rows)}
        job["status"] = "completed"
    except asyncio.CancelledError:
        job["status"] = "cancelled"
    except Exception as exc:
        logger.warning("Polarity Check failed: %s", exc)
        job["status"] = "failed"
        job["error"] = str(exc)
    finally:
        job["finished_at"] = speaker_sweep.now()


@router.get("/api/measurements/polarity-check")
async def get_polarity_check(bank_id: str):
    target, area = await asyncio.to_thread(_resolve_area, bank_id)
    return {"status": "ok", "mode": target["mode"], "bank_id": bank_id,
            "units": [unit["label"] for unit in area["units"]],
            "job": speaker_sweep.public_job(_slot.job)}


@router.post("/api/measurements/polarity-check/start")
async def start_polarity_check(
    input_id: str = Form(...),
    input_key: str = Form(""),
    mic_input_channel: str = Form("1"),
    calibration_ref: str = Form(""),
    bank_id: str = Form(...),
):
    _slot.ensure_startable(auto_sub_active=_dependencies().auto_sub_active())
    target, area = await asyncio.to_thread(_resolve_area, bank_id)
    _slot.ensure_startable(auto_sub_active=_dependencies().auto_sub_active())
    job = speaker_sweep.new_job(
        "polarity-check", mode=target["mode"], bank_id=bank_id, units=area["units"],
        params={"input_id": input_id, "input_key": input_key,
                "mic_input_channel": mic_input_channel, "calibration_ref": calibration_ref})
    _slot.start(job, _run(job, target, area))
    return {"status": "ok", "job": speaker_sweep.public_job(job)}


@router.get("/api/measurements/polarity-check/jobs/{job_id}")
async def get_polarity_check_job(job_id: str):
    return {"status": "ok", "job": speaker_sweep.public_job(_slot.get(job_id))}


@router.post("/api/measurements/polarity-check/jobs/{job_id}/cancel")
async def cancel_polarity_check_job(job_id: str):
    job = _slot.cancel(job_id, _dependencies().get_store())
    return {"status": "ok", "job": speaker_sweep.public_job(job)}


async def shutdown() -> None:
    await _slot.shutdown()
