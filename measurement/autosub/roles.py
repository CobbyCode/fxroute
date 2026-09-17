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

from audio.output_state import routing_for_device, validate_output_state
from audio.output_topology import OutputTopology, derive_topology

from .candidates import _auto_sub_clamped_delay

__all__ = [
    "autosub_topology_from_state",
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


def autosub_scan_knobs(
    start_state: dict, *, output_key: str, channels: int,
    sub_role_map: dict[str, str], delay_ms: float,
    sub1_alignment_ms: float | None, sub2_alignment_ms: float | None,
    active_subs: tuple[str, ...] | list[str],
    sub1_polarity: str | None, sub2_polarity: str | None,
    crossover_hz: int, main_highpass_enabled: bool,
) -> dict:
    """Translate one funnel candidate into complete role-keyed proposal knobs.

    Pure: logical optimizer slots become routed sub roles, start-relative and
    complete over every routed sub (inactive slots park at -80 dB, untouched
    fields carry the frozen start values). The caller wraps the result in an
    ``AutoSubProposal`` and stages it through the job's owner; the owner
    revalidates roles, revision and values before touching any runtime.
    """
    validated = validate_output_state(start_state)
    topology = require_autosub_topology(autosub_topology_from_state(
        validated, output_key=output_key, channels=channels))
    sub_roles = topology.sub_roles
    if not isinstance(sub_role_map, dict) or set(sub_role_map.values()) != set(sub_roles):
        raise ValueError("AutoSub sub slot map must cover every routed sub role exactly")
    if (not isinstance(active_subs, (tuple, list))
            or any(slot not in sub_role_map for slot in active_subs)):
        raise ValueError("AutoSub active subs must name mapped sub slots")
    processing = validated["modes"][validated["active_mode"]]["processing"]
    for role in sub_roles:
        if role not in processing:
            raise ValueError(f"Routed sub role {role} has no processing state")
    if len(sub_role_map) == 1:
        role = next(iter(sub_role_map.values()))
        delays = {role: _autosub_scan_delay(delay_ms, "delay")}
    elif set(sub_role_map) == {"sub1", "sub2"}:
        second_role = sub_role_map["sub2"]
        first = (sub1_alignment_ms if sub1_alignment_ms is not None else delay_ms)
        second = (sub2_alignment_ms if sub2_alignment_ms is not None
                  else processing[second_role]["alignment_ms"])
        delays = {sub_role_map["sub1"]: _autosub_scan_delay(first, "sub1 delay"),
                  second_role: _autosub_scan_delay(second, "sub2 delay")}
    else:
        raise ValueError("AutoSub sub slot map must hold one slot or sub1/sub2")
    active_roles = {sub_role_map[slot] for slot in active_subs}
    levels = {role: (float(processing[role]["level_db"]) if role in active_roles else -80.0)
              for role in sub_roles}
    explicit = {"sub1": sub1_polarity, "sub2": sub2_polarity}
    polarities = {}
    for slot, role in sub_role_map.items():
        chosen = explicit.get(slot)
        if chosen is None:
            polarities[role] = str(processing[role].get("polarity") or "normal")
        else:
            polarities[role] = "invert" if chosen == "invert" else "normal"
    try:
        frequency = int(crossover_hz)
    except (TypeError, ValueError) as exc:
        raise ValueError("AutoSub scan crossover must be numeric") from exc
    return {"sub_delays": delays, "sub_levels": levels,
            "sub_polarities": polarities,
            "bass": {"frequency_hz": frequency,
                     "main_highpass_enabled": bool(main_highpass_enabled)}}
