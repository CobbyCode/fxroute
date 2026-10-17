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
from audio.filter_banks import resolve_bank, summarize_all_banks, summarize_banks
from audio.output_state import effective_crossover_enabled, routing_for_device, validate_output_state
from audio.output_topology import MAX_CHANNELS, SUB_ROLES, derive_topology, surround_group
from audio.samplerate.constants import FXROUTE_MAX_PROCESSING_RATE
from dsp.native_config import role_side
from dsp.surround_global import routed_groups
from dsp.processing_plan import rendered_crossover_filters

__all__ = [
    "ALL_BANKS_ID",
    "GLOBAL_BANK_ID",
    "LEGACY_TARGET",
    "REFERENCE_TAP_INGRESS",
    "area_measured_roles",
    "attach_measurement_target",
    "freeze_measurement_target",
    "measurement_target_from_context",
    "narrow_measured_target",
    "require_commit_target",
    "summed_role_ids",
    "sweep_output_masks",
    "target_input_mask",
    "target_output_mask",
    "targets_compatible",
]

SCHEMA = "fxroute.measurement-target"
VERSION = 1
GLOBAL_BANK_ID = "global"
# Ind. Banks measures the system or a Surround group as currently listened: no bank is owned,
# so a result can be saved and inspected but never committed as a correction.
ALL_BANKS_ID = "all"
# A host-reference or electrical capture taps the DSP ingress monitor, i.e.
# upstream of Global, the matrix, and every crossover/area filter.  A reference
# tapped after a way's filters would subtract the delay being measured, so the
# tap location is recorded in every target instead of being assumed.
REFERENCE_TAP_INGRESS = "fxroute_dsp_sink.monitor"
# Surround program positions the host sink always feeds (FL, FR): engine inputs 0 and 1.
SURROUND_FRONT_INPUT_MASK = 0b11
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


def _measurement_area(validated: dict, *, bank_id: object, output_key: object, channels: object,
                      group: str | None):
    """Resolve the outputs a measurement of this bank addresses.

    Returns ``(topology, measured, grouped)``: the roles the bank owns on the
    given outputs, narrowed to a speaker group plus every sub when ``grouped``.
    """
    mode = validated["active_mode"]
    config = validated["modes"][mode]
    if not isinstance(bank_id, str):
        raise ValueError("Measurement bank must be a string")
    all_banks = bank_id == ALL_BANKS_ID
    try:
        definition = {"id": ALL_BANKS_ID, "roles": []} if all_banks else resolve_bank(config, bank_id)
    except ValueError as exc:
        raise ValueError(f"Bank {bank_id!r} is not stored in output mode {mode}") from exc
    if not isinstance(output_key, str) or not output_key.strip():
        raise ValueError("Output device key must be a non-empty string")
    _bounded_int(channels, "Measurement channel count", MAX_CHANNELS)
    topology = derive_topology(mode, routing_for_device(validated, mode, output_key, channels), channels=channels,
                               crossover_enabled=effective_crossover_enabled(validated, mode, channels))
    topology.require_activatable()
    if all_banks:
        if not topology.roles:
            raise ValueError("Ind. Banks needs at least one routed speaker")
        definition = {**definition, "roles": list(topology.roles)}
    surround_global = mode == "surround" and bank_id == GLOBAL_BANK_ID
    group_supported = mode == "surround" and bank_id in (GLOBAL_BANK_ID, ALL_BANKS_ID)
    if group is not None and not group_supported:
        raise ValueError("A speaker group applies to Surround Global or Ind. Banks only")
    grouped = surround_global or (group_supported and group is not None)
    if grouped:
        if group not in routed_groups(topology.roles):
            raise ValueError(
                "Surround measurement is per speaker group; select a routed one "
                f"(got {group!r})")
        definition = {**definition, "roles": [
            role for role in topology.roles if role in SUB_ROLES or surround_group(role) == group]}
    # Internal Speaker Align captures can still address one physical way.
    measured = ([bank_id] if bank_id in topology.roles else definition["roles"])
    if bank_id != GLOBAL_BANK_ID and not set(measured) <= set(topology.roles):
        raise ValueError(f"Bank {bank_id} is not an active role on the selected outputs")
    return topology, measured, grouped


def _covered_roles(topology, bank_id: str, measured: Sequence[str], grouped: bool) -> list[str]:
    whole_system = bank_id in (GLOBAL_BANK_ID, ALL_BANKS_ID) and not grouped
    return list(topology.roles) if whole_system else list(measured)


def area_measured_roles(state: dict, *, bank_id: str, output_key: str, channels: int,
                        group: str | None = None) -> list[str]:
    """Roles a measurement of this bank covers on the given outputs.

    The ``measured_roles`` a target frozen now would record.  No processing is
    compiled, so the answer depends on routing and bank layout only.
    """
    topology, measured, grouped = _measurement_area(
        validate_output_state(state), bank_id=bank_id, output_key=output_key, channels=channels, group=group)
    return _covered_roles(topology, bank_id, measured, grouped)


def freeze_measurement_target(
    state: dict,
    *,
    bank_id: str,
    output_key: str,
    channels: int,
    sample_rate_hz: int,
    fingerprint: str,
    group: str | None = None,
) -> dict:
    """Freeze the measurement target from committed output state.

    Surround Global requires a speaker ``group`` (front, center, side, rear).
    Ind. Banks optionally uses the same scope; without a group it measures
    the whole system. A group covers its speakers plus every sub.

    ``fingerprint`` is the service-computed processing fingerprint for the
    same device and rate.  The result is a detached, JSON-serializable
    document; later state edits never change a frozen target.
    """
    validated = validate_output_state(state)
    mode = validated["active_mode"]
    topology, measured, grouped = _measurement_area(
        validated, bank_id=bank_id, output_key=output_key, channels=channels, group=group)
    all_banks = bank_id == ALL_BANKS_ID
    _bounded_int(sample_rate_hz, "Measurement sample rate", FXROUTE_MAX_PROCESSING_RATE)
    fingerprint = _fingerprint_token(fingerprint)
    banks = validated["modes"][mode]["banks"]
    all_summary = summarize_all_banks([banks[role] for role in topology.roles]) if all_banks else None
    target = {
        "schema": SCHEMA,
        "version": VERSION,
        "mode": mode,
        "device_key": device_key(output_key),
        "bank_id": bank_id,
        "preset": (all_summary["preset"] if all_banks else
                   summarize_banks([banks[role] for role in (['global'] if bank_id == GLOBAL_BANK_ID else measured)])["preset"]),
        "revision": validated["revision"],
        "processing_fingerprint": fingerprint,
        "sample_rate_hz": sample_rate_hz,
        "channels": channels,
        "roles": list(topology.roles),
        "measured_roles": _covered_roles(topology, bank_id, measured, grouped),
        "reference_tap": REFERENCE_TAP_INGRESS,
    }
    if grouped:
        # Keep the existing scope field for both Surround aggregate targets.
        target["global_group"] = group
    if all_banks:
        # The listened A/B side and preset of every bank; a mixed state has no single preset.
        target["active_side"] = all_summary["active_side"]
        target["bank_presets"] = {role: banks[role]["preset"] for role in topology.roles}
    if bank_id not in (GLOBAL_BANK_ID, ALL_BANKS_ID) and len(measured) == 1 and measured[0] in SUB_ROLES:
        # An isolated sub has no broadband plateau at 120 Hz..8 kHz.
        # Freeze its rendered band, including per-side bass and way filters.
        filters = rendered_crossover_filters(validated, output_key=output_key, channels=channels)[measured[0]]
        low = max([20.0] + [float(spec["frequency_hz"]) for spec in filters if spec["kind"] == "highpass"])
        high = min([20000.0, sample_rate_hz / 2.0] + [float(spec["frequency_hz"]) for spec in filters if spec["kind"] == "lowpass"])
        if low < high:
            target["level_reference_band_hz"] = [low, high]
    return target


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
    output index order).  Bit ``n`` mutes engine output ``n``.  A Global target
    that measures every active role returns 0; one narrowed to a role set
    (one speaker side's shared verification sweep) mutes every role it does
    not measure, exactly like an area target, so a fanned-out measured role
    keeps all of its physical ports audible.
    """
    measured = _measured_roles(target)
    ordered = _engine_roles(roles, measured)
    if target.get("bank_id") == GLOBAL_BANK_ID and len(measured) == len(ordered):
        return 0
    return sum(1 << index for index, role in enumerate(ordered) if role not in measured)


def target_input_mask(target: dict) -> int:
    """Return the engine input mute mask for this target (0 mutes nothing).

    The sweep always plays into the host sink, which feeds the Front program
    positions (FL, FR) as the timing reference.  A Surround Global group other
    than Front measures its speakers together with the audible subs; Front bass
    would reach them through the shared bass bus, so those two inputs are
    silenced for the take.
    """
    group = target.get("global_group")
    if target.get("mode") == "surround" and group is not None and group != "front":
        return SURROUND_FRONT_INPUT_MASK
    return 0


def narrow_measured_target(target: dict, *, roles: Sequence[str]) -> dict:
    """Return a copy of ``target`` that measures only ``roles``.

    A Speaker Align shared verification sweep plays every way of one speaker
    side at once: the narrowed target keeps the frozen schema, device,
    revision and processing identity while its measured roles -- and with them
    its mute mask -- narrow to that side.  A Global target keeps measuring
    through the complete active chain.
    """
    if not isinstance(target, dict) or target.get("legacy"):
        raise ValueError("A narrowed measurement target needs a frozen target document")
    ordered = _engine_roles(target.get("roles") or [], target.get("measured_roles") or [])
    wanted = [str(role) for role in roles]
    if not wanted or len(set(wanted)) != len(wanted):
        raise ValueError("A narrowed measurement target needs distinct role names")
    missing = [role for role in wanted if role not in ordered]
    if missing:
        raise ValueError(
            f"Narrowed measurement target role is no longer routed: {', '.join(sorted(missing))}")
    narrowed = copy.deepcopy(target)
    narrowed["measured_roles"] = [role for role in ordered if role in set(wanted)]
    return narrowed


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


def _role_set(roles: object) -> set[str]:
    return {role for role in roles if isinstance(role, str)} if isinstance(roles, list) else set()


def require_commit_target(target: object, roles: object, *, mode: str, bank_id: str,
                          group: str | None = None) -> None:
    """Reject a generated-correction commit into an area its measurement does not fit.

    ``target`` is the frozen target of a source measurement; ``roles`` are the
    roles the area the preset is committed into plays now (see
    area_measured_roles).  The measurement must have been taken for that mode,
    bank and speaker group and must cover exactly those roles, so a correction
    measured through one area or channel shape can never be committed as if it
    were measured through another.  Sample rate, processing, output device and
    reference tap are not compared: a measurement stays committable after they
    change.  A result without a frozen target is rejected.
    """
    if not isinstance(target, dict) or target.get("schema") != SCHEMA or target.get("legacy"):
        raise ValueError("Source measurement carries no frozen target; measure again")
    if target.get("bank_id") == ALL_BANKS_ID:
        raise ValueError("A measurement taken through Ind. Banks cannot be imported or committed into a bank")
    if target.get("mode") != mode or target.get("bank_id") != bank_id:
        raise ValueError(
            f"Measurement was captured for {target.get('mode')} area "
            f"{target.get('bank_id')!r}, but this commit targets {mode} area {bank_id!r}"
        )
    if target.get("global_group") != group:
        raise ValueError(
            f"Measurement was captured for speaker group {target.get('global_group')!r}, "
            f"but this commit targets {group!r}"
        )
    measured, played = _role_set(target.get("measured_roles")), _role_set(roles)
    if not measured or measured != played:
        raise ValueError(
            f"Measurement covers {sorted(measured)!r}, but {mode} area {bank_id!r} "
            f"plays {sorted(played)!r}"
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
        target.get("global_group"),
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
    if not {"mode", "device_key", "channels", "sample_rate_hz"}.issubset(target):
        raise ValueError("Measurement target has no complete v2 output context")
    result = copy.deepcopy(context) if isinstance(context, dict) else {}
    result.update(output_mode=target["mode"], output_key=target["device_key"],
                  output_channels=target["channels"], sample_rate_hz=target["sample_rate_hz"])
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
