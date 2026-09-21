# SPDX-License-Identifier: AGPL-3.0-only
"""Serial Speaker Align way acquisition over the internal evidence transport.

This adapter drives one ``CaptureEvidence`` owner per frozen
``SpeakerAlignment`` request, strictly serially, and returns propose-ready
captures. It never stages, applies or commits anything.

Each way records the configured electrical-reference candidate channels in a
single take; the store's electrical-reference evaluation then admits the
channel that actually carries that way, and the admitted channel is kept in
the capture evidence and provenance. Attestation limits, stated plainly:
software can prove that every way was captured through the same observed
microphone input, the same configured reference candidate set and the same
reference node at the same rate, with the same frozen target the alignment was
planned from. It cannot prove physical wiring or that the microphone stayed
fixed, and the admitted loopback channel may legitimately differ between ways.
The caller supplies the session's upstream ``reference_id`` and
``microphone_position_id`` as the operator's assertion; the adapter binds those
IDs to the observed common input and rejects any change between ways. Only
stable electrical-reference evidence is admitted; fallback, tolerated and
acoustic-only timing fail fast before the next way.
Quality, confidence, overlap and staleness-at-proposal-time remain
``SpeakerAlignment.propose`` gates; this adapter is acquisition, not
authorization.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

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
    """Capture every speaker way serially; return propose-ready captures.

    Returns ``{"captures": [...], "provenance": {...}}`` where each capture
    carries exactly the evidence shape ``SpeakerAlignment.propose`` expects
    and ``provenance`` records the observed common input all ways shared.
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
    provenance: dict[str, Any] | None = None
    expected_key: tuple | None = None
    # Admitted loopback channel per role; the real channel of every way stays
    # visible even when the ways differ.
    channels_by_role: dict[str, Any] = {}
    for way_index, request in enumerate(requests):
        role = request["role"]
        if cancel_requested is not None and cancel_requested():
            raise asyncio.CancelledError("Speaker Align acquisition was cancelled")
        owner = CaptureEvidence()
        if on_progress is not None:
            on_progress(role, way_index + 1, len(requests))
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
        # capture is spent and no worker task leaks past this call. Any
        # registration-time throw drains; the checks below are synchronous, so
        # no caller cancellation can interleave here.
        try:
            key = _job_input_key(job, role)
            if expected_key is None:
                expected_key = key
            elif key != expected_key:
                raise RuntimeError(
                    f"Speaker Align microphone input changed before capturing {role}; "
                    "keep the same microphone and reference input channels for every way"
                )
            if job.get("measurement_target") != request["measurement_target"]:
                raise ValueError(
                    f"Speaker Align target for {role} is stale; revision, device and "
                    "processing must match the frozen requests"
                )
        except BaseException:
            await store.drain_job(job_id)
            raise
        # Same-package internal boundary: the store returns no public handle
        # for awaiting a job without cancelling it (drain_job cancels live
        # jobs), so await the runner task directly, then drain for cleanup.
        task = store._job_tasks[job_id]
        try:
            await task
        except asyncio.CancelledError:
            await store.drain_job(job_id)
            raise
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            # Our own cancellation was absorbed by cancelling the runner task,
            # which swallows it into a cancelled job status: restore the
            # caller's CancelledError contract instead of reporting failure.
            await store.drain_job(job_id)
            raise asyncio.CancelledError("Speaker Align acquisition was cancelled")
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
        capture_info = evidence.get("capture") if isinstance(evidence.get("capture"), dict) else {}
        try:
            require_timing_reference(analysis, capture_info.get("reference_node"))
            if electrical_requested and not analysis["reference_path"].get("electrical_reference_used"):
                raise ValueError("Selected electrical reference was not captured")
        except ValueError as exc:
            raise RuntimeError(f"Speaker Align {role} reference failed (electrical reference or ingress monitor): {exc}") from exc
        finished_input = finished.get("input") if isinstance(finished.get("input"), dict) else {}
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
        }
        if observed["electrical_reference_channel"] is not None:
            channels_by_role[role] = observed["electrical_reference_channel"]
        # Fail closed before comparing: two ways both missing a field must
        # never attest sameness of nothing.
        missing = [name for name, value in observed.items() if value is None
                   and (name not in ("electrical_reference_channel", "electrical_reference_candidates")
                        or electrical_requested)]
        if missing:
            raise RuntimeError(
                f"Speaker Align capture for {role} is missing {', '.join(missing)}; "
                "refusing to attest an incomplete input chain"
            )
        if provenance is None:
            # ``electrical_reference_channel`` stays the first way's admitted
            # channel; the per-role map below carries the rest.
            provenance = {
                **observed,
                "reference_id": reference_id,
                "microphone_position_id": microphone_position_id,
                "job_ids": [job_id],
            }
        else:
            # The chosen loopback channel is per-way evidence, not an identity: a
            # two-way speaker can carry its low way on one loopback half and its
            # high way on the other, so only the configured candidate set has to
            # stay identical between ways.
            differing = [
                name for name in observed
                if name != "electrical_reference_channel" and observed[name] != provenance[name]
            ]
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
            "reference_node": observed["reference_node"],
            "time_reference": evidence["time_reference"],
            "impulse_response": evidence["impulse_response"],
            "analysis": analysis,
        })
    provenance["electrical_reference_channels_by_role"] = channels_by_role
    return {"captures": captures, "provenance": provenance}
