# SPDX-License-Identifier: AGPL-3.0-only
"""Serial Speaker Align way acquisition over the internal evidence transport.

This adapter drives one ``CaptureEvidence`` owner per frozen
``SpeakerAlignment`` request, strictly serially, and returns propose-ready
captures. It never stages, applies or commits anything.

Attestation limits, stated plainly: software can prove that every way was
captured through the same observed microphone input, electrical-reference
input channel and reference node at the same rate, with the same frozen
target the alignment was planned from. It cannot prove physical wiring or
that the microphone stayed fixed. The caller supplies the session's upstream
``reference_id`` and ``microphone_position_id`` as the operator's assertion;
the adapter binds those IDs to the observed common input and rejects any
change between ways. Only stable electrical-reference evidence is admitted;
fallback, tolerated and acoustic-only timing fail fast before the next way.
Quality, confidence, overlap and staleness-at-proposal-time remain
``SpeakerAlignment.propose`` gates; this adapter is acquisition, not
authorization.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from measurement.capture_evidence import CaptureEvidence
from measurement.target import REFERENCE_TAP_INGRESS


def _session_identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"Speaker Align acquisition requires a {label} identity")
    return value


def _job_input_key(job: dict[str, Any]) -> tuple:
    """Identify the capture input chain a job was started with.

    Compared before each way's sweep so a changed microphone or reference
    input fails before spending another capture, not after it.
    """
    input_info = job.get("input") if isinstance(job.get("input"), dict) else {}
    channels = job.get("input_channels") if isinstance(job.get("input_channels"), dict) else {}
    return (
        input_info.get("node_name"),
        input_info.get("node_serial"),
        channels.get("mic"),
        channels.get("electrical_reference"),
        input_info.get("measurement_sample_rate") or input_info.get("sample_rate"),
    )


def _require_stable_reference(analysis: dict[str, Any], role: str) -> None:
    reference = analysis.get("reference_path") if isinstance(analysis.get("reference_path"), dict) else {}
    if (reference.get("usable") is not True
            or reference.get("electrical_reference_used") is not True
            or reference.get("electrical_reference_fallback")
            or reference.get("timing_status") != "electrical-reference"
            or reference.get("stability") != "stable"):
        status = reference.get("timing_status") or reference.get("stability") or "unusable"
        raise RuntimeError(
            f"Speaker Align acquisition for {role} has no stable electrical reference ({status}); "
            "fix the upstream tap or its level and repeat the acquisition"
        )


async def acquire_speaker_captures(
    store: Any,
    alignment: Any,
    *,
    input_id: str,
    mic_input_channel: str | int | None = "1",
    reference_input_channel: str | int | None,
    reference_id: str,
    microphone_position_id: str,
    sweep_profile: dict[str, float] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Capture every speaker way serially; return propose-ready captures.

    Returns ``{"captures": [...], "provenance": {...}}`` where each capture
    carries exactly the evidence shape ``SpeakerAlignment.propose`` expects
    and ``provenance`` records the observed common input all ways shared.
    """
    reference_id = _session_identity(reference_id, "upstream reference")
    microphone_position_id = _session_identity(microphone_position_id, "microphone position")
    if reference_input_channel is None or not str(reference_input_channel).strip():
        raise ValueError("Speaker Align acquisition requires an electrical reference input channel")
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
    provenance: dict[str, Any] | None = None
    expected_key: tuple | None = None
    for request in requests:
        role = request["role"]
        if cancel_requested is not None and cancel_requested():
            raise asyncio.CancelledError("Speaker Align acquisition was cancelled")
        owner = CaptureEvidence()
        job = await store.start_measurement(
            input_id=input_id,
            mic_input_channel=mic_input_channel,
            reference_input_channel=reference_input_channel,
            channel=request["channel"],
            measurement_bank=role,
            sweep_profile=dict(sweep_profile) if sweep_profile else None,
            capture_evidence=owner,
        )
        job_id = str(job["id"])
        # Fail fast before the worker's first sweep: the frozen target and the
        # resolved input chain are both known at registration time. Draining
        # first cancels the just-started job before its worker runs, so no
        # capture is spent and no worker task leaks past this call.
        key = _job_input_key(job)
        if expected_key is None:
            expected_key = key
        elif key != expected_key:
            await store.drain_job(job_id)
            raise RuntimeError(
                f"Speaker Align microphone input changed before capturing {role}; "
                "keep the same microphone and reference input channels for every way"
            )
        if job.get("measurement_target") != request["measurement_target"]:
            await store.drain_job(job_id)
            raise ValueError(
                f"Speaker Align target for {role} is stale; revision, device and "
                "processing must match the frozen requests"
            )
        # Same-package internal boundary: the store returns no public handle
        # for awaiting a job without cancelling it (drain_job cancels live
        # jobs), so await the runner task directly, then drain for cleanup.
        task = store._job_tasks[job_id]
        try:
            await task
        except asyncio.CancelledError:
            await store.drain_job(job_id)
            raise
        await store.drain_job(job_id)
        finished = store.get_job(job_id)
        if finished.get("status") != "completed":
            error = finished.get("error") if isinstance(finished.get("error"), dict) else {}
            detail = error.get("detail") or finished.get("message") or finished.get("status")
            raise RuntimeError(f"Speaker Align capture for {role} did not complete: {detail}")
        evidence = owner.take()
        if evidence.get("time_reference") != "deconvolved-sweep-origin":
            raise RuntimeError(f"Speaker Align capture for {role} lost its deconvolved sweep origin")
        analysis = evidence["analysis"]
        if analysis.get("sample_rate") != request["measurement_target"].get("sample_rate_hz"):
            raise RuntimeError(f"Speaker Align capture sample rate changed for {role}")
        _require_stable_reference(analysis, role)
        capture_info = evidence.get("capture") if isinstance(evidence.get("capture"), dict) else {}
        finished_input = finished.get("input") if isinstance(finished.get("input"), dict) else {}
        observed = {
            "microphone_node": capture_info.get("microphone_node"),
            "microphone_serial": finished_input.get("node_serial"),
            "mic_channel": capture_info.get("mic_input_channel"),
            "electrical_reference_channel": capture_info.get("electrical_reference_input_channel"),
            "reference_node": capture_info.get("reference_node"),
            "sample_rate_hz": analysis.get("sample_rate"),
        }
        if provenance is None:
            provenance = {**observed, "job_ids": [job_id]}
        else:
            differing = [name for name in observed if observed[name] != provenance[name]]
            if differing:
                raise RuntimeError(
                    f"Speaker Align {'/'.join(differing)} changed between ways; "
                    "keep the microphone fixed and the reference tap untouched"
                )
            provenance["job_ids"].append(job_id)
        captures.append({
            "role": role,
            "measurement_target": evidence["measurement_target"],
            "reference_id": reference_id,
            "microphone_position_id": microphone_position_id,
            "reference_tap": REFERENCE_TAP_INGRESS,
            "time_reference": evidence["time_reference"],
            "impulse_response": evidence["impulse_response"],
            "analysis": analysis,
        })
    return {"captures": captures, "provenance": provenance}
