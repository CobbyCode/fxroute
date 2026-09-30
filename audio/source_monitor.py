# SPDX-License-Identifier: AGPL-3.0-only
"""Shared pieces of the line-source monitors (Bluetooth, external input)."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable

logger = logging.getLogger(__name__)

# A single failed availability probe (bluetoothctl or pactl timeout, a
# WirePlumber restart dropping a node) looks exactly like a removed device.
# A selected source only counts as lost once its monitor kept observing it
# unavailable for this long.
SOURCE_UNAVAILABLE_CONFIRM_SECONDS = 5.0
# Optimistic source-overview builds before a reader gives up for this round.
# Player callbacks take the source-transition lock about every 0.5 s during
# local playback, so a single build overlaps a holder fairly often.
SOURCE_OVERVIEW_BUILD_ATTEMPTS = 3
# Consecutive monitor ticks without a validated build before the loop
# reports it once (30 s at the 3 s monitor interval).
SOURCE_MONITOR_SKIPPED_TICKS_REPORT = 10


class UnavailabilityConfirmation:
    """Confirm a source loss only after it persisted across observations.

    The owning monitor observes one availability probe per tick; the source
    overview only reads confirmed(). A loss is confirmed once at least two
    consecutive observations saw the source unavailable and the outage was
    first observed at least ``confirm_seconds`` ago, so one failed probe
    never confirms; any available observation starts over.
    """

    def __init__(self, confirm_seconds: float = SOURCE_UNAVAILABLE_CONFIRM_SECONDS,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._confirm_seconds = confirm_seconds
        self._monotonic = monotonic
        self._lock = threading.Lock()  # read from overview worker threads
        self._unavailable_since: float | None = None
        self._unavailable_observations = 0

    def observe(self, available: bool) -> None:
        with self._lock:
            if available:
                self._unavailable_since = None
                self._unavailable_observations = 0
                return
            if self._unavailable_since is None:
                self._unavailable_since = self._monotonic()
            self._unavailable_observations += 1

    def reset(self) -> None:
        self.observe(True)

    def confirmed(self) -> bool:
        with self._lock:
            since = self._unavailable_since
            observations = self._unavailable_observations
        return (observations >= 2 and since is not None
                and self._monotonic() - since >= self._confirm_seconds)


@asynccontextmanager
async def validated_build(
    lock: Any,
    build: Callable[[], dict],
    attempts: int = SOURCE_OVERVIEW_BUILD_ATTEMPTS,
) -> AsyncIterator[dict | None]:
    """Hold ``lock`` and yield a build made outside it, or None.

    ``build`` (a pactl/bluetoothctl pipeline) runs in a worker thread and
    never under the lock. It only counts when no other holder acquired the
    lock meanwhile, proven by the lock's acquisition count
    (audio.source_feed.SourceTransitionLock): a build read before a source
    switch must not be acted on or recorded after it. Yields None when
    every attempt overlapped a holder. A holder already running when an
    attempt starts is waited out first, since a build overlapping it would
    be discarded anyway.
    """
    for _attempt in range(attempts):
        if lock.locked():
            async with lock:
                pass
        generation = lock.generation
        started_idle = not lock.locked()
        result = await asyncio.to_thread(build)
        async with lock:
            if started_idle and lock.generation == generation + 1:
                yield result
                return
    async with lock:
        yield None


async def run_source_monitor_loop(
    *,
    name: str,
    interval: float,
    idle: Callable[[], bool],
    build: Callable[[], dict],
    act: Callable[[dict], Awaitable[None]],
    lock_provider: Callable[[], Any] | None,
    after: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Run one monitor tick per interval.

    ``idle`` skips a tick without building the source overview when the
    source is neither selected nor holding anything to clean up. The build
    runs outside the source-transition lock (validated_build), only ``act``
    runs under it, so a slow probe never holds the lock. ``after`` runs
    outside the lock after ``act``, for deferred commands (bluetoothctl
    actions) that can hang and must never hold it. A tick whose builds all
    overlapped a lock holder is skipped; a run of such ticks is reported
    once at info, and a validated tick starts the count over.
    """
    skipped_ticks = 0
    while True:
        if not idle():
            try:
                if lock_provider is None:
                    await act(await asyncio.to_thread(build))
                else:
                    async with validated_build(lock_provider(), build) as overview:
                        if overview is None:
                            skipped_ticks += 1
                            if skipped_ticks == SOURCE_MONITOR_SKIPPED_TICKS_REPORT:
                                logger.info("%s monitor skipped %d ticks in a row: every overview build "
                                            "overlapped a source transition", name, skipped_ticks)
                        else:
                            skipped_ticks = 0
                            await act(overview)
                if after is not None:
                    await after()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("%s monitor loop check failed: %s", name, exc)
        await asyncio.sleep(interval)
