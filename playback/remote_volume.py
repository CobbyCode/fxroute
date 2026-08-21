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

from typing import Any, Awaitable, Callable


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
        self._anchor = None
        self._pending_delta = None

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
            # is stale and must never touch the master anymore.
            self._pending_delta = None
            return
        delta = self._pending_delta
        self._pending_delta = None
        self._anchor += delta
        await self.apply_volume_delta(delta)
