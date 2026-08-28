# SPDX-License-Identifier: AGPL-3.0-only

"""Remote-volume-to-master translation, shared by both Connect bridges.

Two controller semantics exist and each bridge uses the matching translator:

* **spotifyd** reports its Connect volume on a controller scale whose offset
  against the master is stable, so the controller and the master stay in step
  once aligned: :class:`RemoteVolumeDeltaTranslator` anchors the first value
  of a session and applies later values as relative deltas.
* **Qobuz (qbzd, ``volume_mode=locked``)** has no shared scale at all: the
  Qobuz app maintains a persistent per-renderer volume slider and pushes its
  value on Connect activation and on every gesture (live-verified on .104,
  2026-08-28: 0.01-step slider drags, the activation push equals the last
  slider position). Delta semantics preserve a permanent controller-to-master
  offset, so the two displays never match. The Qobuz bridge therefore uses
  :class:`RemoteVolumeAbsoluteTranslator`: every observed value is an
  absolute renderer-volume intent and the master adopts it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

OWNER_POLL_INTERVAL_SECONDS = 0.05
logger = logging.getLogger(__name__)


class RemoteVolumeDeltaTranslator:
    """Coalesce remote volume observations into one canonical master write.

    ``submit`` records observations and computes deltas against the running
    anchor; ``flush`` applies the latest pending delta exactly once. Bursts
    (one observation per drag/button step) therefore apply the gesture's net
    delta instead of every intermediate step.
    """

    def __init__(
        self,
        is_active: Callable[[], bool],
        apply_volume_delta: Callable[[int], Awaitable[Any]],
    ) -> None:
        self.is_active = is_active
        self.apply_volume_delta = apply_volume_delta
        self._anchor: int | None = None
        self._pending_delta: int | None = None
        self._pending_observed = False
        self._activation_epoch = 0
        self._inflight_apply_task: asyncio.Task[Any] | None = None

    @property
    def pending(self) -> int | None:
        return self._pending_delta

    @property
    def anchored(self) -> bool:
        return self._anchor is not None

    def observe_activation(self) -> None:
        """Session (re)activation or ownership loss: drop anchor and pending.

        The next observed value re-anchors the controller scale without
        touching the master, so connect-time sync pushes, restored session
        volumes and daemon restarts can never leak into the master as user
        intent.
        """
        self._activation_epoch += 1
        self._anchor = None
        self._pending_delta = None
        self._pending_observed = False
        if self._inflight_apply_task is not None and not self._inflight_apply_task.done():
            self._inflight_apply_task.cancel()

    def submit(self, percent: int) -> bool:
        """Record an observation; ``True`` when it produced a pending delta.

        The anchor stays fixed until :meth:`flush` applies the pending delta,
        so a drag burst nets against the pre-gesture scale (100->99->98 yields
        -2, not the last step's -1).
        """
        if not self.is_active():
            # Not the playback owner: nothing may be tracked or applied, and
            # the next active observation re-anchors from scratch.
            self.observe_activation()
            return False
        if self._anchor is None:
            self._anchor = percent
            return False
        self._pending_observed = True
        if percent == self._anchor:
            # The gesture returned to the anchored value: net delta is zero.
            self._pending_delta = None
            return False
        self._pending_delta = percent - self._anchor
        return True

    async def flush(self) -> None:
        if self._pending_delta is None:
            return
        if not self.is_active():
            # The owner changed inside the debounce window: the pending delta
            # is stale and must never touch the master anymore.  Reset through
            # the activation path so an in-flight write is cancelled too.
            self.observe_activation()
            return
        delta = self._pending_delta
        self._pending_delta = None
        self._pending_observed = False
        self._anchor += delta
        epoch = self._activation_epoch
        apply_task = asyncio.create_task(
            self.apply_volume_delta(delta),
            name="remote-volume-apply",
        )
        self._inflight_apply_task = apply_task
        owner_monitor = asyncio.create_task(
            self._wait_for_owner_loss(epoch),
            name="remote-volume-owner-monitor",
        )
        try:
            done, _ = await asyncio.wait(
                (apply_task, owner_monitor),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if owner_monitor in done and owner_monitor.result() and not apply_task.done():
                self.observe_activation()
            await apply_task
        except asyncio.CancelledError:
            if epoch != self._activation_epoch or not self.is_active():
                if epoch == self._activation_epoch:
                    self.observe_activation()
                return
            self._restore_failed_delta(delta)
            raise
        except Exception as exc:
            if getattr(exc, "volume_write_applied", False):
                # A failed verification after wpctl accepted the set is already
                # committed; retrying the delta would apply the gesture twice.
                logger.warning("Remote volume write committed but readback failed: %s", exc)
                if epoch != self._activation_epoch or not self.is_active():
                    if epoch == self._activation_epoch:
                        self.observe_activation()
                return
            if epoch != self._activation_epoch or not self.is_active():
                if epoch == self._activation_epoch:
                    self.observe_activation()
                return
            self._restore_failed_delta(delta)
            raise
        finally:
            if not apply_task.done():
                apply_task.cancel()
            if not owner_monitor.done():
                owner_monitor.cancel()
            await asyncio.gather(apply_task, owner_monitor, return_exceptions=True)
            if self._inflight_apply_task is apply_task:
                self._inflight_apply_task = None

        if epoch != self._activation_epoch or not self.is_active():
            if epoch == self._activation_epoch:
                self.observe_activation()

    def _restore_failed_delta(self, delta: int) -> None:
        """Rebase a failed write and any newer observation onto the old anchor."""
        if self._anchor is None:
            return
        self._anchor -= delta
        if self._pending_observed and self._pending_delta is not None:
            restored = self._pending_delta + delta
            self._pending_delta = restored or None
        else:
            self._pending_delta = delta
        self._pending_observed = False

    async def _wait_for_owner_loss(self, epoch: int) -> bool:
        """Return when ownership disappears during an asynchronous write."""
        while epoch == self._activation_epoch:
            try:
                if not self.is_active():
                    return True
            except Exception:
                return False
            await asyncio.sleep(OWNER_POLL_INTERVAL_SECONDS)
        return False


class RemoteVolumeAbsoluteTranslator:
    """Coalesce remote volume observations into absolute master writes.

    Every observed value is an absolute renderer-volume intent: ``flush``
    writes the latest pending value instead of a delta, so the controller
    display and the master show the same number (Spotify-Connect-like sync).
    Absolute writes are idempotent, so a failed write simply keeps the value
    pending for retry. Repeats of the already-applied value are dropped, while
    a fresh activation always re-adopts the controller value.
    """

    def __init__(
        self,
        is_active: Callable[[], bool],
        apply_volume_value: Callable[[int], Awaitable[Any]],
    ) -> None:
        self.is_active = is_active
        self.apply_volume_value = apply_volume_value
        self._pending_value: int | None = None
        self._last_written: int | None = None
        self._activation_epoch = 0
        self._inflight_apply_task: asyncio.Task[Any] | None = None

    @property
    def pending(self) -> int | None:
        return self._pending_value

    def observe_activation(self) -> None:
        """Session (de)activation or ownership loss: drop pending state.

        ``_last_written`` is cleared too, so the first value of a fresh session
        is always adopted even when it repeats the previous session's value —
        connect-time adoption is what keeps the two displays aligned.
        """
        self._activation_epoch += 1
        self._pending_value = None
        self._last_written = None
        if self._inflight_apply_task is not None and not self._inflight_apply_task.done():
            self._inflight_apply_task.cancel()

    def submit(self, percent: int) -> bool:
        """Record an observation; ``True`` when a write became pending.

        A repeat of the already-written value (echo/poll push) produces no
        write; anything else replaces the pending value so a drag burst
        collapses to its latest position.
        """
        if not self.is_active():
            # Not the playback owner: nothing may be tracked or applied.
            self.observe_activation()
            return False
        if percent == self._last_written and self._pending_value is None:
            return False
        changed = self._pending_value != percent
        self._pending_value = percent
        return changed

    async def flush(self) -> None:
        if self._pending_value is None:
            return
        if not self.is_active():
            # The owner changed inside the debounce window: the pending value
            # is stale and must never touch the master anymore.
            self.observe_activation()
            return
        value = self._pending_value
        self._pending_value = None
        epoch = self._activation_epoch
        apply_task = asyncio.create_task(
            self.apply_volume_value(value),
            name="remote-volume-apply",
        )
        self._inflight_apply_task = apply_task
        owner_monitor = asyncio.create_task(
            self._wait_for_owner_loss(epoch),
            name="remote-volume-owner-monitor",
        )
        try:
            done, _ = await asyncio.wait(
                (apply_task, owner_monitor),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if owner_monitor in done and owner_monitor.result() and not apply_task.done():
                self.observe_activation()
            await apply_task
            if epoch == self._activation_epoch and self.is_active():
                self._last_written = value
        except asyncio.CancelledError:
            if epoch != self._activation_epoch or not self.is_active():
                if epoch == self._activation_epoch:
                    self.observe_activation()
                return
            # Still the owner: the write was interrupted before it committed;
            # keep the value pending so the drain retries it.
            self._pending_value = value
            raise
        except Exception:
            # Absolute writes are idempotent: a failed write (or a failed
            # readback after a committed write) is retried with the same
            # value, which can never double-apply a gesture.
            if epoch != self._activation_epoch or not self.is_active():
                if epoch == self._activation_epoch:
                    self.observe_activation()
                return
            self._pending_value = value
            raise
        finally:
            if not apply_task.done():
                apply_task.cancel()
            if not owner_monitor.done():
                owner_monitor.cancel()
            await asyncio.gather(apply_task, owner_monitor, return_exceptions=True)
            if self._inflight_apply_task is apply_task:
                self._inflight_apply_task = None

    async def _wait_for_owner_loss(self, epoch: int) -> bool:
        """Return when ownership disappears during an asynchronous write."""
        while epoch == self._activation_epoch:
            try:
                if not self.is_active():
                    return True
            except Exception:
                return False
            await asyncio.sleep(OWNER_POLL_INTERVAL_SECONDS)
        return False
