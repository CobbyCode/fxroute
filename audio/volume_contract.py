# SPDX-License-Identifier: AGPL-3.0-only
"""Single canonical volume model for FXRoute output level."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable

from audio.system_volume import volume_percent_to_db

EXCLUDED_PRESETS = frozenset({"Direct"})


@dataclass(frozen=True)
class VolumeAction:
    op: str
    value: Any


@dataclass(frozen=True)
class VolumeState:
    preset: str
    loudness_enabled: bool
    volume_db: float
    master_percent: int
    dsp_guard_db: float = 0.0

    @property
    def loudness_in_path(self) -> bool:
        return loudness_in_path(self.preset, self.loudness_enabled)


def loudness_in_path(preset: str, loudness_enabled: bool) -> bool:
    return bool(loudness_enabled) and bool(preset) and preset not in EXCLUDED_PRESETS


def effective_db(state: VolumeState) -> float:
    loudness_part = float(state.volume_db) if state.loudness_in_path else 0.0
    return loudness_part + volume_percent_to_db(state.master_percent) + float(state.dsp_guard_db)


def canonical_percent(state: VolumeState) -> int:
    # The global master is the single user-facing volume.  Loudness volumeDb
    # is only the ISO-226 work point of the curve and never owns the master.
    return int(state.master_percent)


def apply_action(state: VolumeState, action: VolumeAction) -> VolumeState:
    if action.op == "set_volume_db":
        return replace(state, volume_db=float(action.value))
    if action.op == "set_master":
        return replace(state, master_percent=int(round(float(action.value))))
    if action.op == "set_guard":
        return replace(state, dsp_guard_db=float(action.value))
    if action.op == "set_preset":
        return replace(state, preset=str(action.value))
    if action.op == "set_loudness_enabled":
        return replace(state, loudness_enabled=bool(action.value))
    raise ValueError(f"unknown volume action: {action.op}")


def target_for(
    *,
    current: VolumeState,
    preset: str | None = None,
    loudness_enabled: bool | None = None,
    percent: int | float | None = None,
) -> VolumeState:
    """Project one transition onto the canonical volume state.

    Loudness on/off and preset switches never move the master and never
    rewrite the ISO-226 work point (``volume_db``).  Only an explicit
    ``percent`` (the footer slider) changes the master.
    """
    new_preset = current.preset if preset is None else str(preset)
    new_enabled = current.loudness_enabled if loudness_enabled is None else bool(loudness_enabled)
    new_master = int(current.master_percent)
    if percent is not None:
        new_master = max(0, min(100, int(round(float(percent)))))
    return VolumeState(
        preset=new_preset,
        loudness_enabled=new_enabled,
        volume_db=float(current.volume_db),
        master_percent=new_master,
        dsp_guard_db=float(current.dsp_guard_db),
    )


def plan_transition(start: VolumeState, target: VolumeState) -> list[VolumeAction]:
    actions: list[VolumeAction] = []
    if abs(float(target.volume_db) - float(start.volume_db)) > 1e-9:
        actions.append(VolumeAction("set_volume_db", float(target.volume_db)))
    if bool(target.loudness_enabled) != bool(start.loudness_enabled):
        actions.append(VolumeAction("set_loudness_enabled", bool(target.loudness_enabled)))
    if target.preset != start.preset:
        actions.append(VolumeAction("set_preset", target.preset))
    if int(target.master_percent) != int(start.master_percent):
        actions.append(VolumeAction("set_master", int(target.master_percent)))

    start_guard = float(start.dsp_guard_db)
    target_guard = float(target.dsp_guard_db)
    if abs(target_guard - start_guard) > 1e-9:
        action = VolumeAction("set_guard", target_guard)
        if target_guard < start_guard:
            actions.insert(0, action)
        else:
            actions.append(action)
    return actions


def partition_actions(
    actions: Iterable[VolumeAction], skip_ops: Iterable[str]
) -> tuple[list[VolumeAction], list[VolumeAction]]:
    skip = set(skip_ops)
    pre: list[VolumeAction] = []
    post: list[VolumeAction] = []
    seen_skip = False
    for action in actions:
        if action.op in skip:
            seen_skip = True
            continue
        (post if seen_skip else pre).append(action)
    return pre, post
