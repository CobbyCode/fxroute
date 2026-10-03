# SPDX-License-Identifier: AGPL-3.0-only
"""Serialized SRC policy application; graph lifecycle is injected by main."""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from audio import src_policy
from common.run_to_completion import run_to_completion


class SrcController:
    def __init__(self, guard: Callable[[], Awaitable[None]], restart, lock, *, snapshot=None):
        self.guard = guard
        self.restart = restart
        self.lock = lock
        self.snapshot = snapshot

    async def apply(self, mode: object) -> dict:
        selected = src_policy.normalize_mode(mode)
        async with self.lock:
            await self.guard()
            error = await asyncio.to_thread(src_policy.runtime_error)
            if error:
                raise RuntimeError(error)
            state = await self.snapshot() if self.snapshot else None
            return await run_to_completion(self._apply(selected, state))

    async def _restart(self, state):
        if self.snapshot:
            await self.restart(state)
        else:
            await self.restart()

    async def _apply(self, mode: str, state) -> dict:
        previous = await asyncio.to_thread(src_policy.load_policy)
        await asyncio.to_thread(src_policy.persist_policy, mode)
        try:
            await self._restart(state)
            result = await asyncio.to_thread(src_policy.status)
            if not result["available"]:
                raise RuntimeError(result["error"])
            return result
        except Exception as exc:
            try:
                await asyncio.to_thread(src_policy.persist_policy, previous["mode"])
                await self._restart(state)
            except Exception as rollback:
                raise RuntimeError(f"SRC apply failed: {exc}; previous graph could not be restored: {rollback}") from exc
            raise RuntimeError(f"SRC apply failed; previous mode restored: {exc}") from exc
