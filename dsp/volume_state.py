# SPDX-License-Identifier: AGPL-3.0-only

"""Canonical volume-state capture and restore around DSP extras changes.

Extracted from main.py (REFACTOR-016). The capture reads the master volume,
the DSP guard, the Loudness extras and the Global preset of the active
output mode; the restore puts master, guard and Loudness volume back after a
failed extras change. The DSP runtime, volume backends and worker offload are
injected through :class:`VolumeStateDeps`; only the volume contract
dataclass is a leaf import.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import audio.volume_contract as volume_contract


@dataclass(frozen=True)
class VolumeStateDeps:
    """Application services injected from main.py."""

    get_dsp_runtime: Callable[[], Any]
    get_output_volume: Callable[[], int]
    set_output_volume: Callable[[int], Any]
    drain_worker: Callable[..., Awaitable[Any]]


@dataclass
class _VolumeStateRuntime:
    deps: VolumeStateDeps | None = None


_runtime = _VolumeStateRuntime()


def configure_volume_state(deps: VolumeStateDeps) -> None:
    """Bind the application services used by the volume-state capture."""
    _runtime.deps = deps


def _deps() -> VolumeStateDeps:
    if _runtime.deps is None:
        raise RuntimeError("Volume state runtime is not configured")
    return _runtime.deps


async def _volume_state_for_manager(
    manager, *, live_master: int | None = None
) -> volume_contract.VolumeState:
    extras = {}
    preset = ""
    if manager:
        load_extras = getattr(manager, "load_global_extras", None)
        if callable(load_extras):
            extras = load_extras() or {}
        get_active = getattr(manager, "get_active_preset", None)
        if callable(get_active):
            preset = get_active() or ""
    loudness = extras.get("loudness") if isinstance(extras, dict) else {}
    loudness = loudness if isinstance(loudness, dict) else {}
    params = loudness.get("params") if isinstance(loudness.get("params"), dict) else {}
    enabled = bool(loudness.get("enabled"))
    if live_master is None:
        live_master = int(await _deps().drain_worker(_deps().get_output_volume))
    guard = 0.0
    dsp_runtime = _deps().get_dsp_runtime()
    if dsp_runtime is not None:
        try:
            guard = float(dsp_runtime.snapshot().get("output_gain_db") or 0.0)
        except Exception:
            guard = 0.0
    return volume_contract.VolumeState(
        preset=preset,
        loudness_enabled=enabled,
        volume_db=float(params.get("volumeDb") or 0.0),
        master_percent=int(live_master),
        dsp_guard_db=guard,
    )


async def _restore_volume_state(manager, start: volume_contract.VolumeState) -> None:
    await _deps().drain_worker(_deps().set_output_volume, int(start.master_percent))
    dsp_runtime = _deps().get_dsp_runtime()
    if dsp_runtime is not None and dsp_runtime.snapshot().get("active"):
        await dsp_runtime.set_output_gain_db(float(start.dsp_guard_db))
    if not manager:
        return
    extras = copy.deepcopy(manager.load_global_extras())
    extras.setdefault("loudness", {}).setdefault("params", {})["volumeDb"] = float(start.volume_db)
    extras.setdefault("loudness", {})["enabled"] = bool(start.loudness_enabled)
    save = getattr(manager, "save_global_extras", None)
    if callable(save):
        save(extras)
