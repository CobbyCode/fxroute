# SPDX-License-Identifier: AGPL-3.0-only

"""System power HTTP surface: logind suspend/power-off routes over audio.power.

Extracted verbatim from main.py (REFACTOR-014). Behavior is identical to
the previous inline implementation. This module owns the three
``/api/system/power*`` route handlers plus the capability-resolution and
error-mapping helpers: capability envelope with the strict-yes conveniences,
the trusted-origin + fresh CanSuspend/CanPowerOff gates before dispatch, and
the denied/unavailable error mapping.

It owns no playback, DSP, measurement or library state. The audio.power
backend is a leaf import; the only application service that cannot be
imported cheaply (the shared trusted-origin predicate owned by main.py) is
injected through :class:`PowerApiDeps` (same *Deps pattern as the extracted
dsp/streaming routers).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable

from fastapi import APIRouter, HTTPException, Request

from audio import power as system_power

logger = logging.getLogger(__name__)

router = APIRouter()


@dataclass(frozen=True)
class PowerApiDeps:
    """Application services injected from main.py."""

    is_origin_trusted: Callable[[Request], bool]


@dataclass
class _PowerApiRuntime:
    deps: PowerApiDeps | None = None


_runtime = _PowerApiRuntime()


def configure_power_api(deps: PowerApiDeps) -> None:
    """Bind the application services used by the route handlers."""
    _runtime.deps = deps


def register_power_routes(app, deps: PowerApiDeps) -> None:
    """Register the /api/system/power* routes on the FastAPI application."""
    configure_power_api(deps)
    app.include_router(router)


def _deps() -> PowerApiDeps:
    if _runtime.deps is None:
        raise RuntimeError("Power API runtime is not configured")
    return _runtime.deps


async def _resolve_system_power_capabilities() -> system_power.PowerCapabilities:
    """Thin alias for :func:`power.get_capabilities` kept for readability.

    All capability translation, the strict-yes gate, the timeout, and the
    ``unavailable`` fall-through live in ``power.py``.  The handler here
    only routes the result into the HTTP envelope.
    """

    return await system_power.get_capabilities()


@router.get("/api/system/power")
async def system_power_capabilities():
    """Report suspend/power-off capability of systemd-logind.

    A 503 is returned only when the dbus-send binary itself is missing
    (then no capability probe can succeed at all); every other failure is
    reported through the textual ``unavailable`` value so the UI keeps
    working even on a host without a running logind.  The body shape --
    including the strict-yes ``suspend_supported`` / ``power_off_supported``
    conveniences -- and the full logind vocabulary are documented on
    :func:`power.is_logind_call_executable`.
    """

    try:
        caps = await _resolve_system_power_capabilities()
    except asyncio.TimeoutError:
        logger.warning("system power capabilities timed out")
        raise HTTPException(
            status_code=503,
            detail="systemd-logind not reachable via dbus",
        )

    return {
        "available": caps.available,
        "suspend": caps.suspend,
        "power_off": caps.power_off,
        "suspend_supported": system_power.is_logind_call_executable(caps.suspend),
        "power_off_supported": system_power.is_logind_call_executable(caps.power_off),
        "unavailable_reason": caps.unavailable_reason,
    }


def _system_power_error_to_http(result: system_power.PowerCallResult):
    """Map a failed :class:`PowerCallResult` to the right HTTP code.

    * ``"denied"``     -> 403 (polkit refused; the user-facing message is
      carried by the body).
    * ``"unavailable"`` -> 503 (dbus-send or login1 missing).
    """

    if result.status == "denied":
        return HTTPException(status_code=403, detail=result.error or "denied")
    return HTTPException(status_code=503, detail=result.error or "unavailable")


@router.post("/api/system/power/suspend")
async def system_power_suspend(request: Request):
    """Trigger ``Manager.Suspend`` via systemd-logind.

    Returns ``200 OK`` when the suspend request was dispatched
    successfully.  The HTTP response must be sent before the system
    actually suspends; the frontend uses this signal to switch the
    connection badge into the ``"Suspending…"`` state.

    Two gates run before the action:

    * the injected trusted-origin predicate rejects cross-site POSTs so a
      foreign web page in the user's browser cannot trivially shut the
      host down.  This is the standard CSRF mitigation when the
      application deliberately has no session cookies.
    * :func:`system_power.is_now_supported` re-probes ``CanSuspend`` so
      a stale UI snapshot, an inhibitor lock that engaged after the
      page loaded, or a CLI caller hitting the endpoint directly will
      all fail cleanly with HTTP 409 if logind now reports anything
      other than ``"yes"``.  This prevents FXRoute from dispatching the
      action under a different capability than the one the menu
      advertises -- which would either touch auth or inhibitor blocks
      the polkit rule does not cover, i.e. exactly the "additional
      privilege" we must not acquire.
    """

    if not _deps().is_origin_trusted(request):
        raise HTTPException(
            status_code=403,
            detail="Cross-site request rejected: open FXRoute on this host before using the power menu.",
        )
    supported, raw = await system_power.is_now_supported("suspend")
    if not supported:
        raise HTTPException(
            status_code=409,
            detail=f"Suspend is not directly executable right now (logind CanSuspend={raw!r}).",
        )
    result = await system_power.request_suspend()
    if result.ok:
        return {"ok": True, "status": "suspended", "action": result.action}
    raise _system_power_error_to_http(result)


@router.post("/api/system/power/power-off")
async def system_power_power_off(request: Request):
    """Trigger ``Manager.PowerOff`` via systemd-logind.

    Returns ``200 OK`` when the shutdown request was dispatched.
    Like :func:`system_power_suspend`, this responds before logind has
    actually powered the machine off; the frontend shows
    ``"Shutting down…"`` until the websocket drops.

    The same two gates apply: cross-origin rejection through
    the injected trusted-origin predicate, plus a fresh ``CanPowerOff``
    re-probe through :func:`system_power.is_now_supported` so any
    non-``"yes"`` logind answer (``"challenge"``, ``"inhibited"`` ...)
    fails cleanly with HTTP 409 instead of bypassing the polkit rule.
    """

    if not _deps().is_origin_trusted(request):
        raise HTTPException(
            status_code=403,
            detail="Cross-site request rejected: open FXRoute on this host before using the power menu.",
        )
    supported, raw = await system_power.is_now_supported("power_off")
    if not supported:
        raise HTTPException(
            status_code=409,
            detail=f"Shutdown is not directly executable right now (logind CanPowerOff={raw!r}).",
        )
    result = await system_power.request_power_off()
    if result.ok:
        return {"ok": True, "status": "shutting_down", "action": result.action}
    raise _system_power_error_to_http(result)
