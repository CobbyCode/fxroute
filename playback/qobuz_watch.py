# SPDX-License-Identifier: AGPL-3.0-only
"""Qobuz (qbzd) external-renderer claim watcher.

Owns the bounded watch loop that observes the qbzd control-plane state and
claims FXRoute playback ownership on a Playing rising edge (Qobuz Connect
started playback on another device). It reacts to state transitions rather
than polling transport actions, coalesces duplicate claims, and backs off on
repeat failures so a broken daemon cannot busy-loop the event loop.

No imports from ``main``: the claim entry point and the state accessors are
injected by the composition root.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 2.0
CLAIM_BACKOFF_BASE_SECONDS = 1.0
CLAIM_BACKOFF_MAX_SECONDS = 30.0


@dataclass
class QobuzWatchDependencies:
    """Live services the qbzd claim watcher needs."""

    get_playback_state: Callable[[], Any]
    broadcast_qobuz_state: Callable[..., Any]
    is_qobuz_playback_active: Callable[[dict | None], bool]
    claim_qobuz_playback: Callable[[str], Any]


class QobuzPlayerWatch:
    """Single owner of the qbzd watch loop and its claim backoff."""

    def __init__(self, deps: QobuzWatchDependencies) -> None:
        self._deps = deps
        self.watch_task: asyncio.Task | None = None
        self._backoff_seconds = 0.0

    async def run_watch_loop(self) -> None:
        logger.info("Qobuz qbzd claim watch loop entered")
        was_playing = False
        while True:
            try:
                state = await self._deps.broadcast_qobuz_state()
                playing = bool(self._deps.is_qobuz_playback_active(state))
                if playing and not was_playing:
                    # Rising edge: qbzd started playing externally (Qobuz
                    # Connect). Claim ownership; the claim is a no-op when
                    # qobuz already owns playback, and paused/stopped events
                    # never reclaim.
                    await self._deps.claim_qobuz_playback("qbzd-playing")
                    self._backoff_seconds = 0.0
                was_playing = playing
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Qobuz qbzd claim watch pass failed: %s", exc)
                if self._backoff_seconds < CLAIM_BACKOFF_MAX_SECONDS:
                    self._backoff_seconds = min(
                        CLAIM_BACKOFF_MAX_SECONDS,
                        self._backoff_seconds + CLAIM_BACKOFF_BASE_SECONDS,
                    )
            await asyncio.sleep(
                POLL_INTERVAL_SECONDS + self._backoff_seconds
            )

    async def stop(self) -> None:
        if self.watch_task is not None and not self.watch_task.done():
            self.watch_task.cancel()
        self.watch_task = None
