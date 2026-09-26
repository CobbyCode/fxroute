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
Every job also ends with the runtime back on the head's normal plan at the
measurement rate, verified active by readback, so the next sweep of the
still open window matches it; a failed restore or verification fails the
job instead of reporting success (the AutoSub restore-or-fail-job contract).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from dsp.runtime import DSPRuntimeConfig, PlannedSyncTarget
from common.run_to_completion import run_to_completion
from measurement.speaker_acquisition import acquire_speaker_captures, verify_speaker_alignment
from measurement.speaker_align import SpeakerAlignment
from measurement.speaker_commit import SpeakerAlignSession
from measurement.speaker_service import (
    SpeakerAlignBusyError,
    SpeakerAlignService,
    SpeakerAlignStaleError,
    SpeakerAlignTeardownError,
)
from measurement.target import freeze_measurement_target

router = APIRouter()
logger = logging.getLogger(__name__)


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
            reference_input_channel_left=body.get("reference_input_channel_left"),
            reference_input_channel_right=body.get("reference_input_channel_right"),
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
    verification_runner: Callable[..., Awaitable[dict]] | None = None,
    get_measurement_session: Callable[[], Any] | None = None,
    build_release_adapter: Callable[..., Any] | None = None,
    prepare_measurement: Callable[..., Awaitable[Any]] | None = None,
    another_measurement_active: Callable[[], bool] | None = None,
    input_keeper: Callable[[str, dict], Any] | None = None,
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
    verifier = verification_runner or verify_speaker_alignment
    if measurement_store is None and (runner is acquire_speaker_captures
                                      or verifier is verify_speaker_alignment):
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
    if input_keeper is not None and not callable(input_keeper):
        raise ValueError("Speaker Align composition requires a callable input_keeper")

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
                      reference_input_channel_left: str | int | None = None,
                      reference_input_channel_right: str | int | None = None,
                      reference_id: str, microphone_position_id: str,
                      sweep_profile: dict | None,
                      cancel_requested: Callable[[], bool] | None = None,
                      expected_native_context: dict | None = None,
                      on_progress: Callable | None = None) -> dict:
        return await runner(
            measurement_store, alignment, input_id=input_id,
            mic_input_channel=mic_input_channel,
            reference_input_channel=reference_input_channel,
            reference_input_channel_left=reference_input_channel_left,
            reference_input_channel_right=reference_input_channel_right,
            reference_id=reference_id, microphone_position_id=microphone_position_id,
            sweep_profile=sweep_profile, cancel_requested=cancel_requested,
            expected_native_context=expected_native_context, on_progress=on_progress)

    async def confirm(alignment: SpeakerAlignment, *, input_id: str,
                      mic_input_channel: str | int | None,
                      reference_input_channel: str | int | None,
                      reference_input_channel_left: str | int | None = None,
                      reference_input_channel_right: str | int | None = None,
                      reference_id: str, microphone_position_id: str,
                      sweep_profile: dict | None,
                      cancel_requested: Callable[[], bool] | None = None,
                      expected_native_context: dict | None = None,
                      on_progress: Callable | None = None) -> dict:
        """One shared verification take per side plus its confirmation document."""
        return await verifier(
            measurement_store, alignment, input_id=input_id,
            mic_input_channel=mic_input_channel,
            reference_input_channel=reference_input_channel,
            reference_input_channel_left=reference_input_channel_left,
            reference_input_channel_right=reference_input_channel_right,
            reference_id=reference_id, microphone_position_id=microphone_position_id,
            sweep_profile=sweep_profile, cancel_requested=cancel_requested,
            expected_native_context=expected_native_context, on_progress=on_progress)

    def check_available() -> None:
        session = get_measurement_session() if get_measurement_session else None
        if ((session is not None and session.has_active_jobs)
                or (another_measurement_active and another_measurement_active())
                or (measurement_store is not None and measurement_store.has_active_measurement_job())):
            raise SpeakerAlignBusyError("Another measurement is already running")

    async def restore_measurement_plan() -> dict[str, Any]:
        """Put the runtime back on the head's normal plan at the measurement rate.

        A run stages the neutralized alignment plan (Global/area banks
        bypassed) and keeps it after its own commit or restore. The session
        release rebuilds the normal plan only once the measurement window
        closes, so a normal sweep in the still open window would compile the
        full plan and refuse the runtime as a fingerprint mismatch. Raises on
        failure: the teardown turns a failed restore into a failed job (the
        AutoSub restore-or-fail-job contract) instead of reporting success
        with a dirty runtime.
        """
        if build_release_adapter is None:
            raise SpeakerAlignTeardownError("Speaker Align plan restore is not wired")
        device = describe_device(output_service.load())
        rate = int(get_measurement_rate())
        adapter = build_release_adapter(
            output_key=device["output_key"], channels=device["channels"])
        rendered = await adapter(rate)
        logger.info("Speaker Align restored the measurement plan: %s", rendered)
        return rendered

    async def verify_measurement_plan(rendered: dict[str, Any]) -> None:
        """Require the restored normal plan to be actually active.

        The adapter rendered the head's normal plan at the measurement rate;
        the runtime must now show that plan fingerprint at that rate. Raises
        on any deviation, like the pre-commit readback gate.
        """
        native_runtime = get_native_runtime()
        if native_runtime is None:
            raise SpeakerAlignTeardownError("Native DSP runtime is unavailable for plan verification")
        snapshot = native_runtime.snapshot()
        config = snapshot.get("config") if isinstance(snapshot, dict) else None
        expected = rendered.get("plan_fingerprint") if isinstance(rendered, dict) else None
        rate = int(rendered.get("sample_rate_hz") or 0) if isinstance(rendered, dict) else 0
        if (not isinstance(config, dict) or snapshot.get("active") is not True
                or not expected or config.get("plan_fingerprint") != expected
                or config.get("sample_rate") != rate):
            raise SpeakerAlignTeardownError(
                "restored plan is not active: "
                f"fingerprint={config.get('plan_fingerprint') if isinstance(config, dict) else None!r} "
                f"expected={expected!r}")
        logger.info("Speaker Align verified the measurement plan: %s", expected)

    async def restore_and_verify_measurement_plan() -> None:
        await verify_measurement_plan(await restore_measurement_plan())

    def job_scope(job_id: str):
        session = get_measurement_session() if get_measurement_session else None
        epoch = session.capture_entry_epoch() if session is not None else None

        @asynccontextmanager
        async def owned():
            registered = False
            try:
                if session is not None:
                    _, entered = await session.register_speaker_job(job_id, entry_epoch=epoch)
                    registered = True
                    if prepare_measurement is not None:
                        await prepare_measurement(int(get_measurement_rate()), graph_already_verified=entered)
                yield
            finally:
                if registered:
                    # Still owned by this job: no sweep can start before the
                    # normal plan is back and verified, and the job reads
                    # terminal only after. Both steps run to completion like
                    # the AutoSub teardown; a failed restore/verify fails the
                    # job through SpeakerAlignTeardownError, while a failing
                    # unregister is logged and never overwrites the outcome.
                    teardown_error: Exception | None = None
                    try:
                        await run_to_completion(restore_and_verify_measurement_plan())
                    except Exception as exc:
                        teardown_error = exc
                    try:
                        await run_to_completion(session.unregister_speaker_job(job_id))
                    except Exception:
                        logger.exception(
                            "Speaker Align job=%s session unregister failed", job_id)
                    if teardown_error is not None:
                        raise SpeakerAlignTeardownError(
                            str(teardown_error) or type(teardown_error).__name__
                        ) from teardown_error

        return owned()

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
        get_state=get_state, describe=describe, acquire=acquire, confirm=confirm,
        create_session=create_session, freeze_live=freeze_live,
        on_committed=on_committed, job_scope=job_scope, check_available=check_available,
        input_keeper=input_keeper)


__all__ = [
    "SpeakerAlignApiDeps",
    "build_speaker_align_service",
    "configure_speaker_align",
    "router",
]
