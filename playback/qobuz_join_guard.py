# SPDX-License-Identifier: AGPL-3.0-only

"""Restore the user's Qobuz intent after a Connect session (re)join.

The qbzd fork persists no session state. Every daemon start ends with
``Renderer join complete`` and the session then hands the renderer the queue,
repeat mode and current track it holds, which the fork loads and plays
(live-verified on .104 after a power cycle: join 23:34:38, ``New track from
renderer: 198555`` 23:34:39, audio from 23:34:42 seeking to the position from
before the restart) although FXRoute showed repeat Off and the renderer had
reported Paused before the restart. Neither fact survives in qbzd itself: a
repeat mode FXRoute wrote existed only in the running daemon's queue, and the
session's own copy (the differing value) wins the re-join.

FXRoute therefore restores the intent it last observed for as long as a fresh
join is being reconciled:

* the stored repeat mode is re-applied absolutely whenever the daemon does not
  report it (the UI control is a cycle, so a restored state cannot be expressed
  as "one more step"),
* while the stored intent says playback was stopped, qbzd is paused again and
  the resulting Playing edge is not claimed, so an autoplay neither reaches
  the output nor takes ownership from the previous source,
* observations made during that window are not recorded as intent: they are
  the session's values, and recording them would overwrite the very intent the
  restore works from.

An explicit FXRoute control during the window ends it (``note_user_intent``):
a user who presses loop or play has decided, and their value is authoritative
from then on (a play that FXRoute itself starts must never be paused as an
"autoplay"). Without a stored intent the join is left completely alone.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from playback.qobuz_intent import QobuzIntent, normalize_loop

logger = logging.getLogger(__name__)

# The session hands the renderer its queue right after the join and the fork
# then plays the session's track once its download finished (live-verified on
# .104: join +0.0s, track push +0.4s, audio +3.2s for a 64MB HiRes track).
# The repeat restore covers the queue adoption; the autoplay suppression covers
# the playback start, which a slow download can delay, so it gets its own
# shorter window: every second of it is time in which a play the user starts
# in the Qobuz app right after a re-join would be paused once.
LOOP_RESTORE_WINDOW_SECONDS = 30.0
AUTOPLAY_SUPPRESS_WINDOW_SECONDS = 20.0


@dataclass
class QobuzJoinGuardDependencies:
    """Live services the join guard needs."""

    load_intent: Callable[[], QobuzIntent | None]
    record_intent: Callable[..., Any]
    read_loop: Callable[[], Awaitable[str | None]]
    apply_loop: Callable[[str], Awaitable[Any]]
    pause_qobuz: Callable[[], Awaitable[Any]]


class QobuzJoinGuard:
    """Reconcile the stored Qobuz intent with a Connect session (re)join."""

    def __init__(
        self,
        deps: QobuzJoinGuardDependencies,
        *,
        window_seconds: float = LOOP_RESTORE_WINDOW_SECONDS,
        autoplay_window_seconds: float = AUTOPLAY_SUPPRESS_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._deps = deps
        self._window_seconds = float(window_seconds)
        self._autoplay_window_seconds = float(autoplay_window_seconds)
        self._clock = clock
        self._armed_until: float | None = None
        self._autoplay_until: float | None = None

    def note_session_join(self) -> None:
        """Arm the reconcile windows for a fresh Connect session (re)join."""
        now = self._clock()
        self._armed_until = now + self._window_seconds
        self._autoplay_until = now + self._autoplay_window_seconds

    def note_user_intent(self) -> None:
        """End the windows: an explicit user control is authoritative now."""
        self._armed_until = None
        self._autoplay_until = None

    def window_active(self) -> bool:
        """Return whether a join is currently being reconciled."""
        return self._armed_until is not None and self._clock() < self._armed_until

    def autoplay_window_active(self) -> bool:
        """Return whether a join autoplay may still be starting."""
        return self._autoplay_until is not None and self._clock() < self._autoplay_until

    def reconcile_observed(self, *, loop: str, playing: bool) -> None:
        """Record an observed daemon state as the user's intent.

        Nothing is recorded while a join is being reconciled: those values are
        the session's, and the restore needs the pre-join intent to work from.
        """
        if self.window_active():
            return
        self._deps.record_intent(loop=loop, playing=playing)

    async def _pause_qobuz(self, reason: str) -> bool:
        try:
            await self._deps.pause_qobuz()
            return True
        except Exception as exc:
            logger.warning("Qobuz join restore could not pause qbzd (%s): %s", reason, exc)
            return False

    async def reconcile_join(self) -> dict:
        """Re-apply the stored repeat mode and stop a join autoplay.

        Runs once per watch pass for as long as the window is open, so a
        session value that arrives after the join is still corrected. The
        repeat write only happens on a real mismatch, and the pre-emptive
        pause (before the session's track starts playing) keeps the autoplay
        from reaching the output at all.
        """
        if not self.window_active():
            return {"reconciled": False, "reason": "window-closed"}
        intent = self._deps.load_intent()
        if intent is None:
            return {"reconciled": False, "reason": "no-intent"}
        loop_applied = False
        try:
            observed = normalize_loop(await self._deps.read_loop())
        except Exception as exc:
            logger.warning("Qobuz repeat readback during join failed: %s", exc)
            observed = None
        if observed != intent.loop:
            try:
                await self._deps.apply_loop(intent.loop)
                loop_applied = True
            except Exception as exc:
                logger.warning("Qobuz repeat restore after join failed: %s", exc)
        paused = None
        if not intent.playing and self.autoplay_window_active():
            paused = await self._pause_qobuz("join-autoplay")
        return {"reconciled": True, "loop": intent.loop, "loop_applied": loop_applied,
                "playing": intent.playing, "paused": paused}

    async def reconcile_join_autoplay(self) -> bool:
        """Stop a join autoplay and suppress its ownership claim.

        Returns ``True`` when this Playing edge is the daemon's own session-join
        autoplay and must not claim playback. Every edge inside the window is
        covered: the user has not started playback since the join, so none of
        them is a Connect takeover. A play the user starts after the window (or
        any FXRoute-initiated control, which ends the window) claims as before.
        """
        if not self.autoplay_window_active():
            return False
        intent = self._deps.load_intent()
        if intent is None or intent.playing:
            return False
        await self._pause_qobuz("join-autoplay-edge")
        return True
