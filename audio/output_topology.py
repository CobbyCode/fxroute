# SPDX-License-Identifier: AGPL-3.0-only
"""Logical output roles, independent of hardware port order and DSP settings."""

from __future__ import annotations

from dataclasses import dataclass

from audio.filter_banks import bank_definitions

MODES = ("stereo", "stereo-sub")
MAIN_ROLES = ("main_l", "main_r")
SUB_ROLES = ("sub_l", "sub_r", "sub1", "sub2")
WAYS = ("low", "low_mid", "mid", "high")
SPEAKER_ROLES = tuple(f"{side}_{way}" for side in ("left", "right") for way in WAYS)
MAX_CHANNELS = 32
_WAY_SETS = (("low", "high"), ("low", "mid", "high"), WAYS)


def roles_for_mode(mode: str, *, crossover_enabled: bool = False) -> tuple[str, ...]:
    if mode not in MODES:
        raise ValueError(f"Unsupported output mode: {mode}")
    if type(crossover_enabled) is not bool:
        raise ValueError("Crossover enabled must be a boolean")
    return (SPEAKER_ROLES if crossover_enabled else MAIN_ROLES) + (SUB_ROLES if mode == "stereo-sub" else ())


def validate_assignments(mode: str, assignments: object, *, crossover_enabled: bool = False) -> tuple[str, ...]:
    allowed = (*roles_for_mode(mode, crossover_enabled=crossover_enabled), "off")
    if not isinstance(assignments, (list, tuple)) or len(assignments) > MAX_CHANNELS:
        raise ValueError("Routing must contain at most 32 hardware assignments")
    if any(not isinstance(role, str) or role not in allowed for role in assignments):
        raise ValueError(f"Routing contains an invalid role for {mode}")
    return tuple(assignments)


@dataclass(frozen=True)
class OutputTopology:
    mode: str
    roles: tuple[str, ...]
    sub_roles: tuple[str, ...]
    sub_mode: str
    left_ways: tuple[str, ...]
    right_ways: tuple[str, ...]
    way_count: int | None
    issues: tuple[str, ...]
    crossover_enabled: bool = False

    @property
    def bank_ids(self) -> tuple[str, ...]:
        return ("global", *bank_definitions(self.roles))

    def require_activatable(self) -> None:
        if self.issues:
            raise ValueError("; ".join(self.issues))


def derive_topology(mode: str, assignments: object, *, channels: int | None = None,
                    crossover_enabled: bool = False) -> OutputTopology:
    """Derive active logical roles; repeated roles are physical fan-out only."""
    values = validate_assignments(mode, assignments, crossover_enabled=crossover_enabled)
    if channels is not None:
        if type(channels) is not int or not 0 <= channels <= MAX_CHANNELS:
            raise ValueError("Hardware channel count must be an integer from 0 to 32")
        values = values[:channels]
    active = set(values) - {"off"}
    roles = tuple(role for role in roles_for_mode(mode, crossover_enabled=crossover_enabled) if role in active)
    subs = tuple(role for role in SUB_ROLES if role in active)
    sub_mode = ("none" if not subs else "mono" if len(subs) == 1 else
                "stereo" if subs == ("sub_l", "sub_r") else "dual-mono" if len(subs) == 2 else "unsupported")
    issues = []
    if len(subs) > 2:
        issues.append("At most two distinct sub roles are supported")
    left = tuple(role for role in roles if role.startswith("left_"))
    right = tuple(role for role in roles if role.startswith("right_"))
    way_count = None
    if not crossover_enabled:
        if not set(MAIN_ROLES).issubset(active):
            issues.append("Stereo routing requires Main L and Main R")
    else:
        left_names = tuple(role.removeprefix("left_") for role in left)
        right_names = tuple(role.removeprefix("right_") for role in right)
        if left_names not in _WAY_SETS or right_names not in _WAY_SETS:
            issues.append("Crossover requires complete Low/High, Low/Mid/High, or Low/Low-Mid/Mid/High ways")
        elif left_names != right_names:
            issues.append("Left and Right crossover ways must match")
        else:
            way_count = len(left_names)
    return OutputTopology(mode, roles, subs, sub_mode, left, right, way_count, tuple(issues), crossover_enabled)
