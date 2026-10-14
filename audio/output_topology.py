# SPDX-License-Identifier: AGPL-3.0-only
"""Logical output roles, independent of hardware port order and DSP settings."""

from __future__ import annotations

from dataclasses import dataclass

from audio.filter_banks import bank_definitions

MODES = ("stereo", "stereo-sub", "surround")
MAIN_ROLES = ("main_l", "main_r")
SURROUND_SPEAKERS = ("center", "side_l", "side_r", "rear_l", "rear_r")
SURROUND_POSITIONS = ("FL", "FR", "FC", "LFE", "SL", "SR", "BL", "BR")
# Speaker groups of Surround bass management, in display order.
SURROUND_GROUPS = ("front", "center", "side", "rear")
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
    fronts = SPEAKER_ROLES if crossover_enabled else MAIN_ROLES
    if mode == "surround":
        return fronts + SURROUND_SPEAKERS + ("sub1", "sub2")
    return fronts + (SUB_ROLES if mode == "stereo-sub" else ())


def side_for_role(role: str) -> str:
    """Input side a role belongs to (sub1/sub2 sum both sides, main is L/R)."""
    if role in ("left_low", "main_l", "sub_l") or str(role).startswith("left_"):
        return "left"
    return "right"


def surround_group(role: str) -> str:
    """Surround bass-management group of a speaker role (fronts include ways)."""
    if role in MAIN_ROLES or role in SPEAKER_ROLES:
        return "front"
    if role in SURROUND_SPEAKERS:
        return role.split("_")[0]
    raise ValueError(f"Role {role} belongs to no Surround speaker group")


def dormant_roles(mode: str, *, crossover_enabled: bool) -> tuple[str, ...]:
    """Roles a mode keeps stored but never applies while its crossover is off.

    The speaker ways are the crossover path. Turning the crossover off returns
    the front ports to Main and leaves the way assignments stored, so the
    configured multi-way system comes back on the same ports; until then those
    ports are dormant and stay silent.
    """
    if type(crossover_enabled) is not bool:
        raise ValueError("Crossover enabled must be a boolean")
    active = set(roles_for_mode(mode))
    if crossover_enabled:
        return ()
    return tuple(role for role in roles_for_mode(mode, crossover_enabled=True) if role not in active)


def supported_bank_ids() -> tuple[str, ...]:
    """Every filterbank id any output mode and crossover setting can define.

    A stored library is a bank only if a mode can define it, so a stray
    directory in the preset root cannot claim one.
    """
    ids = set()
    for mode in MODES:
        for crossover_enabled in (False, True):
            ids.update(bank_definitions(roles_for_mode(mode, crossover_enabled=crossover_enabled)))
    return tuple(sorted(ids))


def validate_assignments(mode: str, assignments: object, *, crossover_enabled: bool = False,
                         allow_dormant: bool = False) -> tuple[str, ...]:
    """Check routing roles against the mode's domain.

    ``allow_dormant`` accepts the roles of the other crossover domain as well:
    stored routing may keep dormant crossover-path assignments while the
    crossover is off, and only the active routing reads them as Off.
    """
    if type(allow_dormant) is not bool:
        raise ValueError("Dormant assignment allowance must be a boolean")
    allowed = {*roles_for_mode(mode, crossover_enabled=crossover_enabled)}
    if allow_dormant:
        allowed |= {*roles_for_mode(mode, crossover_enabled=not crossover_enabled)}
    if not isinstance(assignments, (list, tuple)) or len(assignments) > MAX_CHANNELS:
        raise ValueError("Routing must contain at most 32 hardware assignments")
    if any(not isinstance(role, str) or role not in (*allowed, "off") for role in assignments):
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
    if mode == "surround" and "sub2" in subs and "sub1" not in subs:
        issues.append("Surround Sub 2 requires Sub 1")
    if mode == "surround":
        for label, pair in (("Side", ("side_l", "side_r")), ("Rear", ("rear_l", "rear_r"))):
            if len(active & set(pair)) == 1:
                issues.append(f"Surround {label} requires {label} L and {label} R")
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
