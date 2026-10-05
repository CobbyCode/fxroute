# SPDX-License-Identifier: AGPL-3.0-only

"""HTTP surface of the per-device custom input mapping.

The selected device, its capture channels and its mapping come from the
source overview (``custom_mapping_context``), so the dialog and the input
list always describe the same device. Writes run through
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


def _payload(overview: dict[str, Any]) -> dict[str, Any]:
    context = dict(overview.get("custom_mapping_context") or {})
    return {**context, "positions": custom_input_mapping.positions_payload()}


def _device_base(context: dict[str, Any]) -> str:
    base = (context.get("device") or {}).get("card_base")
    if not base:
        raise bad_request(ValueError("No audio output device is selected"))
    return base


@router.get("/api/audio/custom-mapping")
async def get_custom_mapping():
    return _payload(await _deps().read_overview())


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
    context = (await _deps().read_overview()).get("custom_mapping_context") or {}
    base = _device_base(context)
    capture = context.get("capture")
    if not capture:
        raise bad_request(ValueError("The selected audio device has no input channels"))
    try:
        mapping = custom_input_mapping.validate(body.get("name"), body.get("channels"), len(capture["channels"]))
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
    return _payload(overview)


@router.delete("/api/audio/custom-mapping")
async def delete_custom_mapping():
    context = (await _deps().read_overview()).get("custom_mapping_context") or {}
    base = _device_base(context)
    previous = custom_input_mapping.load_mappings().get(base)
    if previous is None:
        raise HTTPException(status_code=404, detail="No custom mapping is saved for the selected audio device")
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
    return _payload(overview)
