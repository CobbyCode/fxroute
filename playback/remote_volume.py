# SPDX-License-Identifier: AGPL-3.0-only

"""Remote-volume-to-master translation, shared by both Connect bridges.

Both controllers (spotifyd's reported Connect volume and qbzd's locked-mode
journal values) deliver absolute values on a controller scale that is
unrelated to the FXRoute master: at connect time the apps push the phone's
media volume (live-verified on .104, 2026-08-28 — Qobuz pushed 98% on
reactivate; Spotify pushed 42% -> 100% mid-session), and gesture scales have
arbitrary offsets. Neither the push nor an out-of-position gesture may move
the master.

:class:`RemoteVolumePickupTranslator` implements the motorized-fader
"pickup" contract for both bridges: the session's first value only anchors
the controller scale, a gesture takes over **only when it crosses the
current master level with a bounded overshoot**, and from the pickup on the
master tracks the controller value absolutely so both displays match.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

OWNER_POLL_INTERVAL_SECONDS = 0.05
logger = logging.getLogger(__name__)

# Maximum distance between a crossing gesture's observed value and the
# current master level for the pickup to adopt that value. Gestures reach
# the master in small steps (slider drags, hardware-key steps); app pushes
# leap far across it (42% -> 100% was live-observed on Spotify) and must
# never adopt.
MAX_PICKUP_OVERSHOOT = 10


class RemoteVolumePickupTranslator:
    """Motorized-fader pickup semantics for absolute remote volume values.

    Session lifecycle:

    * The first observation of a session (the app's connect-time push of the
      phone's media volume) only **anchors** the controller scale — it never
      writes the master.
    * While not picked up, a gesture writes only when it **crosses the
      current master level** with a bounded overshoot
      (``max_pickup_overshoot``): the write adopts the observed value, so the
      jump is bounded by the gesture step. Gestures that stay on the far side
      of the master, and app pushes that leap far across it (live-verified:
      Spotify pushed the phone's media volume 42% -> 100% mid-session), are
      ignored — adopting them would jump the master onto the controller's
      unrelated scale.
    * After the pickup the master tracks the controller value absolutely, so
      both displays show the same number.

    Absolute writes are idempotent: a failed write keeps the value pending
    for retry. Owner loss or a session (de)activation drops all state and
    re-arms the pickup.
    """

    def __init__(
        self,
        is_active: Callable[[], bool],
        apply_volume_value: Callable[[int], Awaitable[Any]],
        current_master: Callable[[], int],
        max_pickup_overshoot: int = MAX_PICKUP_OVERSHOOT,
    ) -> None:
        self.is_active = is_active
        self.apply_volume_value = apply_volume_value
        self.current_master = current_master
        self.max_pickup_overshoot = max_pickup_overshoot
        self._anchor: int | None = None
        self._last_value: int | None = None
        self._picked_up = False
        self._pending_value: int | None = None
        self._last_written: int | None = None
        self._activation_epoch = 0
        self._inflight_apply_task: asyncio.Task[Any] | None = None

    @property
    def pending(self) -> int | None:
        return self._pending_value

    @property
    def picked_up(self) -> bool:
        return self._picked_up

    def observe_activation(self) -> None:
        """Session (de)activation or ownership loss: drop all session state.

        The next observation re-anchors (the app re-pushes after activation)
        and the pickup must happen again before any write.
        """
        self._activation_epoch += 1
        self._anchor = None
        self._last_value = None
        self._picked_up = False
        self._pending_value = None
        self._last_written = None
        if self._inflight_apply_task is not None and not self._inflight_apply_task.done():
            self._inflight_apply_task.cancel()

    def submit(self, percent: int) -> bool:
        """Record an observation; ``True`` when a write became pending."""
        if not self.is_active():
            # Not the playback owner: nothing may be tracked or applied.
            self.observe_activation()
            return False
        if self._anchor is None:
            # Connect-time push: anchors the controller scale, never writes.
            self._anchor = percent
            self._last_value = percent
            return False
        previous = self._last_value if self._last_value is not None else self._anchor
        self._last_value = percent
        if self._picked_up:
            if percent == self._last_written and self._pending_value is None:
                # Repeat of the already-applied value (echo/re-push): no write.
                return False
            changed = self._pending_value != percent
            self._pending_value = percent
            return changed
        master = max(0, min(100, int(self.current_master())))
        crossed = min(previous, percent) <= master <= max(previous, percent)
        if not crossed:
            # The gesture stays on the far side of the master: adopting it
            # would jump the master onto the controller's unrelated scale.
            return False
        if abs(percent - master) > self.max_pickup_overshoot:
            # A leap far across the master (app pushing the phone's media
            # volume) is not a gesture: ignore it and stay unarmed.
            return False
        # Pickup: the gesture reaches the master level; adopt absolutely.
        self._picked_up = True
        self._pending_value = percent
        return True

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


async def drain_pending_loop(
    *,
    translator: RemoteVolumePickupTranslator,
    debounce_seconds: float,
    sleep: Callable[[float], Awaitable[None]],
    log_warning: Callable[..., None],
    log_prefix: str,
) -> None:
    """Shared debounce/drain loop for the remote-volume watches (M3).

    Coalesces rapid remote values behind one bounded debounce, retries a
    failed canonical write after a bounded delay so a transient failure
    never strands the latest intent, and drains a delta that arrives while
    the async write is still in flight. Cancellation propagates; any other
    error is the caller's to observe (the detached drain task must never
    hold an unretrieved exception).
    """
    while True:
        if debounce_seconds > 0:
            await sleep(debounce_seconds)
        try:
            await translator.flush()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Keep the latest intent pending and retry after a bounded
            # delay; a transient master-write failure must not strand it.
            log_warning("%s volume drain failed: %s", log_prefix, exc)
            await sleep(max(debounce_seconds, 0.5))
            continue
        # A new remote value can arrive while the async canonical write
        # above is still in flight. Do not leave that final delta stranded
        # behind the active drain task.
        if translator.pending is None:
            break
