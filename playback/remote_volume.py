# SPDX-License-Identifier: AGPL-3.0-only

"""Remote-volume-to-master delta translation, shared by both Connect bridges.

Qobuz Connect (qbzd, ``volume_mode=locked``) and spotifyd both deliver remote
phone volume as absolute values on the controller's own scale. Live-verified on
.104 (2026-08-21): when the phone activates the Connect device, its app pushes
its own media volume as an absolute remote SetVolume a few seconds later. That
value anchors the controller scale and must never move the FXRoute master;
every later value is a user gesture whose delta against the running anchor is
applied to the master.
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
