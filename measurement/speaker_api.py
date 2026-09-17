# SPDX-License-Identifier: AGPL-3.0-only
"""Speaker Align HTTP API: start, status and cancel over an injected service.

Thin transport only: request validation stays in
``measurement.speaker_service`` (one validation owner — the handlers pass
fields through and map the service's typed outcomes to status codes), and
production boundaries are composed by ``build_speaker_align_service`` from
injected parts so this module never imports ``main``. Committed jobs
register a release adapter on the measurement session so a later session
release rebuilds the committed plan at the restore rate; without release
wiring the persisted head still keeps the commit for the next transition.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from dsp.runtime import DSPRuntimeConfig, PlannedSyncTarget
from measurement.speaker_acquisition import acquire_speaker_captures
from measurement.speaker_align import SpeakerAlignment
from measurement.speaker_commit import SpeakerAlignSession
from measurement.speaker_service import (
    SpeakerAlignBusyError,
    SpeakerAlignService,
    SpeakerAlignStaleError,
)
from measurement.target import freeze_measurement_target

router = APIRouter()


@dataclass(frozen=True)
class SpeakerAlignApiDeps:
    """Service accessor injected by the composition root (``main.py``)."""

    get_service: Callable[[], SpeakerAlignService]


_dependencies: SpeakerAlignApiDeps | None = None


def configure_speaker_align(get_service: Callable[[], Any]) -> None:
    """Bind (or, with a ``None``-returning accessor, release) the service."""
    global _dependencies
    _dependencies = SpeakerAlignApiDeps(get_service=get_service)


def _service() -> SpeakerAlignService:
    if _dependencies is None:
        raise HTTPException(status_code=503, detail="Speaker alignment is unavailable")
    service = _dependencies.get_service()
    if service is None:
        raise HTTPException(status_code=503, detail="Speaker alignment is unavailable")
    return service


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, SpeakerAlignStaleError):
        return HTTPException(status_code=409, detail={
            "code": "speaker-align-stale", "message": str(exc) or "stale"})
    if isinstance(exc, SpeakerAlignBusyError):
        return HTTPException(status_code=409, detail={
            "code": "speaker-align-busy", "message": str(exc) or "busy"})
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail={
            "code": "speaker-align-invalid", "message": str(exc) or "invalid"})
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail={
            "code": "speaker-align-unknown-job", "message": "Unknown speaker alignment job"})
    raise exc


@router.post("/api/speaker-align/start")
async def start_speaker_alignment(request: Request):
    """Validate and launch one alignment job; return its initial record."""
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail={
            "code": "speaker-align-invalid", "message": f"Request body must be JSON: {exc}"})
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail={
            "code": "speaker-align-invalid", "message": "Request body must be an object"})
    dry_run = body.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise HTTPException(status_code=400, detail={
            "code": "speaker-align-invalid", "message": "dry_run must be a boolean"})
    service = _service()
    try:
        job_id = service.start(
            body.get("side"),
            input_id=body.get("input_id", ""),
            mic_input_channel=body.get("mic_input_channel", "1"),
            reference_input_channel=body.get("reference_input_channel", ""),
            reference_id=body.get("reference_id", ""),
            microphone_position_id=body.get("microphone_position_id", ""),
            sweep_profile=body.get("sweep_profile"),
            dry_run=dry_run)
    except Exception as exc:
        raise _error(exc)
    return {"status": "ok", "job": service.status(job_id)}


@router.get("/api/speaker-align/jobs")
async def list_speaker_alignments():
    """List every known alignment job record, newest last."""
    return {"status": "ok", "jobs": _service().jobs()}


@router.get("/api/speaker-align/jobs/{job_id}")
async def get_speaker_alignment(job_id: str):
    """Return one alignment job record; unknown ids are 404."""
    try:
        return {"status": "ok", "job": _service().status(job_id)}
    except Exception as exc:
        raise _error(exc)


@router.post("/api/speaker-align/jobs/{job_id}/cancel")
async def cancel_speaker_alignment(job_id: str):
    """Request cancellation; terminal jobs are returned unchanged."""
    try:
        return {"status": "ok", "job": _service().cancel(job_id)}
    except Exception as exc:
        raise _error(exc)


def build_speaker_align_service(
    *,
    output_service: Any,
    measurement_store: Any,
    dsp_manager: Any,
    get_native_runtime: Callable[[], Any],
    describe_device: Callable[[dict], dict],
    get_measurement_rate: Callable[[], int],
    capture_runner: Callable[..., Awaitable[dict]] | None = None,
    get_measurement_session: Callable[[], Any] | None = None,
    build_release_adapter: Callable[..., Any] | None = None,
) -> SpeakerAlignService:
    """Compose a service from application parts (no ``main`` import).

    ``describe_device(state)`` returns ``output_key``, ``channels`` and
    ``hardware_ports`` for the selected output;
    ``get_measurement_rate()`` resolves the sweep rate per job. All other
    parts are used directly: the authoritative output service, the
    measurement store, the DSP manager (engine text), and a native-runtime
    accessor pinned per session with ownership checks. A ``None``
    measurement store is only accepted together with a custom
    ``capture_runner`` (test seam); the default runner requires the store.

    Release wiring is optional and paired: ``get_measurement_session`` and
    ``build_release_adapter`` must be given together. The committed hook
    then registers one release adapter per committed job so a later
    measurement-session release rebuilds the committed plan. Without both,
    the legacy release path stays byte-identical.
    """
    if output_service is None or dsp_manager is None:
        raise ValueError("Speaker Align composition requires output and DSP services")
    runner = capture_runner or acquire_speaker_captures
    if measurement_store is None and runner is acquire_speaker_captures:
        raise ValueError("Speaker Align composition requires a measurement store")
    for name, bound in (("get_native_runtime", get_native_runtime),
                        ("describe_device", describe_device),
                        ("get_measurement_rate", get_measurement_rate)):
        if not callable(bound):
            raise ValueError(f"Speaker Align composition requires a callable {name}")
    if (get_measurement_session is None) != (build_release_adapter is None):
        raise ValueError("Speaker Align release wiring requires both session and factory")
    if get_measurement_session is not None and not callable(get_measurement_session):
        raise ValueError("Speaker Align composition requires a callable get_measurement_session")
    if build_release_adapter is not None and not callable(build_release_adapter):
        raise ValueError("Speaker Align composition requires a callable build_release_adapter")

    def get_state() -> dict:
        return output_service.load()

    def describe(state: dict) -> dict:
        device = describe_device(state)
        rate = int(get_measurement_rate())
        if rate <= 0:
            raise ValueError("Speaker Align measurement rate must be positive")
        fingerprint = output_service.fingerprint(
            state, output_key=device["output_key"], channels=device["channels"],
            sample_rate_hz=rate)
        return {"output_key": device["output_key"], "channels": device["channels"],
                "sample_rate_hz": rate, "fingerprint": fingerprint}

    def freeze_live(state: dict, *, output_key: str, channels: int,
                    sample_rate_hz: int, fingerprint: str) -> dict:
        return freeze_measurement_target(
            state, bank_id="global", output_key=output_key, channels=channels,
            sample_rate_hz=sample_rate_hz, fingerprint=fingerprint)

    async def acquire(alignment: SpeakerAlignment, *, input_id: str,
                      mic_input_channel: str | int | None,
                      reference_input_channel: str | int | None,
                      reference_id: str, microphone_position_id: str,
                      sweep_profile: dict | None,
                      cancel_requested: Callable[[], bool] | None = None) -> dict:
        return await runner(
            measurement_store, alignment, input_id=input_id,
            mic_input_channel=mic_input_channel,
            reference_input_channel=reference_input_channel,
            reference_id=reference_id, microphone_position_id=microphone_position_id,
            sweep_profile=sweep_profile, cancel_requested=cancel_requested)

    def create_session(start_state: dict, *, output_key: str, channels: int,
                       sample_rate_hz: int) -> SpeakerAlignSession:
        native_runtime = get_native_runtime()
        if native_runtime is None:
            raise RuntimeError("Native DSP runtime is unavailable")
        ports = list(describe_device(start_state).get("hardware_ports") or [])
        if len(ports) < channels:
            raise ValueError("Speaker Align output has insufficient discovered playback ports")

        def require_runtime() -> None:
            if get_native_runtime() is not native_runtime:
                raise RuntimeError("Speaker Align native runtime ownership changed")

        def build_target(plan: dict, *, fingerprint: str) -> PlannedSyncTarget:
            require_runtime()
            layout = output_service.compile_layout(plan)
            config = DSPRuntimeConfig.from_plan(
                plan, layout=layout, output_key=output_key, sample_rate_hz=sample_rate_hz,
                hardware_ports=list(ports), plan_fingerprint=fingerprint)
            text = dsp_manager.compile_engine_text(
                [dict(entry) for entry in layout], preset_name=plan["global"]["preset"],
                sample_rate_hz=sample_rate_hz, extras_override=plan["global"]["extras"])
            return PlannedSyncTarget(config=config, text=text)

        async def guarded_stage(*args: Any, **kwargs: Any) -> None:
            require_runtime()
            await native_runtime.guarded_rebuild_rendered(*args, **kwargs)
            require_runtime()

        async def readback() -> dict:
            require_runtime()
            before = native_runtime.snapshot()
            links_valid = await native_runtime.verify()
            require_runtime()
            after = native_runtime.snapshot()
            config = after.get("config") or {}
            same_graph = (before.get("helper_pid") == after.get("helper_pid")
                          and before.get("config") == config)
            expected_device = (config.get("output_key") == output_key
                               and config.get("hardware_ports") == ports)
            if (not links_valid or not same_graph or not expected_device
                    or before.get("active") is not True or after.get("active") is not True
                    or not after.get("helper_pid")):
                raise RuntimeError(
                    "Speaker Align runtime links, device or process identity "
                    "could not be verified")
            return after

        return SpeakerAlignSession(
            service=output_service, start_state=start_state, output_key=output_key,
            channels=channels, sample_rate_hz=sample_rate_hz, build_target=build_target,
            guarded_stage=guarded_stage, readback=readback)

    on_committed = None
    if get_measurement_session is not None and build_release_adapter is not None:
        async def on_committed(context: dict) -> None:
            session = get_measurement_session()
            if session is None:
                return
            adapter = build_release_adapter(
                output_key=context["output_key"], channels=context["channels"])
            await session.register_speaker_align_release_adapter(adapter)

    return SpeakerAlignService(
        get_state=get_state, describe=describe, acquire=acquire,
        create_session=create_session, freeze_live=freeze_live,
        on_committed=on_committed)


__all__ = [
    "SpeakerAlignApiDeps",
    "build_speaker_align_service",
    "configure_speaker_align",
    "router",
]
