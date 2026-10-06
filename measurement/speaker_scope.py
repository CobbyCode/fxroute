# SPDX-License-Identifier: AGPL-3.0-only
"""Which speakers a calibration sweep on one area measures (pure planning)."""

from __future__ import annotations

from collections.abc import Sequence

from audio.filter_banks import bank_definitions
from audio.output_topology import SUB_ROLES, SURROUND_GROUPS, surround_group
from dsp.native_config import role_side


def plan_area(mode: str, bank_id: str, roles: Sequence[str]) -> dict:
    """Resolve which speakers a sweep run on one area measures.

    Returns ``group`` (the Surround group, else None), ``members`` and ``units``;
    a unit is one take: the roles that play together and the sweep side that
    excites them. Subwoofers are never measured (Auto Sub Optimize owns them).
    """
    active = [role for role in roles if role not in SUB_ROLES]
    if bank_id in SUB_ROLES or bank_id.startswith("sub"):
        raise ValueError("Subwoofers are handled by Auto Sub Optimize")
    if mode == "surround":
        group = _surround_group(bank_id, active)
        members = [role for role in active if surround_group(role) == group]
        return {"group": group, "members": members, "units": _side_units(members)}
    if bank_id == "global":
        members = active
    else:
        definition = bank_definitions(active).get(bank_id)
        if definition is None:
            raise ValueError(f"Bank {bank_id} is not an active speaker bank")
        members = list(definition["roles"])
    return {"group": None, "members": members, "units": _side_units(members)}


def _surround_group(bank_id: str, roles: Sequence[str]) -> str:
    if bank_id in SURROUND_GROUPS:
        return bank_id
    definition = bank_definitions(roles).get(bank_id)
    if definition is None:
        raise ValueError(f"Bank {bank_id} is not an active speaker bank")
    return surround_group(definition["roles"][0])


def _side_units(members: Sequence[str]) -> list[dict]:
    """Group roles into takes: all left roles, all right roles, a mono role alone."""
    units: list[dict] = []
    for side in ("left", "right"):
        side_roles = [role for role in members if role_side(role) == side]
        if side_roles:
            units.append({"roles": side_roles, "channel": side, "label": side})
    mono_roles = [role for role in members if role_side(role) == "mono"]
    if mono_roles:
        units.append({"roles": mono_roles, "channel": "left", "label": mono_roles[0]})
    return units
