# SPDX-License-Identifier: AGPL-3.0-only
"""Speaker Align evidence transport: serial way takes and one shared verification take.

``acquire_speaker_captures`` runs the side's one shared *planning* take first —
every way of one side at once, the rest of the plan muted, so the ways share one
capture time base — and then drives one ``CaptureEvidence`` owner per frozen
``SpeakerAlignment`` request, strictly serially. The planning document built
from the shared take is what ``propose`` reads the relative way delays from; the
per-way takes stay the evidence for every way's own level and reference quality.
``verify_speaker_alignment`` runs the same kind of shared take as the
verification and returns the confirmation document built from its band-limited
arrivals. Neither stages, applies or commits anything. Both shared takes also
return the store's normal measurement of that take (traces and IR preview), so
the frontend can show Before and After like any other sweep.

Each way records the configured electrical-reference candidate channels in a
single take; the store's electrical-reference evaluation then admits the
channel that actually carries that way, and the admitted channel is kept in
the capture evidence and provenance. Attestation limits, stated plainly:
software can prove that every take was captured through the same observed
microphone input, the same configured reference candidate set and the same
reference node at the same rate, with the same frozen target the alignment was
planned from. It cannot prove physical wiring or that the microphone stayed
fixed, and the admitted loopback channel may legitimately differ between ways.
The caller supplies the session's upstream ``reference_id`` and
``microphone_position_id`` as the operator's assertion; the adapter binds those
IDs to the observed common input and rejects any change between takes. Only
stable electrical-reference evidence is admitted; fallback, tolerated and
acoustic-only timing fail fast before the next take.
Quality, confidence, overlap and staleness-at-proposal-time remain
``SpeakerAlignment.propose`` and ``SpeakerAlignment.confirmation`` gates; this
adapter is acquisition, not authorization.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from common.run_to_completion import restore_after
from measurement.capture_evidence import CaptureEvidence
from measurement.speaker_align import require_timing_reference
from measurement.target import REFERENCE_TAP_INGRESS


def _session_identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"Speaker Align acquisition requires a {label} identity")
    return value


def _reference_candidate_channels(*values: str | int | None) -> list[str]:
    """List the distinct configured loopback channels that may carry the sweep.

    Every candidate is recorded in the same capture and the store's
    electrical-reference evaluation decides which channel actually carries the
    way.  Nothing here maps a side, a way or an output role to a specific
    input channel.
    """
    candidates: list[str] = []
    for value in values:
        token = str(value).strip() if value is not None else ""
        if token and token not in candidates:
            candidates.append(token)
    return candidates


def _job_input_key(job: dict[str, Any], role: str) -> tuple:
    """Identify the capture input chain a job was started with.

    Compared before each way's sweep so a changed microphone or reference
    input fails before spending another capture, not after it. Fail closed:
    an incomplete identity never compares equal to anything, not even itself.
    """
    input_info = job.get("input") if isinstance(job.get("input"), dict) else {}
    channels = job.get("input_channels") if isinstance(job.get("input_channels"), dict) else {}
    candidate_channels = channels.get("electrical_reference_candidates")
    key = (
        input_info.get("node_name"),
        input_info.get("node_serial"),
        channels.get("mic"),
        (
            tuple(candidate_channels)
            if isinstance(candidate_channels, (list, tuple)) and candidate_channels
            else channels.get("electrical_reference")
        ),
        input_info.get("measurement_sample_rate") or input_info.get("sample_rate"),
    )
    # A missing electrical reference is legitimate (acoustic-only path) and is
    # rejected later at the reference gate with its accurate status; every
    # other part of the identity is required for attestation.
    required = (key[0], key[1], key[2], key[4])
    if any(part is None or part == "" for part in required):
        raise ValueError(
            f"Speaker Align capture input is incomplete for {role}; "
            "microphone and reference input identity are required for every way"
        )
    return key


def input_chain_key(provenance: object) -> tuple:
    """The observed input chain every take of one alignment has to share.

    The admitted loopback channel is deliberately excluded: a two-way speaker
    can carry its low way on one loopback half and its high way on the other,
    so only the microphone, the configured reference candidates, the reference
    node, the rate and the microphone calibration (every level is read through
    it) have to stay identical between takes.
    """
    if not isinstance(provenance, dict):
        raise ValueError("Speaker Align requires observed capture provenance")
    candidates = provenance.get("electrical_reference_candidates")
    return (
        provenance.get("microphone_node"),
        provenance.get("microphone_serial"),
        provenance.get("mic_channel"),
        tuple(candidates) if isinstance(candidates, (list, tuple)) else candidates,
        provenance.get("reference_node"),
        provenance.get("sample_rate_hz"),
        provenance.get("microphone_calibration"),
    )


def _observed_input_chain(
    *,
    role: str,
    capture_info: dict[str, Any],
    finished_input: dict[str, Any],
    analysis: dict[str, Any],
    reference_candidate_channels: list[str],
    electrical_requested: bool,
    microphone_calibration: str,
) -> dict[str, Any]:
    """Observed input chain of one take, complete or loudly absent.

    Fail closed: two takes both missing a field must never attest sameness of
    nothing.
    """
    observed_candidate_channels = capture_info.get("electrical_reference_candidate_channels")
    if not observed_candidate_channels:
        observed_candidate_channels = list(reference_candidate_channels) or None
    elif isinstance(observed_candidate_channels, list):
        observed_candidate_channels = tuple(observed_candidate_channels)
    observed = {
        "microphone_node": capture_info.get("microphone_node"),
        "microphone_serial": finished_input.get("node_serial"),
        "mic_channel": capture_info.get("mic_input_channel"),
        "electrical_reference_candidates": observed_candidate_channels,
        "electrical_reference_channel": capture_info.get("electrical_reference_input_channel"),
        "reference_node": capture_info.get("reference_node"),
        "sample_rate_hz": analysis.get("sample_rate"),
        "microphone_calibration": microphone_calibration,
    }
    missing = [name for name, value in observed.items() if value is None
               and (name not in ("electrical_reference_channel", "electrical_reference_candidates")
                    or electrical_requested)]
    if missing:
        raise RuntimeError(
            f"Speaker Align capture for {role} is missing {', '.join(missing)}; "
            "refusing to attest an incomplete input chain"
        )
    return observed


def _applied_calibration(finished: dict[str, Any]) -> str:
    """The microphone calibration file a take's levels were read through, or ``""``."""
    calibration = finished.get("calibration") if isinstance(finished.get("calibration"), dict) else {}
    return str(calibration.get("path") or "") if calibration.get("applied") is True else ""


def _require_unboosted_levels(capture_info: dict[str, Any], *, role: str) -> None:
    """Refuse a way take whose microphone gain was raised after an earlier way take.

    The store raises a quiet microphone's input volume once and keeps it
    raised. The gain proposal compares the ways' absolute levels across their
    takes, so a raise between two way takes would read as a level difference
    of the ways themselves.
    """
    if capture_info.get("mic_auto_boosted") is True:
        raise RuntimeError(
            f"Speaker Align microphone input volume was raised while capturing {role}; "
            "the way levels are no longer comparable, start Speaker Align again"
        )


async def _await_measurement_job(store: Any, job_id: str, *, label: str) -> dict[str, Any]:
    """Await one registered capture job, drain it and return its finished record.

    Same-package internal boundary: the store returns no public handle for
    awaiting a job without cancelling it (``drain_job`` cancels live jobs), so
    the runner task is awaited directly and drained afterwards for cleanup. A
    caller cancellation absorbed by the runner task is restored as the caller's
    own cancellation contract instead of being reported as a failure. A drain
    that fails on the way out is noted on that cancellation, never raised in
    its place.
    """
    task = store._job_tasks[job_id]
    try:
        await task
    except asyncio.CancelledError as cancelled:
        await restore_after(cancelled, store.drain_job(job_id), what="Draining the capture job")
        raise
    current = asyncio.current_task()
    if current is not None and current.cancelling():
        cancelled = asyncio.CancelledError(f"{label} was cancelled")
        await restore_after(cancelled, store.drain_job(job_id), what="Draining the capture job")
        raise cancelled
    await store.drain_job(job_id)
    finished = store.get_job(job_id)
    if finished.get("status") != "completed":
        error = finished.get("error") if isinstance(finished.get("error"), dict) else {}
        detail = error.get("detail") or finished.get("message") or finished.get("status")
        raise RuntimeError(f"{label} did not complete: {detail}")
    return finished


def _admit_reference(analysis: dict[str, Any], reference_node: object, *, electrical_requested: bool,
                     label: str) -> None:
    """Admit the take's reference or fail loudly with its accurate status."""
    try:
        require_timing_reference(analysis, reference_node)
        if electrical_requested and not (analysis.get("reference_path") or {}).get("electrical_reference_used"):
            raise ValueError("Selected electrical reference was not captured")
    except ValueError as exc:
        raise RuntimeError(f"{label} reference failed (electrical reference or ingress monitor): {exc}") from exc


async def _run_side_take(
    store: Any,
    request: dict[str, Any],
    *,
    label: str,
    reference_id: str,
    microphone_position_id: str,
    input_id: str,
    mic_input_channel: str | int | None,
    reference_input_channel: str | int | None,
    reference_input_channel_left: str | int | None,
    reference_input_channel_right: str | int | None,
    reference_candidate_channels: list[str],
    sweep_profile: dict[str, float] | None,
    cancel_requested: Callable[[], bool] | None,
    expected_native_context: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], tuple, dict[str, Any] | None]:
    """Run one side-wide take; return evidence, observed chain, input key, measurement.

    Every way of one side plays the same sweep at once, the rest of the plan
    muted, so the side's ways share one capture time base. The frozen target,
    the mute mask and the capture input identity are all checked before the
    worker's first sweep, so a stale request or a changed input costs no
    capture. The returned key is the pre-sweep identity the caller attests the
    side's per-way captures against. The measurement is the store's normal
    result of the take, or ``None`` when the job published none.
    """
    if request.get("reference_id") != reference_id:
        raise ValueError(f"Speaker Align {label} reference_id differs from the frozen requests")
    if request.get("microphone_position_id") != microphone_position_id:
        raise ValueError(
            f"Speaker Align {label} microphone_position_id differs from the frozen requests")
    if request.get("reference_tap") != REFERENCE_TAP_INGRESS:
        raise ValueError(f"Speaker Align {label} requires the ingress reference tap")
    if cancel_requested is not None and cancel_requested():
        raise asyncio.CancelledError(f"Speaker Align {label} was cancelled")
    electrical_requested = bool(reference_candidate_channels)
    side = str(request["side"])
    owner = CaptureEvidence()
    job = await store.start_measurement(
        input_id=input_id,
        mic_input_channel=mic_input_channel,
        reference_input_channel=reference_input_channel,
        reference_input_channel_left=reference_input_channel_left,
        reference_input_channel_right=reference_input_channel_right,
        reference_candidate_channels=reference_candidate_channels,
        channel=request["channel"],
        sweep_profile=dict(sweep_profile) if sweep_profile else None,
        frozen_target=request["measurement_target"],
        capture_evidence=owner,
        **(expected_native_context or {}),
    )
    job_id = str(job["id"])
    try:
        if job.get("measurement_target") != request["measurement_target"]:
            raise ValueError(
                f"Speaker Align {label} target is stale; revision, device and "
                "processing must match the frozen requests"
            )
        if job.get("output_mask") != request["output_mask"]:
            raise ValueError(
                f"Speaker Align {label} must play exactly this side's ways; "
                "the resolved mute mask differs from the frozen request"
            )
        # A shared side take is registered with its already narrowed target, so
        # the store resolves no target for it and cannot notice one that moved.
        # Compare against the live target the store would have resolved: a
        # rebased output state fails the take before its sweep, never after.
        expected_target = request["measurement_target"]
        provider = getattr(store, "measurement_target_provider", None)
        if callable(provider):
            live_target = provider(str(expected_target.get("bank_id") or ""),
                                   int(expected_target.get("sample_rate_hz") or 0))
            if (not isinstance(live_target, dict)
                    or live_target.get("revision") != expected_target.get("revision")
                    or live_target.get("processing_fingerprint")
                    != expected_target.get("processing_fingerprint")):
                raise ValueError(
                    f"Speaker Align {label} target is stale; revision, device and "
                    "processing must match the frozen requests"
                )
        input_key = _job_input_key(job, f"{side} ways")
    except BaseException as error:
        await restore_after(error, store.drain_job(job_id), what="Draining the capture job")
        raise
    finished = await _await_measurement_job(
        store, job_id, label=f"Speaker Align {label} capture")
    evidence = owner.take()
    if evidence.get("time_reference") != "deconvolved-sweep-origin":
        raise RuntimeError(f"Speaker Align {label} lost its deconvolved sweep origin")
    analysis = evidence["analysis"]
    if analysis.get("sample_rate") != request["measurement_target"].get("sample_rate_hz"):
        raise RuntimeError(f"Speaker Align {label} capture sample rate changed")
    capture_info = evidence.get("capture") if isinstance(evidence.get("capture"), dict) else {}
    _admit_reference(analysis, capture_info.get("reference_node"),
                     electrical_requested=electrical_requested,
                     label=f"Speaker Align {label}")
    finished_input = finished.get("input") if isinstance(finished.get("input"), dict) else {}
    observed = _observed_input_chain(
        role=f"{side} ways", capture_info=capture_info, finished_input=finished_input,
        analysis=analysis, reference_candidate_channels=reference_candidate_channels,
        electrical_requested=electrical_requested,
        microphone_calibration=_applied_calibration(finished))
    take = {
        "measurement_target": evidence["measurement_target"],
        "reference_id": reference_id,
        "microphone_position_id": microphone_position_id,
        "reference_tap": REFERENCE_TAP_INGRESS,
        "reference_node": observed["reference_node"],
        "time_reference": evidence["time_reference"],
        "impulse_response": evidence["impulse_response"],
        "analysis": analysis,
        "calibration_curve": evidence.get("calibration_curve"),
    }
    result = finished.get("result") if isinstance(finished.get("result"), dict) else {}
    measurement = result.get("measurement") if isinstance(result.get("measurement"), dict) else None
    return take, {**observed, "job_id": job_id}, input_key, measurement


async def acquire_speaker_captures(
    store: Any,
    alignment: Any,
    *,
    input_id: str,
    mic_input_channel: str | int | None = "1",
    reference_input_channel: str | int | None,
    reference_input_channel_left: str | int | None = None,
    reference_input_channel_right: str | int | None = None,
    reference_id: str,
    microphone_position_id: str,
    sweep_profile: dict[str, float] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
    expected_native_context: dict[str, Any] | None = None,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, Any]:
    """Capture the side's shared planning take and every way serially.

    Returns ``{"captures": [...], "planning": {...}, "provenance": {...},
    "measurement": {...} | None}``.
    ``planning`` is the shared planning take's document: one take in which
    every way of the side played at once, giving each way's arrival on one
    capture time base -- that is where ``propose`` reads the relative delays
    from.  The per-way captures stay the evidence for the ways' levels, their
    reference quality and the observed input chain. ``measurement`` is the
    planning take's normal measurement (the Before view).
    """
    reference_id = _session_identity(reference_id, "upstream reference")
    microphone_position_id = _session_identity(microphone_position_id, "microphone position")
    reference_candidate_channels = _reference_candidate_channels(
        reference_input_channel, reference_input_channel_left, reference_input_channel_right
    )
    electrical_requested = bool(reference_candidate_channels)
    if getattr(store, "measurement_target_provider", None) is None:
        raise ValueError("Speaker Align acquisition requires a measurement target provider")
    requests = alignment.capture_requests()
    if not requests:
        raise ValueError("Speaker Align acquisition requires at least one speaker way")
    for request in requests:
        if request.get("reference_id") != reference_id:
            raise ValueError("Speaker Align acquisition reference_id differs from the frozen requests")
        if request.get("microphone_position_id") != microphone_position_id:
            raise ValueError("Speaker Align acquisition microphone_position_id differs from the frozen requests")
        if request.get("reference_tap") != REFERENCE_TAP_INGRESS:
            raise ValueError(
                f"Speaker Align acquisition for {request.get('role')} requires the ingress reference tap"
            )
    captures: list[dict[str, Any]] = []
    # One shared take before the ways: the relative delays are planned from it,
    # and a take that cannot carry them fails before a way sweep is spent. Its
    # observed input chain opens both the provenance and the identity every way
    # is attested against before its own sweep.
    planning_request = alignment.planning_request()
    if on_progress is not None:
        on_progress(f"{str(planning_request['side'])}_ways", 1, len(requests) + 1)
    planning_take, planning_observed, expected_key, planning_measurement = await _run_side_take(
        store, planning_request, label="planning", reference_id=reference_id,
        microphone_position_id=microphone_position_id, input_id=input_id,
        mic_input_channel=mic_input_channel,
        reference_input_channel=reference_input_channel,
        reference_input_channel_left=reference_input_channel_left,
        reference_input_channel_right=reference_input_channel_right,
        reference_candidate_channels=reference_candidate_channels,
        sweep_profile=sweep_profile, cancel_requested=cancel_requested,
        expected_native_context=expected_native_context)
    planning = alignment.planning(planning_take)
    provenance: dict[str, Any] = {
        key: value for key, value in planning_observed.items() if key != "job_id"
    }
    provenance.update({
        "reference_id": reference_id,
        "microphone_position_id": microphone_position_id,
        "job_ids": [str(planning_observed["job_id"])],
    })
    # Admitted loopback channel per role; the real channel of every way stays
    # visible even when the ways differ.
    channels_by_role: dict[str, Any] = {}
    for way_index, request in enumerate(requests):
        role = request["role"]
        if cancel_requested is not None and cancel_requested():
            raise asyncio.CancelledError("Speaker Align acquisition was cancelled")
        owner = CaptureEvidence()
        if on_progress is not None:
            on_progress(role, way_index + 2, len(requests) + 1)
        job = await store.start_measurement(
            input_id=input_id,
            mic_input_channel=mic_input_channel,
            reference_input_channel=reference_input_channel,
            reference_input_channel_left=reference_input_channel_left,
            reference_input_channel_right=reference_input_channel_right,
            reference_candidate_channels=reference_candidate_channels,
            channel=request["channel"],
            measurement_bank=role,
            sweep_profile=dict(sweep_profile) if sweep_profile else None,
            capture_evidence=owner,
            **(expected_native_context or {}),
        )
        job_id = str(job["id"])
        # Fail fast before the worker's first sweep: the frozen target and the
        # resolved input chain are both known at registration time. Draining
        # first cancels the just-started job before its worker runs, so no
        # capture is spent and no worker task leaks past this call. The checks
        # below are synchronous, so no caller cancellation can interleave here.
        try:
            key = _job_input_key(job, role)
            if key != expected_key:
                raise RuntimeError(
                    f"Speaker Align microphone input changed before capturing {role}; "
                    "keep the same microphone and reference input channels for every way"
                )
            if job.get("measurement_target") != request["measurement_target"]:
                raise ValueError(
                    f"Speaker Align target for {role} is stale; revision, device and "
                    "processing must match the frozen requests"
                )
        except BaseException as error:
            await restore_after(error, store.drain_job(job_id), what="Draining the capture job")
            raise
        finished = await _await_measurement_job(
            store, job_id, label=f"Speaker Align capture for {role}")
        evidence = owner.take()
        if evidence.get("time_reference") != "deconvolved-sweep-origin":
            raise RuntimeError(f"Speaker Align capture for {role} lost its deconvolved sweep origin")
        analysis = evidence["analysis"]
        if analysis.get("sample_rate") != request["measurement_target"].get("sample_rate_hz"):
            raise RuntimeError(f"Speaker Align capture sample rate changed for {role}")
        capture_info = evidence.get("capture") if isinstance(evidence.get("capture"), dict) else {}
        if way_index > 0:
            _require_unboosted_levels(capture_info, role=role)
        _admit_reference(analysis, capture_info.get("reference_node"),
                         electrical_requested=electrical_requested,
                         label=f"Speaker Align {role}")
        finished_input = finished.get("input") if isinstance(finished.get("input"), dict) else {}
        observed = _observed_input_chain(
            role=role, capture_info=capture_info, finished_input=finished_input,
            analysis=analysis, reference_candidate_channels=reference_candidate_channels,
            electrical_requested=electrical_requested,
            microphone_calibration=_applied_calibration(finished))
        if observed["electrical_reference_channel"] is not None:
            channels_by_role[role] = observed["electrical_reference_channel"]
        # The chosen loopback channel is per-take evidence, not an identity: a
        # two-way speaker can carry its low way on one loopback half and its
        # high way on the other, so only the configured candidate set has to
        # stay identical between takes.
        differing = [
            name for name in observed
            if name != "electrical_reference_channel" and observed[name] != provenance[name]
        ]
        if differing:
            raise RuntimeError(
                f"Speaker Align {'/'.join(differing)} changed between takes; "
                "keep the microphone fixed and the reference tap untouched"
            )
        provenance["job_ids"].append(job_id)
        captures.append({
            "role": role,
            "measurement_target": evidence["measurement_target"],
            "reference_id": reference_id,
            "microphone_position_id": microphone_position_id,
            "reference_tap": REFERENCE_TAP_INGRESS,
            "reference_node": observed["reference_node"],
            "time_reference": evidence["time_reference"],
            "impulse_response": evidence["impulse_response"],
            "analysis": analysis,
        })
    provenance["electrical_reference_channels_by_role"] = channels_by_role
    # The planning evidence travels with the job record; the estimator's raw
    # per-band internals stay out of it, like the check summary's.
    provenance["planning"] = {
        key: value for key, value in planning.items() if key != "bands"
    }
    return {"captures": captures, "planning": planning, "provenance": provenance,
            "measurement": planning_measurement}


async def verify_speaker_alignment(
    store: Any,
    alignment: Any,
    *,
    input_id: str,
    mic_input_channel: str | int | None = "1",
    reference_input_channel: str | int | None,
    reference_input_channel_left: str | int | None = None,
    reference_input_channel_right: str | int | None = None,
    reference_id: str,
    microphone_position_id: str,
    sweep_profile: dict[str, float] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
    expected_native_context: dict[str, Any] | None = None,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, Any]:
    """Run the side's single shared verification take and judge it acoustically.

    Returns ``{"confirmation": {...}, "provenance": {...}, "measurement": ...}``:
    the confirmation document ``verify_confirmation`` consumes, built from this
    take's band-limited way arrivals, the observed input chain the caller
    attests against the planning acquisition, and the take's normal measurement
    (the After view). One take, one time base: no way is judged against its own
    post-delay reference here.
    """
    request = alignment.verification_request()
    reference_id = _session_identity(reference_id, "upstream reference")
    microphone_position_id = _session_identity(microphone_position_id, "microphone position")
    reference_candidate_channels = _reference_candidate_channels(
        reference_input_channel, reference_input_channel_left, reference_input_channel_right
    )
    if getattr(store, "measurement_target_provider", None) is None:
        raise ValueError("Speaker Align verification requires a measurement target provider")
    if on_progress is not None:
        on_progress(f"{str(request['side'])}_ways", 1, 1)
    take, observed, _, measurement = await _run_side_take(
        store, request, label="verification", reference_id=reference_id,
        microphone_position_id=microphone_position_id, input_id=input_id,
        mic_input_channel=mic_input_channel,
        reference_input_channel=reference_input_channel,
        reference_input_channel_left=reference_input_channel_left,
        reference_input_channel_right=reference_input_channel_right,
        reference_candidate_channels=reference_candidate_channels,
        sweep_profile=sweep_profile, cancel_requested=cancel_requested,
        expected_native_context=expected_native_context)
    return {
        "confirmation": alignment.confirmation(take),
        "provenance": {
            **{key: value for key, value in observed.items() if key != "job_id"},
            "reference_id": reference_id,
            "microphone_position_id": microphone_position_id,
            "job_ids": [str(observed["job_id"])],
        },
        "measurement": measurement,
    }
