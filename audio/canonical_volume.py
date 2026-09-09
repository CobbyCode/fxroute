# SPDX-License-Identifier: AGPL-3.0-only

"""Canonical master volume: all mutations and reads of the global master.

Extracted verbatim from main.py (REFACTOR-017). Behavior is identical to
the previous inline implementation. This module owns every canonical-master
operation: pinning the MPV source volume to unity, the non-blocking safe
readback, the serialized clamped write with readback verification, and the
remote Connect apply with double-checked owner gating (including restoring
the pre-write master when the owner is lost mid-write).

It owns no playback, DSP, measurement or library state. The player, all
volume backends, the worker offload, the write lock and the playback-owner
resolution are injected through :class:`CanonicalVolumeDeps` (same *Deps
pattern as the other extracted modules). ``get_output_volume_safe`` is
additionally resolved through the injected dependency so the established
``main.*`` patch seams keep working. Log records use this module's logger.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CanonicalVolumeDeps:
    """Application services injected from main.py."""

    get_player_instance: Callable[[], Any]
    set_output_volume: Callable[[int], Any]
    get_status_volume: Callable[[int], int]
    get_output_volume_safe: Callable[[int], int]
    drain_worker: Callable[..., Awaitable[Any]]
    canonical_volume_write_lock: Callable[[], asyncio.Lock]
    resolve_playback_owner: Callable[[], Any]


@dataclass
class _CanonicalVolumeRuntime:
    deps: CanonicalVolumeDeps | None = None


_runtime = _CanonicalVolumeRuntime()


def configure_canonical_volume(deps: CanonicalVolumeDeps) -> None:
    """Bind the application services used by the canonical volume writes."""
    _runtime.deps = deps


def _deps() -> CanonicalVolumeDeps:
    if _runtime.deps is None:
        raise RuntimeError("Canonical volume runtime is not configured")
    return _runtime.deps


def ensure_local_source_volume() -> None:
    player_instance = _deps().get_player_instance()
    if not player_instance or not player_instance._running:
        return
    try:
        player_instance.set_volume(100)
    except Exception as exc:
        logger.warning("Failed to pin MPV source volume to 100%%: %s", exc)


def get_output_volume_safe(default: int = 100) -> int:
    # The global FXRoute master is the single user-facing volume.  Loudness
    # volumeDb is only the ISO-226 work point and must never be reported as
    # the volume.
    return _deps().get_status_volume(default)


async def _set_canonical_output_volume(volume: float | int) -> dict[str, Any]:
    """Apply the one global FXRoute master volume for every source.

    The footer slider (and the remote Connect bridges) drives only the master.
    Loudness volumeDb is the ISO-226 work point and is never touched here.
    Writes are serialized against concurrent canonical volume writes.
    """
    async with _deps().canonical_volume_write_lock():
        requested = max(0, min(100, int(round(float(volume)))))
        verified = await _deps().drain_worker(_deps().set_output_volume, requested)
        return {"volume": int(verified)}


async def _apply_remote_volume_value(
    volume_percent: int,
    *,
    owner: str | None = None,
    source_active: Callable[[], bool] | None = None,
) -> None:
    """Apply an absolute remote Connect volume to the canonical master.

    Called by the Connect bridges only after pickup (the gesture crossed the
    current master level), so the write adopts the controller value without
    any jump larger than the gesture step. The owner check is repeated while
    holding the canonical write lock, and an owner transition during the
    non-cancellable worker call restores the pre-write master.
    """
    deps = _deps()
    owner_is_current = lambda: (
        (owner is None or deps.resolve_playback_owner() == owner)
        and (source_active is None or source_active())
    )
    if not owner_is_current():
        return
    async with deps.canonical_volume_write_lock():
        if not owner_is_current():
            return
        current = deps.get_output_volume_safe()
        requested = max(0, min(100, int(round(float(volume_percent)))))
        try:
            await deps.drain_worker(deps.set_output_volume, requested)
        finally:
            if owner is not None and not owner_is_current():
                try:
                    await deps.drain_worker(deps.set_output_volume, current)
                except Exception as exc:
                    logger.warning("Failed to restore master after remote owner loss: %s", exc)
