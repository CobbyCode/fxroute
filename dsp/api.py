# SPDX-License-Identifier: AGPL-3.0-only
"""Native DSP HTTP API controller: the /api/dsp/* routes.

API/controller layer only.  It owns no DSP configuration, runtime state,
persistence or transition logic; every request delegates to the injected
application dependencies (DSPManager, DSPRuntime, locks, volume and
peak-monitor services) provided by main.py through register_dsp_routes().
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic.fields import FieldInfo
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

import zip_album
from http_errors import bad_request, internal_error
from dsp.effects_extras import (
    is_pure_loudness_strength_change,
    is_runtime_autogain_loudness_change,
    merge_effects_extras_from_json,
    parse_effects_extras_from_json,
)
from dsp.persistence import clean_name
from audio.output_service import MeasurementActiveError
from audio.filter_banks import resolve_bank
from audio.output_state import referenced_presets, set_bank_preset
from audio.output_state_store import StateConflictError
from library.core import path_within_root
from library.api import _cleanup_temp_file
from uploads import (
    DSP_BUNDLE_MAX_BYTES,
    DSP_IR_MAX_BYTES,
    DSP_PRESET_TEXT_MAX_BYTES,
    UploadTooLargeError,
    read_upload,
    save_upload_to_file,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Preset bundle ZIP hardening limits (manifest + preset JSON + IR files).
PRESET_BUNDLE_MAX_MEMBERS = 512
PRESET_BUNDLE_MAX_TOTAL_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
PRESET_BUNDLE_MAX_MEMBER_BYTES = 256 * 1024 * 1024
PRESET_BUNDLE_MAX_JSON_BYTES = 16 * 1024 * 1024
PRESET_BUNDLE_MAX_CENTRAL_DIRECTORY_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class DspApiDeps:
    """Application services injected from main.py."""

    require_dsp_manager: Callable[[], Any]
    get_dsp_manager: Callable[[], Any]
    get_dsp_runtime: Callable[[], Any]
    get_dsp_preset_load_lock: Callable[[], asyncio.Lock]
    dsp_mutation_lock: Callable[[], asyncio.Lock]
    canonical_volume_write_lock: Callable[[], asyncio.Lock]
    drain_worker: Callable[..., Awaitable[Any]]
    run_locked_worker: Callable[..., Awaitable[Any]]
    broadcast: Callable[[dict], Awaitable[Any]]
    load_dsp_preset: Callable[..., Awaitable[Any]]
    restore_volume_state: Callable[..., Awaitable[Any]]
    volume_state_for_manager: Callable[..., Awaitable[Any]]
    schedule_peak_monitor_refresh: Callable[[str], None]
    get_output_service: Optional[Callable[[], Any]] = None
    # Best-effort live sync after a bank preset assignment (import/create
    # flows persist through OutputService without touching the runtime).
    # Returns {"live_applied": bool, "live_reason": str | None}; never raises
    # (the assignment stays committed when the sync fails). Absent in tests.
    sync_v2_head_live: Optional[Callable[[], Awaitable[dict]]] = None
    # Gates a generated-correction commit on the source measurement's frozen
    # target still matching live processing; raises ValueError on mismatch.
    verify_measurement_commit: Optional[Callable[[str, dict], None]] = None


@dataclass
class _DspApiRuntime:
    deps: Optional[DspApiDeps] = None


_runtime = _DspApiRuntime()


def configure_dsp_api(deps: DspApiDeps) -> None:
    """Bind the application services used by the route handlers."""
    _runtime.deps = deps


def register_dsp_routes(app, deps: DspApiDeps) -> None:
    """Register the /api/dsp/* routes on the FastAPI application."""
    configure_dsp_api(deps)
    app.include_router(router)


def _deps() -> DspApiDeps:
    if _runtime.deps is None:
        raise RuntimeError("DSP API runtime is not configured")
    return _runtime.deps


def _parse_effects_extras_from_json(body: dict) -> dict:
    """Thin wrapper: effects extras parsing lives in effects_extras (REFACTOR-010)."""
    return parse_effects_extras_from_json(body)


def _merge_effects_extras_from_json(previous: dict, body: dict) -> dict:
    """Thin wrapper: effects extras merge lives in effects_extras (REFACTOR-010)."""
    return merge_effects_extras_from_json(previous, body)


def _resolve_effects_extras(extras: dict | None = None) -> dict:
    manager = _deps().get_dsp_manager()
    if not manager:
        return extras or {}
    if extras is None:
        return manager.load_global_extras()
    return manager.normalize_effects_extras(extras)


def _is_pure_loudness_strength_change(previous: dict, current: dict) -> bool:
    """Thin wrapper: strength-change detection lives in effects_extras (REFACTOR-010)."""
    return is_pure_loudness_strength_change(previous, current)


def _is_runtime_autogain_loudness_change(previous: dict, current: dict) -> bool:
    """Thin wrapper: autogain/loudness change detection lives in effects_extras (REFACTOR-010)."""
    return is_runtime_autogain_loudness_change(previous, current)


def _effects_extras_from_form(
    *,
    limiter_enabled: bool,
    headroom_enabled: bool,
    headroom_gain_db: float,
    autogain_enabled: bool,
    autogain_target_db: float,
    delay_enabled: bool,
    delay_left_ms: float,
    delay_right_ms: float,
    tone_effect_enabled: bool,
    tone_effect_mode: str,
    bass_enabled: bool | None = None,
    bass_amount: float | None = None,
) -> dict:
    extras = {
        "limiter": {"enabled": limiter_enabled},
        "headroom": {"enabled": headroom_enabled, "params": {"gainDb": headroom_gain_db}},
        "autogain": {"enabled": autogain_enabled, "params": {"targetDb": autogain_target_db}},
        "delay": {
            "enabled": delay_enabled,
            "params": {"leftMs": delay_left_ms, "rightMs": delay_right_ms},
        },
        "tone_effect": {"enabled": tone_effect_enabled, "mode": tone_effect_mode},
    }
    if bass_enabled is not None or bass_amount is not None:
        extras["bass_enhancer"] = {
            "enabled": bool(bass_enabled),
            "params": {"amount": 0.0 if bass_amount is None else bass_amount},
        }
    manager = _deps().get_dsp_manager()
    if manager:
        extras["loudness"] = manager.load_global_extras().get("loudness", {})
    return _resolve_effects_extras(extras)


async def _finish_dsp_preset_mutation(
    *,
    load_after_create: bool,
    preset_name: str,
    refresh_reason: str,
    refresh_only_when_loaded: bool = False,
) -> dict:
    dsp_mgr = _deps().require_dsp_manager()
    if load_after_create:
        await _deps().load_dsp_preset(preset_name)
    status = dsp_mgr.get_status()
    await _deps().broadcast({"type": "dsp", "data": status})
    if load_after_create or not refresh_only_when_loaded:
        _deps().schedule_peak_monitor_refresh(refresh_reason)
    return status


def _raise_dsp_http_error(exc: Exception) -> None:
    if isinstance(exc, FileNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise bad_request(exc) from exc
    if isinstance(exc, RuntimeError):
        # Runtime state errors may carry internal detail; log fully, return generic.
        raise internal_error("DSP operation failed", exc) from exc
    raise exc


def _output_state_service():
    """Return the bound output service, or None when unwired (tests/legacy)."""
    getter = _deps().get_output_service
    return getter() if getter is not None else None


def _require_output_state_service():
    service = _output_state_service()
    if service is None:
        raise HTTPException(status_code=500, detail="Output state service is not configured")
    return service


def _parse_bank_binding(*, bank_mode: Any, bank_id: Any, expected_revision: Any) -> dict | None:
    """Normalize an optional bank target; None means no binding requested.

    FastAPI field markers (direct unit calls that omit form fields) count
    as absent, so pre-existing callers without bank arguments keep working.
    """
    if isinstance(bank_mode, FieldInfo):
        bank_mode = ""
    if isinstance(bank_id, FieldInfo):
        bank_id = ""
    if isinstance(expected_revision, FieldInfo):
        expected_revision = -1
    mode = str(bank_mode or "").strip()
    bank = str(bank_id or "").strip()
    if not mode and not bank:
        return None
    if not mode or not bank:
        raise HTTPException(status_code=400, detail="bank_mode and bank_id are required together")
    if type(expected_revision) is bool or not isinstance(expected_revision, int) or expected_revision < 0:
        raise HTTPException(status_code=400, detail="expected_revision must be a non-negative integer")
    return {"mode": mode, "bank_id": bank, "expected_revision": expected_revision}


def _validate_bank_target(service, binding: dict) -> str:
    """Fail fast (400) when the requested bank does not exist yet.

    Returns the canonical bank id (All Banks is rejected: it owns no
    presets and only switches A/B jointly).
    """
    try:
        state = service.load()
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=f"Output state is unavailable: {exc}") from exc
    modes = state.get("modes") if isinstance(state, dict) else None
    config = modes.get(binding["mode"]) if isinstance(modes, dict) else None
    try:
        if config is None:
            raise ValueError("Unknown output mode")
        if str(binding["bank_id"]).strip() == "all":
            raise ValueError("All Banks owns no presets; select a concrete filterbank")
        return resolve_bank(config, binding["bank_id"])["id"]
    except ValueError as exc:
        if "All Banks" in str(exc):
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raise HTTPException(
            status_code=400,
            detail=f"Unknown bank {binding['bank_id']} for output mode {binding['mode']}") from exc


def _optional_combine_bank(body: dict) -> str | None:
    """Owning bank for a combined preset; None keeps legacy behavior.

    Combine only tags the new file (it never auto-assigns A/B slots).
    All Banks is rejected; absent fields mean no bank was requested.
    """
    if not isinstance(body, dict):
        return None
    if isinstance(body.get("bank_mode"), FieldInfo) or isinstance(body.get("bank_id"), FieldInfo):
        return None
    mode = str(body.get("bank_mode") or "").strip()
    bank = str(body.get("bank_id") or "").strip()
    if not mode and not bank:
        return None
    if not mode or not bank:
        raise HTTPException(status_code=400, detail="bank_mode and bank_id are required together")
    service = _output_state_service()
    if service is None:
        return bank
    return _validate_bank_target(service, {"mode": mode, "bank_id": bank, "expected_revision": 0})


async def _assign_created_preset_to_bank(*, created_name: str, binding: dict) -> dict:
    """Assign a just-created preset to its bank; conflicts report partial state.

    The assignment persists through OutputService, which never touches the
    runtime; without a follow-up sync the engine would keep serving the
    previous bank content until the next unrelated edit.  The injected
    live sync renders the current head afterwards (best-effort: the
    assignment stays committed when the head cannot activate).
    """
    service = _require_output_state_service()
    try:
        service.validate_bank_preset(service.load(), binding["mode"], binding["bank_id"], created_name)
        committed = await _deps().drain_worker(
            service.apply,
            lambda state: set_bank_preset(
                state, binding["mode"], binding["bank_id"], preset=created_name),
            expected_revision=binding["expected_revision"])
    except MeasurementActiveError as exc:
        raise HTTPException(status_code=423, detail={
            "code": "bank-assign-locked", "message": str(exc),
            "created": created_name, "assigned": False}) from exc
    except StateConflictError as exc:
        raise HTTPException(status_code=409, detail={
            "code": "bank-assign-conflict", "message": str(exc),
            "created": created_name, "assigned": False}) from exc
    response = {"assigned": True, "mode": binding["mode"],
                "bank_id": binding["bank_id"], "revision": committed["revision"]}
    sync = getattr(_deps(), "sync_v2_head_live", None)
    if sync is None:
        return {**response, "live_applied": False, "live_reason": "live-sync-unavailable"}
    try:
        live = await sync()
    except Exception as exc:
        logger.warning("Bank-assign live sync failed: %s", exc)
        return {**response, "live_applied": False, "live_reason": "live-apply-failed"}
    return {**response, "live_applied": bool(live.get("live_applied")),
            "live_reason": live.get("live_reason")}


def _verify_measurement_commit(source_measurement_id: object, binding: dict | None) -> None:
    """Gate a generated PEQ/FIR commit on its source measurement still matching.

    A commit without a bank binding is not a commit into an area, and a client
    that sends no source measurement (imports, manual presets) is left to the
    pre-existing path; both stay unchanged.  A mismatch is a conflict, not bad
    input: the preset may still be created elsewhere from the same measurement
    once the area or its processing is restored.
    """
    verifier = _deps().verify_measurement_commit
    measurement_id = str(source_measurement_id or "").strip()
    if verifier is None or binding is None or not measurement_id:
        return
    try:
        verifier(measurement_id, binding)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={
            "code": "measurement-target-mismatch",
            "message": str(exc),
            "source_measurement_id": measurement_id,
            "assigned": False,
        }) from exc


def _pinned_deletion_presets(dsp_mgr) -> set[str]:
    """Presets referenced by output banks or legacy compare slots."""
    pinned: set[str] = set()
    service = _output_state_service()
    if service is not None:
        try:
            pinned.update(referenced_presets(service.load()))
        except ValueError:
            logger.warning("Output state unavailable; bank-pinned preset guard skipped")
    try:
        compare = dsp_mgr.load_compare_state()
    except Exception:
        compare = {}
    if isinstance(compare, dict):
        for key in ("presetA", "presetB"):
            if compare.get(key):
                pinned.add(compare[key])
    return pinned


@router.get("/api/dsp/extras")
async def get_dsp_extras():
    dsp_mgr = _deps().require_dsp_manager()
    return {
        "status": "ok",
        "extras": dsp_mgr.load_global_extras(),
        "excluded_presets": sorted(dsp_mgr.EXCLUDED_GLOBAL_EXTRAS_PRESETS),
    }


@router.post("/api/dsp/extras")
async def save_dsp_extras(request: Request):
    dsp_mgr = _deps().require_dsp_manager()

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # A Loudness enabled-state transition owns the canonical volume write
    # lock from the live read through the Loudness mutation and the final
    # master=100, so a parallel /api/volume or streaming volume request
    # can never interleave.  All transferred values are (re)read under the
    # lock. Other extras updates also hold it until runtime reconciliation
    # completes so no newer preset/volume mutation can pass the rollback.
    #
    # The full read-modify-write (extras read, JSON merge, resolution, manager
    # mutation/persistence) runs under the central DSP mutation
    # ownership so a parallel coordinator/volume/SPL mutation can never
    # interleave between the read and the write.  Lock order: canonical
    # volume write lock first, then DSP mutation lock.
    canonical_lock = _deps().canonical_volume_write_lock()
    await canonical_lock.acquire()
    try:
        async with _deps().dsp_mutation_lock():
            previous = dsp_mgr.load_global_extras()
            parsed = _merge_effects_extras_from_json(previous, body)
            try:
                extras = _resolve_effects_extras(parsed)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "invalid_effects_extras", "message": str(exc)},
                ) from exc
            start = await _deps().volume_state_for_manager(dsp_mgr)
            if extras == previous:
                logger.info("Ignored unchanged effects extras update")
                return {
                    "status": "ok",
                    "extras": extras,
                    "updated_presets": 0,
                    "skipped_presets": [],
                }
            runtime_strength_change = _is_pure_loudness_strength_change(previous, extras)
            runtime_autogain_loudness_change = _is_runtime_autogain_loudness_change(
                previous, extras
            )
            try:
                if runtime_strength_change:
                    result = await _deps().drain_worker(
                        dsp_mgr.apply_loudness_strength_runtime, previous, extras
                    )
                elif runtime_autogain_loudness_change:
                    result = await _deps().drain_worker(
                        dsp_mgr.apply_autogain_loudness_runtime, previous, extras
                    )
                else:
                    result = await _deps().drain_worker(
                        dsp_mgr.apply_global_extras_to_all_presets, extras
                    )
                active_preset = dsp_mgr.get_active_preset()
                if (not result.get("runtime_applied") and active_preset
                        and active_preset not in dsp_mgr.EXCLUDED_GLOBAL_EXTRAS_PRESETS):
                    await _deps().load_dsp_preset(active_preset, _locks_held=True)
            except (Exception, asyncio.CancelledError):
                # The worker may have committed before raising or propagating
                # cancellation. Keep both locks until recovery has completed.
                async def recover_previous_extras():
                    if dsp_mgr.load_global_extras() != previous:
                        await _deps().drain_worker(dsp_mgr.save_global_extras, previous)
                        active = dsp_mgr.get_active_preset()
                        if active and active not in dsp_mgr.EXCLUDED_GLOBAL_EXTRAS_PRESETS:
                            await _deps().load_dsp_preset(active, _locks_held=True)
                    await _deps().restore_volume_state(dsp_mgr, start)

                recovery = asyncio.create_task(recover_previous_extras())
                try:
                    while not recovery.done():
                        try:
                            await asyncio.shield(recovery)
                        except asyncio.CancelledError:
                            pass
                        except Exception:
                            break
                    recovery.result()
                except (Exception, asyncio.CancelledError):
                    logger.exception("Failed to recover previous extras after DSP update failure")
                    dsp_runtime = _deps().get_dsp_runtime()
                    if dsp_runtime is not None:
                        stop = asyncio.create_task(dsp_runtime.stop())
                        try:
                            while not stop.done():
                                try:
                                    await asyncio.shield(stop)
                                except asyncio.CancelledError:
                                    pass
                                except Exception:
                                    break
                            stop.result()
                        except (Exception, asyncio.CancelledError):
                            logger.exception("Failed to stop uncertain DSP runtime after extras recovery failure")
                    raise
                raise
    finally:
        canonical_lock.release()

    status = dsp_mgr.get_status()
    await _deps().broadcast({"type": "dsp", "data": status})
    _deps().schedule_peak_monitor_refresh("global-extras-update")
    return {
        "status": "ok",
        "extras": result["extras"],
        "updated_presets": result["updated"],
        "skipped_presets": result["skipped"],
    }


@router.get("/api/dsp/presets")
async def list_dsp_presets():
    return _deps().require_dsp_manager().get_status()


@router.get("/api/dsp/presets/{preset_name}/file")
async def download_dsp_preset_file(preset_name: str):
    dsp_mgr = _deps().require_dsp_manager()
    preset = next((item for item in dsp_mgr.list_presets() if item.get("name") == preset_name), None)
    if not preset:
        raise HTTPException(status_code=404, detail="Preset not found")
    preset_path = Path(str(preset.get("path") or "")).resolve()
    if not path_within_root(preset_path, dsp_mgr.output_dir):
        raise HTTPException(status_code=403, detail="Preset path outside DSP preset directory")
    if not preset_path.is_file():
        raise HTTPException(status_code=404, detail="Preset file missing")
    try:
        payload = json.loads(preset_path.read_text())
    except Exception:
        payload = None
    kernel_names = dsp_mgr._extract_kernel_names_from_payload(payload) if isinstance(payload, dict) else set()
    ir_paths = []
    for kernel_name in sorted(kernel_names):
        ir_paths.extend(dsp_mgr._find_ir_paths_for_kernel_name(kernel_name))
    if ir_paths:
        with tempfile.NamedTemporaryFile(prefix="fxroute-preset-", suffix=".zip", delete=False) as temp_file:
            temp_zip_path = Path(temp_file.name)
        used_names = set()
        try:
            with zipfile.ZipFile(temp_zip_path, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.write(preset_path, arcname="preset.json")
                for ir_path in ir_paths:
                    if ir_path.is_file() and path_within_root(ir_path.resolve(), dsp_mgr.irs_dir):
                        archive.write(ir_path, arcname=zip_album.dedupe_archive_name(ir_path.name, used_names))
                manifest = {
                    "type": "fxroute-preset-bundle",
                    "version": 1,
                    "preset": preset_path.name,
                    "irs": [path.name for path in ir_paths if path.is_file()],
                }
                archive.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
        except Exception:
            temp_zip_path.unlink(missing_ok=True)
            raise
        return FileResponse(
            temp_zip_path,
            filename=f"{preset_path.stem}.zip",
            media_type="application/zip",
            background=BackgroundTask(_cleanup_temp_file, temp_zip_path),
        )
    return FileResponse(preset_path, filename=preset_path.name)


@router.post("/api/dsp/compare")
async def save_dsp_compare(request: Request):
    dsp_mgr = _deps().require_dsp_manager()

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    compare = dsp_mgr.save_compare_state({
        "presetA": body.get("presetA", body.get("preset_a", "")),
        "presetB": body.get("presetB", body.get("preset_b", "")),
        "activeSide": body.get("activeSide", body.get("active_side")),
    })

    status = dsp_mgr.get_status()
    await _deps().broadcast({"type": "dsp", "data": status})
    return {"status": "ok", "compare": compare}


@router.post("/api/dsp/presets/combine")
async def combine_dsp_presets(request: Request):
    dsp_mgr = _deps().require_dsp_manager()

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON body, expected {'presetName': '...', 'presetNames': ['Preset 1', 'Preset 2']}",
        )

    preset_name = (body.get("presetName") or body.get("preset_name") or "").strip()
    preset_names = body.get("presetNames", body.get("preset_names")) or []
    load_after_create = bool(body.get("loadAfterCreate", body.get("load_after_create", False)))

    if not preset_name:
        raise HTTPException(status_code=400, detail="presetName is required")
    if not isinstance(preset_names, list):
        raise HTTPException(status_code=400, detail="presetNames must be an array")
    canonical_bank = _optional_combine_bank(body)

    try:
        async with _deps().dsp_mutation_lock():
            created = dsp_mgr.combine_presets(preset_name, preset_names, bank=canonical_bank)
        status = await _finish_dsp_preset_mutation(
            load_after_create=load_after_create,
            preset_name=created["name"],
            refresh_reason="combine-presets",
            refresh_only_when_loaded=True,
        )
        return {
            "status": "ok",
            "preset": created,
            "loaded": bool(load_after_create),
            "active_preset": status.get("active_preset"),
        }
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        _raise_dsp_http_error(e)


@router.post("/api/dsp/presets/load")
async def load_dsp_preset(request: Request):
    dsp_mgr = _deps().require_dsp_manager()

    try:
        body = await request.json()
        preset_name = (body.get("preset_name") or "").strip()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body, expected {\"preset_name\": \"...\"}")

    if not preset_name:
        raise HTTPException(status_code=400, detail="preset_name is required")

    try:
        async with _deps().get_dsp_preset_load_lock():
            await _deps().load_dsp_preset(preset_name)
            compare = dsp_mgr.load_compare_state()
            if compare.get("presetA") == preset_name:
                compare["activeSide"] = "A"
                dsp_mgr.save_compare_state(compare)
            elif compare.get("presetB") == preset_name:
                compare["activeSide"] = "B"
                dsp_mgr.save_compare_state(compare)
            status = dsp_mgr.get_status()
        runtime = _deps().get_dsp_runtime()
        if (
            runtime is not None
            and runtime.snapshot().get("active")
            and not runtime.sync_in_progress
        ):
            # A running helper sync owns the graph and verifies/repairs its
            # own links; a concurrent reclean would race that repair.
            await runtime._reclean_guarded(skip_if_locked=False)
        await _deps().broadcast({"type": "dsp", "data": status})
        _deps().schedule_peak_monitor_refresh("preset-load")
        return {"status": "ok", "active_preset": preset_name, "compare": status.get("compare")}
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        _raise_dsp_http_error(e)


@router.post("/api/dsp/irs/upload")
async def upload_dsp_ir(file: UploadFile = File(...)):
    dsp_mgr = _deps().require_dsp_manager()

    tmp_path = None
    try:
        suffix = Path(file.filename or "upload.ir").suffix
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = Path(tmp.name)
            await save_upload_to_file(file, tmp, DSP_IR_MAX_BYTES)

        uploaded = await _deps().run_locked_worker(
            _deps().dsp_mutation_lock(),
            dsp_mgr.upload_ir,
            tmp_path,
            file.filename or tmp_path.name,
        )
        status = dsp_mgr.get_status()
        await _deps().broadcast({"type": "dsp", "data": status})
        _deps().schedule_peak_monitor_refresh("ir-upload")
        return {"status": "ok", "ir": uploaded}
    except UploadTooLargeError as e:
        raise HTTPException(status_code=413, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise internal_error("DSP IR upload failed", e)
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                logger.exception("Failed to remove uploaded IR temp file %s", tmp_path)


@router.post("/api/dsp/presets/create-convolver")
async def create_convolver_preset(
    preset_name: str = Form(...),
    ir_filename: str = Form(...),
    load_after_create: bool = Form(False),
    limiter_enabled: bool = Form(False),
    headroom_enabled: bool = Form(False),
    headroom_gain_db: float = Form(-3.0),
    autogain_enabled: bool = Form(False),
    autogain_target_db: float = Form(-12.0),
    delay_enabled: bool = Form(False),
    delay_left_ms: float = Form(0.0),
    delay_right_ms: float = Form(0.0),
    tone_effect_enabled: bool = Form(False),
    tone_effect_mode: str = Form("crystalizer"),
    bank_mode: str = Form(""),
    bank_id: str = Form(""),
    expected_revision: int = Form(-1),
    source_measurement_id: str = Form(""),
):
    dsp_mgr = _deps().require_dsp_manager()
    binding = _parse_bank_binding(
        bank_mode=bank_mode, bank_id=bank_id, expected_revision=expected_revision)
    canonical_bank = None
    if binding is not None:
        canonical_bank = _validate_bank_target(_require_output_state_service(), binding)
        binding = {**binding, "bank_id": canonical_bank}
    # The bank is known to exist now, so a target mismatch is a real conflict.
    _verify_measurement_commit(source_measurement_id, binding)

    try:
        # The canonical loudness read inside _effects_extras_from_form must
        # happen under the same mutation ownership as the preset creation:
        # a parallel extras mutation may never be frozen into the new preset
        # from a stale pre-lock snapshot.
        async with _deps().dsp_mutation_lock():
            extras = _effects_extras_from_form(
                limiter_enabled=limiter_enabled,
                headroom_enabled=headroom_enabled,
                headroom_gain_db=headroom_gain_db,
                autogain_enabled=autogain_enabled,
                autogain_target_db=autogain_target_db,
                delay_enabled=delay_enabled,
                delay_left_ms=delay_left_ms,
                delay_right_ms=delay_right_ms,
                tone_effect_enabled=tone_effect_enabled,
                tone_effect_mode=tone_effect_mode,
            )
            created = dsp_mgr.create_convolver_preset(preset_name, ir_filename, extras=extras,
                                                      bank=canonical_bank)
        status = await _finish_dsp_preset_mutation(
            load_after_create=load_after_create,
            preset_name=created["name"],
            refresh_reason="create-convolver",
        )
        response = {
            "status": "ok",
            "preset": created,
            "loaded": bool(load_after_create),
            "active_preset": status.get("active_preset"),
        }
        if binding is not None:
            response["bank"] = await _assign_created_preset_to_bank(
                created_name=created["name"], binding=binding)
        return response
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        _raise_dsp_http_error(e)


@router.post("/api/dsp/presets/import-json")
async def import_dsp_preset_json(
    file: UploadFile = File(...),
    load_after_create: bool = Form(False),
    bank_mode: str = Form(""),
    bank_id: str = Form(""),
    expected_revision: int = Form(-1),
):
    dsp_mgr = _deps().require_dsp_manager()
    binding = _parse_bank_binding(bank_mode=bank_mode, bank_id=bank_id, expected_revision=expected_revision)
    canonical_bank = None
    if binding is not None:
        canonical_bank = _validate_bank_target(_require_output_state_service(), binding)
        binding = {**binding, "bank_id": canonical_bank}

    try:
        content = (await read_upload(file, DSP_PRESET_TEXT_MAX_BYTES)).decode("utf-8-sig")
        async with _deps().dsp_mutation_lock():
            created = dsp_mgr.import_preset_json(file.filename or "preset.json", content,
                                                 bank=canonical_bank)
        status = await _finish_dsp_preset_mutation(
            load_after_create=load_after_create,
            preset_name=created["name"],
            refresh_reason="import-preset-json",
        )
        response = {
            "status": "ok",
            "preset": created,
            "loaded": bool(load_after_create),
            "active_preset": status.get("active_preset"),
        }
        if binding is not None:
            response["bank"] = await _assign_created_preset_to_bank(
                created_name=created["name"], binding=binding)
        return response
    except UploadTooLargeError as e:
        raise HTTPException(status_code=413, detail=str(e))
    except UnicodeDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Preset JSON is not valid UTF-8 text: {e}")
    except (ValueError, RuntimeError) as e:
        _raise_dsp_http_error(e)


@router.post("/api/dsp/presets/import-bundle")
async def import_dsp_preset_bundle(
    file: UploadFile = File(...),
    load_after_create: bool = Form(False),
    bank_mode: str = Form(""),
    bank_id: str = Form(""),
    expected_revision: int = Form(-1),
):
    dsp_mgr = _deps().require_dsp_manager()
    binding = _parse_bank_binding(bank_mode=bank_mode, bank_id=bank_id, expected_revision=expected_revision)
    canonical_bank = None
    if binding is not None:
        canonical_bank = _validate_bank_target(_require_output_state_service(), binding)
        binding = {**binding, "bank_id": canonical_bank}

    temp_zip_path = None
    import_succeeded = False
    staged_irs = []
    ir_backups = []
    new_ir_destinations = []
    try:
        with tempfile.NamedTemporaryFile(prefix="fxroute-preset-import-", suffix=".zip", delete=False) as temp_file:
            temp_zip_path = Path(temp_file.name)
            await save_upload_to_file(file, temp_file, DSP_BUNDLE_MAX_BYTES)

        zip_album.check_zip_file_before_open(
            temp_zip_path,
            max_members=PRESET_BUNDLE_MAX_MEMBERS,
            max_central_directory_bytes=PRESET_BUNDLE_MAX_CENTRAL_DIRECTORY_BYTES,
        )
        with zipfile.ZipFile(temp_zip_path) as archive:
            zip_album.check_zip_limits(
                archive,
                max_members=PRESET_BUNDLE_MAX_MEMBERS,
                max_total_uncompressed_bytes=PRESET_BUNDLE_MAX_TOTAL_UNCOMPRESSED_BYTES,
                max_member_bytes=PRESET_BUNDLE_MAX_MEMBER_BYTES,
            )
            safe_members = []
            for member in archive.infolist():
                if member.is_dir():
                    continue
                safe_relative = zip_album.is_safe_relative_zip_path(member.filename)
                if safe_relative is None:
                    raise zip_album.ZipLimitError(
                        f"Unsafe ZIP member path: {member.filename!r}"
                    )
                reason = zip_album.zip_member_hardening_reason(member)
                if reason:
                    raise zip_album.ZipLimitError(
                        f"Unsafe ZIP member {member.filename!r}: {reason}"
                    )
                safe_members.append((member, safe_relative))

            if archive.testzip() is not None:
                raise HTTPException(status_code=400, detail="Invalid ZIP archive")

            json_members = [(member, rel) for member, rel in safe_members if rel.suffix.lower() == ".json" and rel.name.lower() != "manifest.json"]
            preferred_json = next(((member, rel) for member, rel in json_members if rel.name.lower() == "preset.json"), None)
            if preferred_json is None:
                preferred_json = json_members[0] if len(json_members) == 1 else None
            if preferred_json is None:
                raise HTTPException(status_code=400, detail="Preset bundle must contain exactly one preset JSON")

            preset_member, preset_rel = preferred_json
            preset_text = zip_album.read_member_bounded(
                archive, preset_member, PRESET_BUNDLE_MAX_JSON_BYTES
            ).decode("utf-8-sig")
            try:
                preset_payload = json.loads(preset_text)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Preset JSON is invalid: {e}") from e
            kernel_names = dsp_mgr._extract_kernel_names_from_payload(preset_payload if isinstance(preset_payload, dict) else None)

            async with _deps().dsp_mutation_lock():
                dsp_mgr.irs_dir.mkdir(parents=True, exist_ok=True)
                imported_irs = []
                ir_members_by_stem = {}
                for member, rel in safe_members:
                    if rel.suffix.lower() not in {".irs", ".wav"}:
                        continue
                    clean_ir_name = Path(rel.name).name
                    stem = Path(clean_ir_name).stem
                    if kernel_names and stem not in kernel_names:
                        continue
                    existing = ir_members_by_stem.get(stem)
                    if existing is None or rel.suffix.lower() == ".irs":
                        ir_members_by_stem[stem] = (member, clean_ir_name)

                extracted_total = 0
                for _, (member, clean_ir_name) in sorted(ir_members_by_stem.items()):
                    destination = dsp_mgr.irs_dir / clean_ir_name
                    if destination.is_symlink():
                        raise ValueError(
                            f"Import blocked: existing IR path is a symlink: {clean_ir_name}"
                        )
                    if destination.is_dir():
                        raise ValueError(
                            f"Import blocked: existing IR path is a directory: {clean_ir_name}"
                        )
                    stage_path = dsp_mgr.irs_dir / f".fxroute-bundle-stage-{uuid4().hex}"
                    try:
                        remaining = min(
                            PRESET_BUNDLE_MAX_TOTAL_UNCOMPRESSED_BYTES - extracted_total,
                            PRESET_BUNDLE_MAX_MEMBER_BYTES,
                        )
                        written = zip_album.copy_member_bounded(
                            archive, member, stage_path, remaining_bytes=remaining
                        )
                    except BaseException:
                        stage_path.unlink(missing_ok=True)
                        raise
                    extracted_total += written
                    staged_irs.append(stage_path)
                    if destination.exists():
                        backup_path = dsp_mgr.irs_dir / f".fxroute-bundle-backup-{uuid4().hex}"
                        os.replace(destination, backup_path)
                        ir_backups.append((destination, backup_path))
                    else:
                        new_ir_destinations.append(destination)
                    os.replace(stage_path, destination)
                    imported_irs.append(destination.name)

                missing_kernels = [name for name in sorted(kernel_names) if not dsp_mgr._find_ir_paths_for_kernel_name(name)]
                if missing_kernels:
                    raise HTTPException(status_code=400, detail=f"Preset bundle is missing IR file(s): {', '.join(missing_kernels)}")

                preset_filename = preset_rel.name if preset_rel.name.lower() != "preset.json" else (Path(file.filename or "preset.json").stem + ".json")
                created = dsp_mgr.import_preset_json(preset_filename, preset_text, bank=canonical_bank)
            import_succeeded = True
            status = await _finish_dsp_preset_mutation(
                load_after_create=load_after_create,
                preset_name=created["name"],
                refresh_reason="import-preset-bundle",
            )
            response = {
                "status": "ok",
                "preset": created,
                "irs": imported_irs,
                "loaded": bool(load_after_create),
                "active_preset": status.get("active_preset"),
            }
            if binding is not None:
                response["bank"] = await _assign_created_preset_to_bank(
                    created_name=created["name"], binding=binding)
            return response
    except UploadTooLargeError as e:
        raise HTTPException(status_code=413, detail=str(e))
    except zip_album.ZipLimitError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid ZIP archive")
    except UnicodeDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Preset JSON is not valid UTF-8 text: {e}")
    except (ValueError, RuntimeError) as e:
        _raise_dsp_http_error(e)
    finally:
        if import_succeeded:
            for _, backup_path in ir_backups:
                try:
                    backup_path.unlink(missing_ok=True)
                except Exception:
                    logger.exception("Failed to remove bundle IR backup %s", backup_path)
        else:
            for destination in new_ir_destinations:
                try:
                    destination.unlink(missing_ok=True)
                except Exception:
                    logger.exception("Failed to remove partially imported bundle IR %s", destination)
            for destination, backup_path in ir_backups:
                try:
                    os.replace(backup_path, destination)
                except Exception:
                    logger.exception("Failed to restore bundle IR backup %s", backup_path)
        for stage_path in staged_irs:
            try:
                stage_path.unlink(missing_ok=True)
            except Exception:
                logger.exception("Failed to remove staged bundle IR %s", stage_path)
        if temp_zip_path is not None:
            try:
                temp_zip_path.unlink(missing_ok=True)
            except Exception:
                logger.exception("Failed to remove imported preset bundle temp file %s", temp_zip_path)


@router.post("/api/dsp/presets/create-with-ir")
async def create_convolver_preset_with_ir(
    preset_name: str = Form(...),
    load_after_create: bool = Form(False),
    limiter_enabled: bool = Form(False),
    headroom_enabled: bool = Form(False),
    headroom_gain_db: float = Form(-3.0),
    autogain_enabled: bool = Form(False),
    autogain_target_db: float = Form(-12.0),
    delay_enabled: bool = Form(False),
    delay_left_ms: float = Form(0.0),
    delay_right_ms: float = Form(0.0),
    bass_enabled: bool = Form(False),
    bass_amount: float = Form(0.0),
    tone_effect_enabled: bool = Form(False),
    tone_effect_mode: str = Form("crystalizer"),
    file: UploadFile = File(...),
    bank_mode: str = Form(""),
    bank_id: str = Form(""),
    expected_revision: int = Form(-1),
    source_measurement_id: str = Form(""),
):
    dsp_mgr = _deps().require_dsp_manager()

    binding = _parse_bank_binding(
        bank_mode=bank_mode, bank_id=bank_id, expected_revision=expected_revision)
    canonical_bank = None
    if binding is not None:
        canonical_bank = _validate_bank_target(_require_output_state_service(), binding)
        binding = {**binding, "bank_id": canonical_bank}
    # The bank is known to exist now, so a target mismatch is a conflict.
    _verify_measurement_commit(source_measurement_id, binding)

    tmp_path = None
    try:
        suffix = Path(file.filename or "upload.ir").suffix
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = Path(tmp.name)
            await save_upload_to_file(file, tmp, DSP_IR_MAX_BYTES)

        # The canonical loudness read inside _effects_extras_from_form must
        # happen under the same mutation ownership as the preset creation:
        # a parallel extras mutation during the (potentially slow) upload
        # staging may never be frozen into the new preset from a stale
        # pre-lock snapshot.  The lock is acquired here (not via
        # _run_locked_worker, which would re-acquire) and the blocking
        # manager call runs through the cancellation-safe worker.
        async with _deps().dsp_mutation_lock():
            extras = _effects_extras_from_form(
                limiter_enabled=limiter_enabled,
                headroom_enabled=headroom_enabled,
                headroom_gain_db=headroom_gain_db,
                autogain_enabled=autogain_enabled,
                autogain_target_db=autogain_target_db,
                delay_enabled=delay_enabled,
                delay_left_ms=delay_left_ms,
                delay_right_ms=delay_right_ms,
                bass_enabled=bass_enabled,
                bass_amount=bass_amount,
                tone_effect_enabled=tone_effect_enabled,
                tone_effect_mode=tone_effect_mode,
            )
            created = await _deps().drain_worker(
                dsp_mgr.create_convolver_preset_with_upload,
                preset_name,
                tmp_path,
                file.filename or tmp_path.name,
                extras=extras,
                bank=canonical_bank,
            )
        status = await _finish_dsp_preset_mutation(
            load_after_create=load_after_create,
            preset_name=created["preset"]["name"],
            refresh_reason="create-with-ir",
        )
        response = {
            "status": "ok",
            "ir": created["ir"],
            "preset": created["preset"],
            "loaded": bool(load_after_create),
            "active_preset": status.get("active_preset"),
        }
        if binding is not None:
            response["bank"] = await _assign_created_preset_to_bank(
                created_name=created["preset"]["name"], binding=binding)
        return response
    except HTTPException:
        # Already an HTTP outcome (for example the measurement-target gate).
        raise
    except UploadTooLargeError as e:
        raise HTTPException(status_code=413, detail=str(e))
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        _raise_dsp_http_error(e)
    except Exception as e:
        raise internal_error("DSP create-with-ir failed", e)
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                logger.exception("Failed to remove create-with-ir temp file %s", tmp_path)


@router.post("/api/dsp/presets/create-peq")
async def create_peq_preset(request: Request):
    dsp_mgr = _deps().require_dsp_manager()

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON body, expected {'presetName': '...', 'peq': {...}, 'loadAfterCreate': true|false}",
        )

    preset_name = (body.get("presetName") or body.get("preset_name") or "").strip()
    peq_definition = body.get("peq")
    load_after_create = bool(body.get("loadAfterCreate", body.get("load_after_create", False)))
    extras = _parse_effects_extras_from_json(body)
    binding = _parse_bank_binding(
        bank_mode=body.get("bank_mode"), bank_id=body.get("bank_id"),
        expected_revision=body.get("expected_revision"))
    source_measurement_id = body.get("source_measurement_id", body.get("sourceMeasurementId"))

    if not preset_name:
        raise HTTPException(status_code=400, detail="presetName is required")
    if peq_definition is None:
        raise HTTPException(status_code=400, detail="peq is required")
    canonical_bank = None
    if binding is not None:
        canonical_bank = _validate_bank_target(_require_output_state_service(), binding)
        binding = {**binding, "bank_id": canonical_bank}
    # The bank is known to exist now, so a target mismatch is a real conflict.
    _verify_measurement_commit(source_measurement_id, binding)

    try:
        async with _deps().dsp_mutation_lock():
            created = dsp_mgr.create_peq_preset(preset_name, peq_definition, extras=extras,
                                                bank=canonical_bank)
        status = await _finish_dsp_preset_mutation(
            load_after_create=load_after_create,
            preset_name=created["name"],
            refresh_reason="create-peq",
        )
        response = {
            "status": "ok",
            "preset": created,
            "loaded": bool(load_after_create),
            "active_preset": status.get("active_preset"),
        }
        if binding is not None:
            response["bank"] = await _assign_created_preset_to_bank(
                created_name=created["name"], binding=binding)
        return response
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        _raise_dsp_http_error(e)


@router.post("/api/dsp/presets/import-rew-peq")
async def import_rew_peq_preset(
    preset_name: str = Form(...),
    load_after_create: bool = Form(False),
    limiter_enabled: bool = Form(False),
    headroom_enabled: bool = Form(False),
    headroom_gain_db: float = Form(-3.0),
    autogain_enabled: bool = Form(False),
    autogain_target_db: float = Form(-12.0),
    delay_enabled: bool = Form(False),
    delay_left_ms: float = Form(0.0),
    delay_right_ms: float = Form(0.0),
    bass_enabled: bool = Form(False),
    bass_amount: float = Form(0.0),
    tone_effect_enabled: bool = Form(False),
    tone_effect_mode: str = Form("crystalizer"),
    file: UploadFile = File(...),
    bank_mode: str = Form(""),
    bank_id: str = Form(""),
    expected_revision: int = Form(-1),
):
    dsp_mgr = _deps().require_dsp_manager()

    if not preset_name.strip():
        raise HTTPException(status_code=400, detail="preset_name is required")

    binding = _parse_bank_binding(
        bank_mode=bank_mode, bank_id=bank_id, expected_revision=expected_revision)
    canonical_bank = None
    if binding is not None:
        canonical_bank = _validate_bank_target(_require_output_state_service(), binding)
        binding = {**binding, "bank_id": canonical_bank}

    try:
        content = await read_upload(file, DSP_PRESET_TEXT_MAX_BYTES)
        rew_text = content.decode("utf-8-sig")
    except UploadTooLargeError as e:
        raise HTTPException(status_code=413, detail=str(e))
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="REW import file must be UTF-8 text")

    try:
        # Canonical extras resolution under the same mutation ownership as
        # the import: never freeze a stale pre-lock loudness snapshot into
        # the new preset.
        async with _deps().dsp_mutation_lock():
            extras = _effects_extras_from_form(
                limiter_enabled=limiter_enabled,
                headroom_enabled=headroom_enabled,
                headroom_gain_db=headroom_gain_db,
                autogain_enabled=autogain_enabled,
                autogain_target_db=autogain_target_db,
                delay_enabled=delay_enabled,
                delay_left_ms=delay_left_ms,
                delay_right_ms=delay_right_ms,
                bass_enabled=bass_enabled,
                bass_amount=bass_amount,
                tone_effect_enabled=tone_effect_enabled,
                tone_effect_mode=tone_effect_mode,
            )
            created = dsp_mgr.create_peq_preset_from_rew_text(preset_name, rew_text, extras=extras,
                                                             bank=canonical_bank)
        status = await _finish_dsp_preset_mutation(
            load_after_create=load_after_create,
            preset_name=created["name"],
            refresh_reason="import-rew-peq",
        )
        response = {
            "status": "ok",
            "preset": created,
            "loaded": bool(load_after_create),
            "active_preset": status.get("active_preset"),
        }
        if binding is not None:
            response["bank"] = await _assign_created_preset_to_bank(
                created_name=created["name"], binding=binding)
        return response
    except (ValueError, RuntimeError) as e:
        _raise_dsp_http_error(e)


@router.post("/api/dsp/presets/import-filter-dual")
async def import_dual_filter_preset(
    preset_name: str = Form(...),
    left_text: str = Form(""),
    right_text: str = Form(""),
    load_after_create: bool = Form(False),
    limiter_enabled: bool = Form(False),
    headroom_enabled: bool = Form(False),
    headroom_gain_db: float = Form(-3.0),
    autogain_enabled: bool = Form(False),
    autogain_target_db: float = Form(-12.0),
    delay_enabled: bool = Form(False),
    delay_left_ms: float = Form(0.0),
    delay_right_ms: float = Form(0.0),
    bass_enabled: bool = Form(False),
    bass_amount: float = Form(0.0),
    tone_effect_enabled: bool = Form(False),
    tone_effect_mode: str = Form("crystalizer"),
    left_file: Optional[UploadFile] = File(None),
    right_file: Optional[UploadFile] = File(None),
    bank_mode: str = Form(""),
    bank_id: str = Form(""),
    expected_revision: int = Form(-1),
    source_measurement_id: str = Form(""),
):
    dsp_mgr = _deps().require_dsp_manager()

    if not preset_name.strip():
        raise HTTPException(status_code=400, detail="preset_name is required")

    # Shared form values; the canonical loudness read itself happens only
    # inside the mutation ownership below, per import branch.
    def _form_extras_kwargs() -> dict:
        return dict(
            limiter_enabled=limiter_enabled,
            headroom_enabled=headroom_enabled,
            headroom_gain_db=headroom_gain_db,
            autogain_enabled=autogain_enabled,
            autogain_target_db=autogain_target_db,
            delay_enabled=delay_enabled,
            delay_left_ms=delay_left_ms,
            delay_right_ms=delay_right_ms,
            bass_enabled=bass_enabled,
            bass_amount=bass_amount,
            tone_effect_enabled=tone_effect_enabled,
            tone_effect_mode=tone_effect_mode,
        )

    def _detect_upload_kind(upload: Optional[UploadFile]) -> Optional[str]:
        if not upload or not (upload.filename or "").strip():
            return None
        suffix = Path(upload.filename).suffix.lower()
        if suffix in {".txt"}:
            return "rew-text"
        if suffix in {".irs", ".wav"}:
            return "convolver"
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {upload.filename}")

    left_kind = _detect_upload_kind(left_file)
    right_kind = _detect_upload_kind(right_file)

    if bool(left_kind) != bool(right_kind):
        raise HTTPException(status_code=400, detail="Provide both Left and Right files, or neither")

    binding = _parse_bank_binding(
        bank_mode=bank_mode, bank_id=bank_id, expected_revision=expected_revision)
    canonical_bank = None
    if binding is not None:
        canonical_bank = _validate_bank_target(_require_output_state_service(), binding)
        binding = {**binding, "bank_id": canonical_bank}
    # The bank is known to exist now, so a target mismatch is a real conflict.
    _verify_measurement_commit(source_measurement_id, binding)

    tmp_paths = []
    try:
        if left_kind == "convolver" and right_kind == "convolver":
            async def _save_temp(upload: UploadFile) -> Path:
                suffix = Path(upload.filename or "upload.ir").suffix or ".ir"
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                    tmp_path = Path(tmp.name)
                    # Register before writing so a read/write failure still
                    # cleans up the partial file in the outer finally.
                    tmp_paths.append(tmp_path)
                    await save_upload_to_file(upload, tmp, DSP_IR_MAX_BYTES)
                    return tmp_path

            left_tmp = await _save_temp(left_file)
            right_tmp = await _save_temp(right_file)

            # Canonical extras resolution under the same mutation ownership
            # as the creation; the blocking manager call runs through the
            # cancellation-safe worker (the lock is acquired here, not via
            # _run_locked_worker).
            async with _deps().dsp_mutation_lock():
                extras = _effects_extras_from_form(**_form_extras_kwargs())
                created = await _deps().drain_worker(
                    dsp_mgr.create_convolver_preset_with_dual_uploads,
                    preset_name,
                    left_tmp,
                    left_file.filename or left_tmp.name,
                    right_tmp,
                    right_file.filename or right_tmp.name,
                    extras=extras,
                    bank=canonical_bank,
                )
            import_kind = "dual-convolver"
        else:
            if left_kind == "rew-text" and right_kind == "rew-text":
                try:
                    left_text = (await read_upload(left_file, DSP_PRESET_TEXT_MAX_BYTES)).decode("utf-8-sig")
                    right_text = (await read_upload(right_file, DSP_PRESET_TEXT_MAX_BYTES)).decode("utf-8-sig")
                except UnicodeDecodeError:
                    raise HTTPException(status_code=400, detail="Dual REW import files must be UTF-8 text")

            left_text = str(left_text or "").strip()
            right_text = str(right_text or "").strip()
            if not left_text or not right_text:
                raise HTTPException(status_code=400, detail="Provide Left and Right REW text, or Left and Right .irs/.wav files")

            async with _deps().dsp_mutation_lock():
                extras = _effects_extras_from_form(**_form_extras_kwargs())
                created = dsp_mgr.create_dual_peq_preset_from_rew_texts(
                    preset_name,
                    left_text,
                    right_text,
                    extras=extras,
                    bank=canonical_bank,
                )
            import_kind = "dual-peq"

        created_preset = created["preset"] if import_kind == "dual-convolver" else created
        status = await _finish_dsp_preset_mutation(
            load_after_create=load_after_create,
            preset_name=created_preset["name"],
            refresh_reason="import-filter-dual",
        )
        response = {
            "status": "ok",
            "import_kind": import_kind,
            "preset": created_preset,
            "ir": created.get("ir") if isinstance(created, dict) else None,
            "loaded": bool(load_after_create),
            "active_preset": status.get("active_preset"),
        }
        if binding is not None:
            response["bank"] = await _assign_created_preset_to_bank(
                created_name=created_preset["name"], binding=binding)
        return response
    except UploadTooLargeError as e:
        raise HTTPException(status_code=413, detail=str(e))
    except (ValueError, RuntimeError) as e:
        _raise_dsp_http_error(e)
    finally:
        for tmp_path in tmp_paths:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                logger.exception("Failed to remove dual import temp file %s", tmp_path)


@router.post("/api/dsp/presets/delete")
async def delete_dsp_preset(request: Request):
    dsp_mgr = _deps().require_dsp_manager()

    try:
        body = await request.json()
        preset_name = (body.get("preset_name") or "").strip()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body, expected {\"preset_name\": \"...\"}")

    if not preset_name:
        raise HTTPException(status_code=400, detail="preset_name is required")

    try:
        async with _deps().dsp_mutation_lock():
            deleted_active = dsp_mgr.get_active_preset() == clean_name(preset_name)
            dsp_mgr.delete_preset(
                preset_name, pinned_presets=_pinned_deletion_presets(dsp_mgr))
        if deleted_active:
            # The manager already moved the persisted active state to the
            # Neutral fallback; resync the running native engine through the
            # normal locked loader so it never keeps processing the deleted
            # preset's graph.
            await _deps().load_dsp_preset(dsp_mgr.get_active_preset() or "Neutral")
        status = dsp_mgr.get_status()
        await _deps().broadcast({"type": "dsp", "data": status})
        _deps().schedule_peak_monitor_refresh("preset-delete")
        return {"status": "ok", "deleted": preset_name}
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        _raise_dsp_http_error(e)
