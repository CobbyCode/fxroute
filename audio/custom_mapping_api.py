# SPDX-License-Identifier: AGPL-3.0-only

"""HTTP surface of the per-device custom input mapping.

The devices, their capture channels and their mappings come from the source
overview (``custom_mapping_context``), so the dialog and the input list always
describe the same devices. Every call may name a device by its card base;
without one the overview's default device is used. Writes run through
``SourceTransitions.change_input_config`` under the source-transition lock.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException, Request

from audio import custom_input_mapping
from http_errors import bad_request

logger = logging.getLogger(__name__)

router = APIRouter()


@dataclass(frozen=True)
class CustomMappingApiDeps:
    """Application services injected from main.py."""

    read_overview: Callable[[], Awaitable[dict[str, Any]]]
    change_input_config: Callable[..., Awaitable[dict[str, Any]]]
    disarm_auto_select: Callable[[str], Awaitable[Any]]


@dataclass
class _Runtime:
    deps: CustomMappingApiDeps | None = None


_runtime = _Runtime()


def register_custom_mapping_routes(app, deps: CustomMappingApiDeps) -> None:
    _runtime.deps = deps
    app.include_router(router)


def _deps() -> CustomMappingApiDeps:
    if _runtime.deps is None:
        raise RuntimeError("Custom mapping API runtime is not configured")
    return _runtime.deps


def _resolve_device(overview: dict[str, Any], requested: Any) -> dict[str, Any]:
    """The overview's context of the requested device, else of the default one."""
    context = overview.get("custom_mapping_context") or {}
    devices = context.get("devices") or []
    base = str(requested).strip() if isinstance(requested, str) else ""
    if requested is not None and not base:
        raise bad_request(ValueError("device must be an audio device name"))
    base = base or context.get("default_device")
    if not base:
        raise bad_request(ValueError("No audio device with input channels is available"))
    device = next((item for item in devices if item["device"]["card_base"] == base), None)
    if device is None:
        raise bad_request(ValueError(f"Unknown audio device or it has no input channels: {base}"))
    return device


def _payload(overview: dict[str, Any], device: dict[str, Any]) -> dict[str, Any]:
    """The device's context, the choice of devices and the program positions."""
    context = overview.get("custom_mapping_context") or {}
    devices = [
        {
            "card_base": item["device"]["card_base"],
            "label": item["device"]["label"],
            "channel_count": len(item["capture"]["channels"]),
            "has_mapping": item["mapping"] is not None,
        }
        for item in context.get("devices") or []
    ]
    return {**device, "devices": devices, "positions": custom_input_mapping.positions_payload()}


@router.get("/api/audio/custom-mapping")
async def get_custom_mapping(device: str | None = None):
    overview = await _deps().read_overview()
    return _payload(overview, _resolve_device(overview, device))


@router.put("/api/audio/custom-mapping")
async def put_custom_mapping(request: Request):
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("not an object")
    except Exception:
        raise HTTPException(
            status_code=400,
            detail='Invalid JSON body, expected {"name": <string?>, "channels": {<channel>: <input number>}}',
        )
    overview = await _deps().read_overview()
    device = _resolve_device(overview, body.get("device"))
    base = device["device"]["card_base"]
    try:
        mapping = custom_input_mapping.validate(
            body.get("name"), body.get("channels"), len(device["capture"]["channels"]))
    except ValueError as exc:
        raise bad_request(exc)
    previous = custom_input_mapping.load_mappings().get(base)

    def restore() -> None:
        if previous is None:
            custom_input_mapping.delete_mapping(base)
        else:
            custom_input_mapping.save_mapping(base, previous)

    try:
        overview = await _deps().change_input_config(
            lambda: custom_input_mapping.save_mapping(base, mapping), restore)
    except ValueError as exc:
        raise bad_request(exc)
    except (OSError, RuntimeError) as exc:
        logger.warning("Custom mapping save failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to save custom mapping: {exc}")
    return _payload(overview, _resolve_device(overview, base))


@router.delete("/api/audio/custom-mapping")
async def delete_custom_mapping(device: str | None = None):
    base = _resolve_device(await _deps().read_overview(), device)["device"]["card_base"]
    previous = custom_input_mapping.load_mappings().get(base)
    if previous is None:
        raise HTTPException(status_code=404, detail="No custom mapping is saved for this audio device")
    key = custom_input_mapping.input_key(base)

    def restore() -> None:
        custom_input_mapping.save_mapping(base, previous)

    try:
        overview = await _deps().change_input_config(
            lambda: custom_input_mapping.delete_mapping(base), restore, deselect_key=key)
    except (OSError, RuntimeError) as exc:
        logger.warning("Custom mapping delete failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to delete custom mapping: {exc}")
    # Outside the source-transition lock: disarming republishes the state.
    await _deps().disarm_auto_select(key)
    return _payload(overview, _resolve_device(overview, base))
