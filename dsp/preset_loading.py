# SPDX-License-Identifier: AGPL-3.0-only

"""DSP preset loading with volume capture, locks and failure rollback.

Extracted verbatim from main.py (REFACTOR-016). Behavior is identical to
the previous inline implementation. This module owns the preset-load
coordination: the canonical volume state capture/restore pair, the failure
rollback (reload the previously committed preset, re-sync the runtime,
re-raise the original error), and the lock ordering (canonical volume write
lock first, then the DSP mutation lock).

It owns no playback, measurement or library state. The DSP-manager guard,
DSP runtime, volume backends, worker offload, locks and runtime sync are
injected through :class:`PresetLoadDeps` (same *Deps pattern as the other
extracted modules); only the volume contract dataclass is a leaf import.
Log records use this module's logger.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import audio.volume_contract as volume_contract

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PresetLoadDeps:
    """Application services injected from main.py."""

    require_dsp_manager: Callable[[], Any]
    get_dsp_runtime: Callable[[], Any]
    get_output_volume: Callable[[], int]
    set_output_volume: Callable[[int], Any]
    drain_worker: Callable[..., Awaitable[Any]]
    dsp_mutation_lock: Callable[[], asyncio.Lock]
    canonical_volume_write_lock: Callable[[], asyncio.Lock]
    sync_runtime: Callable[..., Awaitable[Any]]


@dataclass
class _PresetLoadRuntime:
    deps: PresetLoadDeps | None = None


_runtime = _PresetLoadRuntime()


def configure_preset_loading(deps: PresetLoadDeps) -> None:
    """Bind the application services used by the preset-load coordination."""
    _runtime.deps = deps


def _deps() -> PresetLoadDeps:
    if _runtime.deps is None:
        raise RuntimeError("Preset loading runtime is not configured")
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
    if (manager.get_active_preset() or "") != start.preset:
        if hasattr(manager, "active_preset"):
            manager.active_preset = start.preset
        elif getattr(manager, "state_store", None) is not None:
            manager.state_store.write(
                "active.json",
                {"schema": "fxroute.dsp.active", "version": 1, "preset": start.preset},
            )


async def _load_dsp_preset(
    preset_name: str, *, convolver_sample_rate_hz: int | None = None,
    _locks_held: bool = False, _rate_lock_held: bool = False,
) -> None:
    """Serialize preset loads against threaded DSP mutations.

    load_preset() also synchronizes global extras into the preset and is
    therefore not read-only: it must never run concurrently with a threaded
    IR/preset mutation.  Lock order: the canonical volume write lock is
    acquired first, then the DSP mutation lock.  Callers that already hold
    both locks pass ``_locks_held=True``.
    ``_rate_lock_held`` is forwarded to the runtime sync so a caller that
    already owns the measurement sample-rate session lock (the measurement
    entry) does not re-enter it.
    """
    volume_lock = None
    if not _locks_held:
        volume_lock = _deps().canonical_volume_write_lock()
        await volume_lock.acquire()
    try:
        if _locks_held:
            await _load_preset_locked(preset_name, convolver_sample_rate_hz=convolver_sample_rate_hz,
                                      _rate_lock_held=_rate_lock_held)
        else:
            async with _deps().dsp_mutation_lock():
                await _load_preset_locked(preset_name, convolver_sample_rate_hz=convolver_sample_rate_hz,
                                          _rate_lock_held=_rate_lock_held)
    finally:
        if volume_lock is not None:
            volume_lock.release()


async def _load_preset_locked(
    preset_name: str, *, convolver_sample_rate_hz: int | None = None,
    _rate_lock_held: bool = False,
) -> None:
    manager = _deps().require_dsp_manager()
    start = await _volume_state_for_manager(manager)
    try:
        await _deps().drain_worker(
            manager.load_preset,
            preset_name,
            convolver_sample_rate_hz=convolver_sample_rate_hz,
        )
        await _deps().sync_runtime(reason="native-dsp-preset-load",
                                   _rate_lock_held=_rate_lock_held)
    except Exception:
        try:
            if (manager.get_active_preset() or "") != start.preset:
                try:
                    await _deps().drain_worker(manager.load_preset, start.preset)
                except Exception:
                    logger.exception("Failed to reload previous preset after preset load failure")
                    if hasattr(manager, "active_preset"):
                        manager.active_preset = start.preset
            await _deps().sync_runtime(reason="native-dsp-preset-load-rollback",
                                       _rate_lock_held=_rate_lock_held)
        except Exception:
            logger.exception("Failed to restore previous preset after preset load failure")
        raise
