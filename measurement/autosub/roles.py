# SPDX-License-Identifier: AGPL-3.0-only
"""AutoSub routing adapter: derived topology to optimizer path and roles.

The routing is the only topology authority. These helpers translate a derived
:class:`~audio.output_topology.OutputTopology` into everything the AutoSub
optimizers need — which legacy optimizer path a system maps to, the engine
output indices of the sub roles, and the mute mask for a Main-only reference
sweep. These are the contracts for migrating runners away from legacy mode
strings and fixed output 3/4 indices; production runner migration is still pending.

Mixed Sub 1/R (and any two sub roles other than a true ``sub_l``/``sub_r``
pair) is dual-mono: it uses the dual-sub optimizer path, never a stereo split.
In Crossover, Main is the sum of all that speaker's ways, so the Main-only
reference excites every way of the side while the subs are muted.

This module is pure: no files, no hardware, no runtime, no global state.
"""

from __future__ import annotations

import math

from audio.output_state import routing_for_device, validate_output_state
from audio.output_topology import OutputTopology, derive_topology

from .candidates import _auto_sub_22_sub, _auto_sub_clamped_delay

__all__ = [
    "autosub_topology_from_state",
    "autosub_apply_knobs",
    "autosub_explicit_knobs",
    "autosub_scan_knobs",
    "main_roles_for_side",
    "optimizer_path",
    "require_autosub_topology",
    "sub_mute_indices",
    "sub_mute_mask",
    "sub_output_indices",
    "sub_roles_for_side",
]


def optimizer_path(topology: OutputTopology) -> str:
    """Map a derived sub topology to the AutoSub optimizer path."""
    return {
        "none": "unavailable",
        "mono": "single-sub",
        "dual-mono": "dual-sub",
        "stereo": "stereo-subs",
    }.get(topology.sub_mode, "unavailable")


def require_autosub_topology(topology: OutputTopology) -> OutputTopology:
    """Reject topologies AutoSub cannot optimize (not activatable or no subs)."""
    topology.require_activatable()
    if optimizer_path(topology) == "unavailable":
        raise ValueError("Auto Sub Optimize requires at least one routed subwoofer")
    return topology


def sub_output_indices(topology: OutputTopology) -> tuple[int, ...]:
    """Engine output indices (0-based, plan order) of the active sub roles."""
    return tuple(index for index, role in enumerate(topology.roles) if role in topology.sub_roles)


def sub_mute_indices(mask: int) -> tuple[int, ...]:
    """Engine output indices (0-based) selected by a mute mask.

    Inverse of ``sum(1 << i for i in indices)``; used to fold a role-derived
    exact mute into stage peak predictions keyed by ``output_n``.
    """
    if type(mask) is not int or mask <= 0 or mask >= (1 << 32):
        raise ValueError("Output mute mask must be a non-zero 32-bit integer")
    return tuple(index for index in range(mask.bit_length()) if mask & (1 << index))


def sub_mute_mask(topology: OutputTopology) -> int:
    """Engine output mute mask that silences exactly the sub roles."""
    return sum(1 << index for index in sub_output_indices(topology))


def sub_roles_for_side(topology: OutputTopology, side: str) -> tuple[str, ...]:
    """Sub roles a sweep of the given input side excites.

    A true stereo ``sub_l``/``sub_r`` pair is side-fed; mono and dual-mono
    subs are summed from both inputs and therefore excited from both sides.
    """
    if side not in ("left", "right"):
        raise ValueError("Side must be left or right")
    if topology.sub_mode == "stereo":
        return tuple(role for role in topology.sub_roles
                     if role == ("sub_l" if side == "left" else "sub_r"))
    return topology.sub_roles


def main_roles_for_side(topology: OutputTopology, side: str) -> tuple[str, ...]:
    """Main roles a Main-only reference of the given side excites.

    In Crossover, Main is the sum of all that speaker's ways; in Stereo it is
    the single ``main_l``/``main_r`` role.
    """
    if side not in ("left", "right"):
        raise ValueError("Side must be left or right")
    if topology.mode == "stereo":
        main_role = "main_l" if side == "left" else "main_r"
        return tuple(role for role in topology.roles if role == main_role)
    return topology.left_ways if side == "left" else topology.right_ways


def autosub_topology_from_state(state: dict, *, output_key: str,
                                channels: int) -> OutputTopology:
    """Derive the AutoSub topology from committed per-mode output state."""
    validated = validate_output_state(state)
    mode = validated["active_mode"]
    return derive_topology(
        mode, routing_for_device(validated, mode, output_key), channels=channels,
    )


def _autosub_scan_delay(value: object, label: str) -> float:
    try:
        return _auto_sub_clamped_delay(float(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"AutoSub scan {label} must be numeric") from exc


def _autosub_checked_bass(crossover_hz: object, main_highpass_enabled: object) -> dict:
    try:
        frequency = int(crossover_hz)
    except (TypeError, ValueError) as exc:
        raise ValueError("AutoSub scan crossover must be numeric") from exc
    return {"frequency_hz": frequency,
            "main_highpass_enabled": bool(main_highpass_enabled)}


def _autosub_strict_polarity(value: object, label: str) -> str:
    if value not in ("normal", "invert"):
        raise ValueError(f"AutoSub {label} must be normal or invert")
    return str(value)


def _autosub_covered_roles(
    start_state: dict, *, output_key: str, channels: int,
    sub_role_map: dict[str, str],
) -> tuple[dict, tuple[str, ...], dict]:
    """Validate one slot map against the routed sub roles, returning context."""
    validated = validate_output_state(start_state)
    topology = require_autosub_topology(autosub_topology_from_state(
        validated, output_key=output_key, channels=channels))
    sub_roles = topology.sub_roles
    if not isinstance(sub_role_map, dict) or set(sub_role_map.values()) != set(sub_roles):
        raise ValueError("AutoSub sub slot map must cover every routed sub role exactly")
    processing = validated["modes"][validated["active_mode"]]["processing"]
    for role in sub_roles:
        if role not in processing:
            raise ValueError(f"Routed sub role {role} has no processing state")
    return validated, sub_roles, processing


def autosub_explicit_knobs(
    start_state: dict, *, output_key: str, channels: int,
    sub_role_map: dict[str, str], slot_delays: dict[str, float],
    slot_levels: dict[str, float], slot_polarities: dict[str, str],
    crossover_hz: int, main_highpass_enabled: bool,
) -> dict:
    """Complete explicit slot values into role-keyed proposal knobs.

    Every slot needs an explicit delay, level and polarity; parked
    inactive subs pass their -80 dB through like any finite level and are
    validated against the authoritative routing by the owner. Polarity
    words are strict: anything but normal/invert is a programming error,
    never silently normalized.
    """
    _autosub_covered_roles(
        start_state, output_key=output_key, channels=channels,
        sub_role_map=sub_role_map)
    for label, mapping in (("delays", slot_delays), ("levels", slot_levels),
                           ("polarities", slot_polarities)):
        if not isinstance(mapping, dict) or set(mapping) != set(sub_role_map):
            raise ValueError(f"AutoSub slot {label} must cover every mapped slot exactly")
    delays = {sub_role_map[slot]: _autosub_scan_delay(value, f"{slot} delay")
              for slot, value in slot_delays.items()}
    levels = {}
    for slot, value in slot_levels.items():
        try:
            level = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"AutoSub slot {slot} level must be numeric") from exc
        if not math.isfinite(level):
            raise ValueError(f"AutoSub slot {slot} level must be finite")
        levels[sub_role_map[slot]] = level
    polarities = {sub_role_map[slot]: _autosub_strict_polarity(value, f"{slot} polarity")
                  for slot, value in slot_polarities.items()}
    return {"sub_delays": delays, "sub_levels": levels,
            "sub_polarities": polarities,
            "bass": _autosub_checked_bass(crossover_hz, main_highpass_enabled)}


def autosub_apply_knobs(
    start_state: dict, *, output_key: str, channels: int,
    sub_role_map: dict[str, str], global_config: dict | None,
    subwoofers_config: dict | None,
) -> dict:
    """Translate one retained legacy candidate triplet into proposal knobs.

    Accepts exactly the shapes the runners persist today: a single-sub
    global config carrying its own sub triplet, or a dual-sub global bass
    config plus a per-slot subwoofers map (inactive slots already parked
    at -80 dB by the callers). Missing keys fail closed instead of
    persisting a partially defaulted candidate.
    """
    if not isinstance(global_config, dict):
        raise ValueError("AutoSub retained candidate requires a global config object")
    if subwoofers_config is None:
        if set(sub_role_map) != {"sub1"}:
            raise ValueError("AutoSub single-sub candidate requires a one-slot map")
        try:
            triplet = {key: global_config[key] for key in (
                "sub_alignment_ms", "sub_level_db", "sub_polarity")}
        except KeyError as exc:
            raise ValueError(f"AutoSub single-sub candidate is missing {exc.args[0]}") from None
        slot_delays = {"sub1": triplet["sub_alignment_ms"]}
        slot_levels = {"sub1": triplet["sub_level_db"]}
        slot_polarities = {"sub1": triplet["sub_polarity"]}
    else:
        if set(sub_role_map) != {"sub1", "sub2"}:
            raise ValueError("AutoSub dual-sub candidate requires a sub1/sub2 map")
        if not isinstance(subwoofers_config, dict):
            raise ValueError("AutoSub dual-sub candidate requires a subwoofers object")
        slot_delays, slot_levels, slot_polarities = {}, {}, {}
        for slot in ("sub1", "sub2"):
            entry = subwoofers_config.get(slot)
            if not isinstance(entry, dict):
                raise ValueError(f"AutoSub dual-sub candidate is missing {slot}")
            try:
                slot_delays[slot] = entry["alignment_ms"]
                slot_levels[slot] = entry["level_db"]
                slot_polarities[slot] = entry["polarity"]
            except KeyError as exc:
                raise ValueError(
                    f"AutoSub dual-sub candidate {slot} is missing {exc.args[0]}") from None
    try:
        frequency = global_config["crossover_frequency_hz"]
    except KeyError as exc:
        raise ValueError(f"AutoSub retained candidate is missing {exc.args[0]}") from None
    return autosub_explicit_knobs(
        start_state, output_key=output_key, channels=channels,
        sub_role_map=sub_role_map, slot_delays=slot_delays,
        slot_levels=slot_levels, slot_polarities=slot_polarities,
        crossover_hz=frequency,
        main_highpass_enabled=global_config.get("main_highpass_enabled", True))


def autosub_scan_knobs(
    start_state: dict, *, output_key: str, channels: int,
    sub_role_map: dict[str, str], delay_ms: float,
    sub1_alignment_ms: float | None, sub2_alignment_ms: float | None,
    active_subs: tuple[str, ...] | list[str],
    sub1_polarity: str | None, sub2_polarity: str | None,
    crossover_hz: int, main_highpass_enabled: bool,
    original_level: float, original_polarity: str,
    original_config_snapshot: dict | None,
) -> dict:
    """Translate one funnel candidate into complete role-keyed proposal knobs.

    Mirrors the legacy per-mode value sources the funnel replaces:
    single-sub candidates carry their level/polarity in the funnel args,
    dual-sub candidates carry them in the snapshot (inactive slots park at
    -80 dB, untouched fields carry the snapshot values). The caller wraps
    the result in an ``AutoSubProposal`` and stages it through the job's
    owner; the owner revalidates roles, revision and values before touching
    any runtime.
    """
    _autosub_covered_roles(
        start_state, output_key=output_key, channels=channels,
        sub_role_map=sub_role_map)
    if (not isinstance(active_subs, (tuple, list))
            or any(slot not in sub_role_map for slot in active_subs)):
        raise ValueError("AutoSub active subs must name mapped sub slots")
    if len(sub_role_map) == 1:
        slot = next(iter(sub_role_map))
        slot_delays = {slot: delay_ms}
        slot_levels = {slot: original_level}
        slot_polarities = {slot: original_polarity}
    elif set(sub_role_map) == {"sub1", "sub2"}:
        snapshot = original_config_snapshot if isinstance(original_config_snapshot, dict) else {}
        snap1 = _auto_sub_22_sub(snapshot, "sub1")
        snap2 = _auto_sub_22_sub(snapshot, "sub2")
        first = sub1_alignment_ms if sub1_alignment_ms is not None else delay_ms
        second = (sub2_alignment_ms if sub2_alignment_ms is not None
                  else snap2.get("alignment_ms", 0.0))
        slot_delays = {"sub1": first, "sub2": second}
        slot_levels = {}
        slot_polarities = {}
        for slot, snap in (("sub1", snap1), ("sub2", snap2)):
            if slot in active_subs:
                try:
                    slot_levels[slot] = float(snap.get("level_db", 0.0))
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"AutoSub snapshot {slot} level must be numeric") from exc
            else:
                slot_levels[slot] = -80.0
            explicit = sub1_polarity if slot == "sub1" else sub2_polarity
            slot_polarities[slot] = explicit if explicit is not None else str(
                snap.get("polarity") or "normal")
    else:
        raise ValueError("AutoSub sub slot map must hold one slot or sub1/sub2")
    return autosub_explicit_knobs(
        start_state, output_key=output_key, channels=channels,
        sub_role_map=sub_role_map, slot_delays=slot_delays,
        slot_levels=slot_levels, slot_polarities=slot_polarities,
        crossover_hz=crossover_hz, main_highpass_enabled=main_highpass_enabled)
