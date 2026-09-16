# SPDX-License-Identifier: AGPL-3.0-only
"""Frozen measurement target: the area, revision, and processing a sweep captures.

A measurement job freezes its target when the job is created: active mode,
selected bank (Global or one area), committed output-state revision, compiled
processing fingerprint, the bank's listened preset, and the reference tap
location.  Everything afterwards compares against the frozen values instead
of re-reading live selection, so changing the editing area during a running
capture can never retarget its result.

Sweeps always run through the complete active chain (Global plus the area's
crossover and correction bank).  Unrelated logical outputs are muted at the
final output stage; Global measures the whole system and mutes nothing.

This module is pure: no files, no hardware, no runtime, no global state.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence

from audio.output_routing import device_key
from audio.output_state import routing_for_device, validate_output_state
from audio.output_topology import MAX_CHANNELS, SUB_ROLES, derive_topology
from audio.samplerate.constants import FXROUTE_MAX_PROCESSING_RATE
from dsp.native_config import role_side

__all__ = [
    "GLOBAL_BANK_ID",
    "LEGACY_TARGET",
    "REFERENCE_TAP_INGRESS",
    "attach_measurement_target",
    "freeze_measurement_target",
    "measurement_target_from_context",
    "require_commit_target",
    "require_target_matches",
    "summed_role_ids",
    "sweep_output_masks",
    "target_output_mask",
    "targets_compatible",
]

SCHEMA = "fxroute.measurement-target"
VERSION = 1
GLOBAL_BANK_ID = "global"
# A host-reference or electrical capture taps the DSP ingress monitor, i.e.
# upstream of Global, the matrix, and every crossover/area filter.  A reference
# tapped after a way's filters would subtract the delay being measured, so the
# tap location is recorded in every target instead of being assumed.
REFERENCE_TAP_INGRESS = "fxroute_dsp_sink.monitor"
# Stored results from before the frozen target existed stay readable: they are
# marked explicitly as legacy rather than silently treated as Global captures.
LEGACY_TARGET = {"schema": SCHEMA, "version": VERSION, "legacy": True}


def _bounded_int(value: object, label: str, maximum: int) -> int:
    if type(value) is not int or not 0 < value <= maximum:
        raise ValueError(f"{label} must be a positive integer up to {maximum}")
    return value


def _fingerprint_token(value: object) -> str:
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or any(character.isspace() for character in value)):
        raise ValueError("Processing fingerprint must be a non-empty token without whitespace")
    return value


def freeze_measurement_target(
    state: dict,
    *,
    bank_id: str,
    output_key: str,
    channels: int,
    sample_rate_hz: int,
    fingerprint: str,
) -> dict:
    """Freeze the measurement target from committed output state.

    ``fingerprint`` is the service-computed processing fingerprint for the
    same device and rate.  The result is a detached, JSON-serializable
    document; later state edits never change a frozen target.
    """
    validated = validate_output_state(state)
    mode = validated["active_mode"]
    if not isinstance(bank_id, str) or bank_id not in validated["modes"][mode]["banks"]:
        raise ValueError(f"Bank {bank_id!r} is not stored in output mode {mode}")
    if not isinstance(output_key, str) or not output_key.strip():
        raise ValueError("Output device key must be a non-empty string")
    _bounded_int(channels, "Measurement channel count", MAX_CHANNELS)
    _bounded_int(sample_rate_hz, "Measurement sample rate", FXROUTE_MAX_PROCESSING_RATE)
    fingerprint = _fingerprint_token(fingerprint)
    topology = derive_topology(mode, routing_for_device(validated, mode, output_key), channels=channels)
    topology.require_activatable()
    if bank_id != GLOBAL_BANK_ID and bank_id not in topology.roles:
        raise ValueError(f"Bank {bank_id} is not an active role on the selected outputs")
    banks = validated["modes"][mode]["banks"]
    return {
        "schema": SCHEMA,
        "version": VERSION,
        "mode": mode,
        "device_key": device_key(output_key),
        "bank_id": bank_id,
        "preset": banks[bank_id]["preset"],
        "revision": validated["revision"],
        "processing_fingerprint": fingerprint,
        "sample_rate_hz": sample_rate_hz,
        "channels": channels,
        "roles": list(topology.roles),
        "measured_roles": list(topology.roles) if bank_id == GLOBAL_BANK_ID else [bank_id],
        "reference_tap": REFERENCE_TAP_INGRESS,
    }


def _measured_roles(target: dict) -> list[str]:
    if not isinstance(target, dict) or target.get("legacy"):
        raise ValueError("A legacy measurement result carries no frozen target")
    measured = target.get("measured_roles")
    if not isinstance(measured, list) or not measured or any(not isinstance(role, str) or not role for role in measured):
        raise ValueError("Measurement target requires measured role names")
    return list(measured)


def _engine_roles(roles: Sequence[str], measured: Sequence[str]) -> list[str]:
    """Validate the ordered engine role list of the running plan."""
    ordered = [str(role) for role in roles]
    if len(set(ordered)) != len(ordered):
        raise ValueError("Engine roles must be unique")
    if len(ordered) > MAX_CHANNELS:
        raise ValueError(f"Engine roles exceed {MAX_CHANNELS} outputs")
    missing = [role for role in measured if role not in ordered]
    if missing:
        raise ValueError(f"Measurement target roles are no longer routed: {', '.join(sorted(missing))}")
    return ordered


def target_output_mask(target: dict, *, roles: Sequence[str]) -> int:
    """Return the engine output mute mask for this target.

    ``roles`` is the ordered active role list of the running plan (engine
    output index order).  Bit ``n`` mutes engine output ``n``.  Global targets
    return 0; area targets mute every output whose role is not measured, so a
    fanned-out measured role keeps all of its physical ports audible.
    """
    measured = _measured_roles(target)
    ordered = _engine_roles(roles, measured)
    if target.get("bank_id") == GLOBAL_BANK_ID:
        return 0
    return sum(1 << index for index, role in enumerate(ordered) if role not in measured)


def summed_role_ids(roles: Sequence[str]) -> set[str]:
    """Sub roles the routing sums from both inputs (both at 0.5 gain).

    Mirrors the plan's input routes: only a real ``sub_l``/``sub_r`` pair feeds
    each sub from its own side.  A single sub role (mono) and every other pair
    (dual-mono) are summed from both inputs, whatever the role is called, so a
    lone ``sub_l`` is *not* a left-sided output.
    """
    active = {str(role) for role in roles}
    subs = tuple(role for role in SUB_ROLES if role in active)
    return set() if subs == ("sub_l", "sub_r") else set(subs)


def sweep_output_masks(target: dict, *, roles: Sequence[str]) -> dict[str, int]:
    """Return the mute mask of each internal sweep of a two-sided capture.

    L/R Repeat (and every other two-sided workflow) runs several sweeps of one
    frozen area: each side is an internal way sweep of the same job, so it
    keeps the measured roles that side can excite audible and mutes the rest.
    Roles that sum both inputs (a mono or dual-mono sub) stay audible in both
    sweeps, and an area that only exists on one side keeps the whole area
    audible in both sweeps instead of silencing a sweep that has nothing else
    to measure.
    """
    measured = _measured_roles(target)
    ordered = _engine_roles(roles, measured)
    summed = summed_role_ids(ordered)
    masks: dict[str, int] = {}
    for side in ("left", "right"):
        side_roles = {role for role in measured if role_side(role) == side and role not in summed}
        if side_roles:
            side_roles |= {role for role in measured if role_side(role) == "mono" or role in summed}
        else:
            side_roles = set(measured)
        masks[side] = sum(1 << index for index, role in enumerate(ordered) if role not in side_roles)
    return masks


def require_target_matches(
    target: dict,
    *,
    mode: str,
    bank_id: str,
    processing_fingerprint: str,
    output_key: str | None = None,
    sample_rate_hz: int | None = None,
) -> None:
    """Reject a result or commit whose live context no longer fits its target.

    Legacy results carry no frozen context and are accepted unchanged.
    """
    if isinstance(target, dict) and target.get("legacy"):
        return
    if not isinstance(target, dict) or target.get("schema") != SCHEMA:
        raise ValueError("Measurement target is missing or malformed")
    mismatches: list[str] = []
    if target.get("mode") != mode:
        mismatches.append(f"mode {target.get('mode')!r} != {mode!r}")
    if target.get("bank_id") != bank_id:
        mismatches.append(f"bank {target.get('bank_id')!r} != {bank_id!r}")
    if target.get("processing_fingerprint") != processing_fingerprint:
        mismatches.append("processing fingerprint changed")
    if output_key is not None and target.get("device_key") != device_key(output_key):
        mismatches.append("output device changed")
    if sample_rate_hz is not None and target.get("sample_rate_hz") != sample_rate_hz:
        mismatches.append(f"sample rate {target.get('sample_rate_hz')} != {sample_rate_hz}")
    if mismatches:
        raise ValueError("Measurement target mismatch: " + "; ".join(mismatches))


# Live facts a stored target must still agree with before a correction that
# was derived from it may be committed anywhere.
_LIVE_FIELDS = (
    ("device_key", "output device"),
    ("sample_rate_hz", "sample rate"),
    ("processing_fingerprint", "processing"),
    ("reference_tap", "reference tap"),
    ("measured_roles", "measured roles"),
)


def require_commit_target(target: dict, live: dict, *, mode: str, bank_id: str) -> None:
    """Reject a generated-correction commit whose measurement no longer applies.

    ``target`` is the frozen target stored with the source measurement and
    ``live`` is the same area frozen from the current committed state.  A
    legacy result (no frozen context) is accepted unchanged, and so is a commit
    into the measurement's own area while its processing is untouched; anything
    else is rejected with the differing fields named, so a correction measured
    through one area or one revision can never be committed as if it were
    measured through another.
    """
    if isinstance(target, dict) and target.get("legacy"):
        return
    if not isinstance(target, dict) or target.get("schema") != SCHEMA:
        raise ValueError("Source measurement carries no frozen target")
    if target.get("mode") != mode or target.get("bank_id") != bank_id:
        raise ValueError(
            f"Measurement was captured for {target.get('mode')} area "
            f"{target.get('bank_id')!r}, but this commit targets {mode} area {bank_id!r}"
        )
    if not isinstance(live, dict) or live.get("schema") != SCHEMA or live.get("legacy"):
        raise ValueError("Live measurement context is unavailable")
    mismatches = [
        f"{label} {target.get(field)!r} != {live.get(field)!r}"
        for field, label in _LIVE_FIELDS
        if target.get(field) != live.get(field)
    ]
    if mismatches:
        raise ValueError(
            "Measurement target no longer matches live processing: " + "; ".join(mismatches)
        )


def _comparison_key(target: dict) -> tuple:
    """Comparable target identity.

    The revision is deliberately absent: a revision bump that leaves the
    compiled processing fingerprint unchanged (a pure editing-selection or
    metadata edit) still describes the same acoustic measurement.
    """
    return (
        target.get("mode"),
        target.get("device_key"),
        target.get("bank_id"),
        target.get("sample_rate_hz"),
        target.get("processing_fingerprint"),
        target.get("reference_tap"),
        tuple(target.get("measured_roles") or ()),
    )


def targets_compatible(left: object, right: object) -> bool:
    """Return whether two results may be averaged or merged together.

    Legacy results are compatible only with other legacy results; mixing areas
    (or the same area measured with different processing) is rejected instead
    of silently averaging incomparable captures.
    """
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    left_legacy = bool(left.get("legacy"))
    right_legacy = bool(right.get("legacy"))
    if left_legacy or right_legacy:
        return left_legacy and right_legacy
    return _comparison_key(left) == _comparison_key(right)


def attach_measurement_target(context: object, target: dict) -> dict:
    """Return a detached audio-output context carrying the frozen target."""
    if not isinstance(target, dict) or target.get("schema") != SCHEMA:
        raise ValueError("Measurement target must be a frozen target document")
    result = copy.deepcopy(context) if isinstance(context, dict) else {}
    result["measurement_target"] = copy.deepcopy(target)
    return result


def measurement_target_from_context(context: object) -> dict:
    """Read a stored target, marking results without one as explicit legacy."""
    if isinstance(context, dict):
        target = context.get("measurement_target")
        if (isinstance(target, dict) and target.get("schema") == SCHEMA
                and type(target.get("version")) is int):
            return copy.deepcopy(target)
    return copy.deepcopy(LEGACY_TARGET)
