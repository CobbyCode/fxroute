# SPDX-License-Identifier: AGPL-3.0-only
"""Revision-stamp audio source overviews and push changes to clients."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable
from uuid import uuid4

logger = logging.getLogger(__name__)

_STAMP_KEYS = ("epoch", "revision")


class SourceTransitionLock(asyncio.Lock):
    """asyncio.Lock that counts its acquisitions.

    A reader that builds a source overview outside the lock compares the
    count before and after: when only its own acquisition was added, no
    holder (switch, monitor tick, STDIN hook, another reader) ran meanwhile.
    """

    def __init__(self) -> None:
        super().__init__()
        self.generation = 0

    async def acquire(self) -> bool:
        await super().acquire()
        self.generation += 1
        return True


class SourceOverviewFeed:
    """Stamp every client-facing audio source overview with a revision.

    Every record() and publish() runs under the source-transition lock, so
    revisions follow lock order and a client can drop an overview that lost
    the race between a push, a poll and a save response. The build itself
    may run outside the lock (the settings poll, STDIN rebuilds) only when
    SourceTransitionLock.generation shows that no holder ran meanwhile; an
    unvalidated build read before a switch would outrank the switch's push.
    The epoch identifies this process: a new epoch after a service restart
    tells clients to reset their revision base instead of rejecting the
    restarted counter.
    """

    def __init__(
        self,
        broadcast: Callable[[dict], Awaitable[Any]],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._broadcast = broadcast
        self._monotonic = monotonic
        self.epoch = uuid4().hex
        self._revision = 0
        self._latest: dict | None = None
        self._latest_built_at: float | None = None
        self._pushed_key: str | None = None

    @property
    def latest(self) -> dict | None:
        """Newest recorded overview, or None before the first record."""
        return self._latest

    def fresh_latest(self, max_age: float) -> dict | None:
        """Newest overview if its last full build is at most ``max_age`` old.

        Partial refreshes (``full_build=False``) keep the age of the full
        build they were derived from, so a stream of them cannot keep a
        stale base alive.
        """
        if self._latest is None or self._latest_built_at is None:
            return None
        if self._monotonic() - self._latest_built_at > max_age:
            return None
        return self._latest

    def reset(self) -> None:
        """Forget the recorded state at startup; epoch and revision stay."""
        self._latest = None
        self._latest_built_at = None
        self._pushed_key = None

    def record(self, overview: dict, *, full_build: bool = True) -> dict:
        """Stamp ``overview`` with the next revision and keep it as newest."""
        self._revision += 1
        body = {key: value for key, value in overview.items() if key not in _STAMP_KEYS}
        self._latest = {**body, "epoch": self.epoch, "revision": self._revision}
        if full_build:
            self._latest_built_at = self._monotonic()
        return self._latest

    async def publish(self, overview: dict, *, full_build: bool = True) -> dict:
        """Record ``overview`` and push it unless it equals the last push.

        A failed push is logged only: the source state it describes is
        already committed.
        """
        stamped = self.record(overview, full_build=full_build)
        key = json.dumps({k: v for k, v in stamped.items() if k not in _STAMP_KEYS},
                         sort_keys=True, default=str)
        if key != self._pushed_key:
            try:
                await self._broadcast({"type": "source", "data": stamped})
                self._pushed_key = key
            except Exception:
                logger.exception("Failed to push the audio source overview")
        return stamped
