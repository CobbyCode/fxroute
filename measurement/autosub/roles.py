# SPDX-License-Identifier: AGPL-3.0-only
"""AutoSub routing adapter: derived topology to optimizer path and roles.

The routing is the only topology authority. These helpers translate a derived
:class:`~audio.output_topology.OutputTopology` into everything the AutoSub
optimizers need — which legacy optimizer path a system maps to, the engine
output indices of the sub roles, and the mute mask for a Main-only reference
sweep — so the runners never read legacy mode strings or hard-coded output 3/4
indices again.

Mixed Sub 1/R (and any two sub roles other than a true ``sub_l``/``sub_r``
pair) is dual-mono: it uses the dual-sub optimizer path, never a stereo split.
In Crossover, Main is the sum of all that speaker's ways, so the Main-only
reference excites every way of the side while the subs are muted.

This module is pure: no files, no hardware, no runtime, no global state.
"""

from __future__ import annotations

from audio.output_state import routing_for_device, validate_output_state
from audio.output_topology import OutputTopology, derive_topology

__all__ = [
    "autosub_topology_from_state",
    "main_roles_for_side",
    "optimizer_path",
    "require_autosub_topology",
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
