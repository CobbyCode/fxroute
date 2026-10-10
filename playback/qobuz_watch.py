# SPDX-License-Identifier: AGPL-3.0-only
"""Qobuz (qbzd) external-renderer claim watcher.

Owns the bounded watch loop that observes the qbzd control-plane state and
claims FXRoute playback ownership on a Playing rising edge (Qobuz Connect
started playback on another device). It coalesces duplicate claims and backs
off on repeat failures so a broken daemon cannot busy-loop the event loop.

Transport source: qbzd exposes an SSE endpoint (``GET /api/events``, the same
stream the ``qbzd watch`` CLI consumes) that documents
``PlaybackStateChanged``/``TrackStarted`` events. Live verification on qbzd
2.0.2 (.104) shows that endpoint holding the connection open but emitting no
events for pause/play or volume transitions, so it is not stable enough to be
the ownership-claim source yet. This watcher therefore reads the qbzd state
via the injected transport accessor (server-side HTTP read, never a UI poll)
on a bounded 2s interval, shortened to 250ms while a line is selected. UI
broadcast and meter setup run separately so they cannot starve these reads. It is structured so that swapping the state accessor
for a parsed ``/api/events`` stream is a drop-in change once qbzd emits events
reliably.

Claiming is a no-op when qobuz already owns App playback. A changed source
generation or a selected line also triggers a claim: a line switch can pause
and resume between two polls. Paused/stopped states never claim.

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
LINE_POLL_INTERVAL_SECONDS = 0.25
CLAIM_BACKOFF_BASE_SECONDS = 1.0
CLAIM_BACKOFF_MAX_SECONDS = 30.0


async def read_claim_state() -> dict:
    """Read transport only; queue, UI volume and meter work cannot delay it."""
    from streaming.qobuz import backend, normalize_state
    if not backend.qbzd_installed():
        return {"available": False, "status": "Stopped"}
    doc = await backend.get_json(backend.default_base_url(), "/api/playback", timeout=1.0)
    return {"available": doc is not None,
            "status": normalize_state((doc or {}).get("state"), (doc or {}).get("is_playing"))}


@dataclass
class QobuzWatchDependencies:
    """Live services the qbzd claim watcher needs."""

    get_playback_state: Callable[[], Any]
    broadcast_qobuz_state: Callable[..., Any]
    is_qobuz_playback_active: Callable[[dict | None], bool]
    claim_qobuz_playback: Callable[[str], Any]
    # Read transport without peak setup delaying the ownership claim.
    read_qobuz_state: Callable[..., Any] | None = None


class QobuzPlayerWatch:
    """Single owner of the qbzd watch loop and its claim backoff."""

    def __init__(self, deps: QobuzWatchDependencies) -> None:
        self._deps = deps
        self.watch_task: asyncio.Task | None = None
        self._backoff_seconds = 0.0
        self._broadcast_task: asyncio.Task | None = None

    async def _broadcast(self, state: dict) -> None:
        try:
            await self._deps.broadcast_qobuz_state(state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Qobuz state broadcast failed: %s", exc)

    async def run_watch_loop(self) -> None:
        logger.info("Qobuz qbzd claim watch loop entered")
        was_playing = False
        observed_source_generation = None
        while True:
            try:
                reader = self._deps.read_qobuz_state
                state = await (reader() if reader is not None else self._deps.broadcast_qobuz_state())
                playback = self._deps.get_playback_state()
                source_generation = getattr(playback, "source_generation", None)
                source_changed = (observed_source_generation is not None
                                  and source_generation != observed_source_generation)
                line_selected = getattr(playback, "source_mode", None) in {
                    "bluetooth-input", "external-input", "stdin-input",
                }
                playing = bool(self._deps.is_qobuz_playback_active(state))
                if playing and (not was_playing or source_changed or line_selected):
                    # Rising edge: qbzd started playing externally (Qobuz
                    # Connect). Claim ownership; the claim is a no-op when
                    # qobuz already owns playback, and paused/stopped events
                    # never reclaim.
                    logger.info("Qobuz Playing claim observed: source_mode=%s generation=%s source_changed=%s",
                                getattr(playback, "source_mode", None), source_generation, source_changed)
                    await self._deps.claim_qobuz_playback("qbzd-playing")
                    self._backoff_seconds = 0.0
                    if reader is not None:
                        state = await reader()
                was_playing = bool(self._deps.is_qobuz_playback_active(state))
                observed_source_generation = getattr(self._deps.get_playback_state(), "source_generation", None)
                if reader is not None and (self._broadcast_task is None or self._broadcast_task.done()):
                    self._broadcast_task = asyncio.create_task(self._broadcast({}), name="qobuz-state-broadcast")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Qobuz qbzd claim watch pass failed: %s", exc)
                if self._backoff_seconds < CLAIM_BACKOFF_MAX_SECONDS:
                    self._backoff_seconds = min(
                        CLAIM_BACKOFF_MAX_SECONDS,
                        self._backoff_seconds + CLAIM_BACKOFF_BASE_SECONDS,
                    )
            interval = (LINE_POLL_INTERVAL_SECONDS
                        if getattr(self._deps.get_playback_state(), "source_mode", "app-playback") != "app-playback"
                        else POLL_INTERVAL_SECONDS)
            await asyncio.sleep(interval + self._backoff_seconds)

    async def stop(self) -> None:
        task = self.watch_task
        broadcast = self._broadcast_task
        tasks = [item for item in (task, broadcast) if item is not None]
        for item in tasks:
            if not item.done():
                item.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.watch_task is task:
            self.watch_task = None
        if self._broadcast_task is broadcast:
            self._broadcast_task = None
