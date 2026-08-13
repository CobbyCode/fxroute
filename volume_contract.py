# SPDX-License-Identifier: AGPL-3.0-only
"""Single canonical volume model for FXRoute output level."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable

from system_volume import volume_db_to_percent, volume_percent_to_db

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
    if state.loudness_in_path:
        return volume_db_to_percent(state.volume_db)
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
    new_preset = current.preset if preset is None else str(preset)
    new_enabled = current.loudness_enabled if loudness_enabled is None else bool(loudness_enabled)
    owns = loudness_in_path(new_preset, new_enabled)
    enabling = bool(new_enabled) and not bool(current.loudness_enabled)
    entering_path = (not current.loudness_in_path) and owns
    leaving_path = current.loudness_in_path and not owns
    if percent is not None:
        perc = max(0, min(100, int(round(float(percent)))))
        stored_db = volume_percent_to_db(perc) if owns or enabling else float(current.volume_db)
    elif enabling or entering_path:
        perc = max(0, min(100, int(current.master_percent)))
        stored_db = volume_percent_to_db(perc)
    elif leaving_path:
        perc = volume_db_to_percent(current.volume_db)
        stored_db = float(current.volume_db)
    else:
        perc = canonical_percent(current)
        stored_db = float(current.volume_db)
    return VolumeState(
        preset=new_preset,
        loudness_enabled=new_enabled,
        volume_db=stored_db,
        master_percent=100 if owns else perc,
        dsp_guard_db=float(current.dsp_guard_db),
    )


def plan_transition(start: VolumeState, target: VolumeState) -> list[VolumeAction]:
    actions: list[VolumeAction] = []
    if abs(float(target.volume_db) - float(start.volume_db)) > 1e-9:
        actions.append(VolumeAction("set_volume_db", float(target.volume_db)))

    start_owns = start.loudness_in_path
    target_owns = target.loudness_in_path
    leaving_loudness = start_owns and not target_owns
    entering_loudness = (not start_owns) and target_owns

    def add_path_changes() -> None:
        if bool(target.loudness_enabled) != bool(start.loudness_enabled):
            actions.append(VolumeAction("set_loudness_enabled", bool(target.loudness_enabled)))
        if target.preset != start.preset:
            actions.append(VolumeAction("set_preset", target.preset))

    def add_master() -> None:
        if int(target.master_percent) != int(start.master_percent):
            actions.append(VolumeAction("set_master", int(target.master_percent)))

    if leaving_loudness:
        add_master()
        add_path_changes()
    elif entering_loudness:
        add_path_changes()
        add_master()
    else:
        add_path_changes()
        add_master()

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
