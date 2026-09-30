# SPDX-License-Identifier: AGPL-3.0-only
"""Source-mode transitions and the live source overview for clients.

Owns the committed source switch (routing, pause, peak sync, rollback), the
startup re-apply, the STDIN state hook and the client reads of the source
overview. Every overview that reaches clients is recorded or published
through the SourceOverviewFeed under the source-transition lock, so revisions
follow commit order. No imports from ``main``: the composition root injects
the services.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable

from audio.samplerate import (
    SOURCE_MODE_APP_PLAYBACK,
    SOURCE_MODE_BLUETOOTH_INPUT,
    SOURCE_MODE_EXTERNAL_INPUT,
    SOURCE_MODE_STDIN_INPUT,
)
from audio.source_feed import SourceOverviewFeed, SourceTransitionLock
from audio.source_monitor import validated_build
from common.run_to_completion import finish_then_cancel

logger = logging.getLogger(__name__)

# Oldest full overview build a STDIN-only refresh may reuse. Older bases are
# rebuilt once, so STDIN pushes also refresh inputs/Bluetooth, while a burst
# of STDIN events costs at most one pactl/bluetoothctl pipeline per window.
STDIN_OVERVIEW_BASE_MAX_AGE_SECONDS = 5.0

_LINE_SOURCE_MODES = {SOURCE_MODE_EXTERNAL_INPUT, SOURCE_MODE_BLUETOOTH_INPUT, SOURCE_MODE_STDIN_INPUT}


@dataclass
class SourceTransitionDependencies:
    """Live services the source transitions need (bound by main)."""

    get_lock: Callable[[], SourceTransitionLock]
    feed: SourceOverviewFeed
    # Blocking pactl/bluetoothctl work; always run in a worker thread.
    build_overview: Callable[[], dict]
    save_selection: Callable[[str, str | None], dict]
    load_selection: Callable[[], dict]
    with_current_stdin_status: Callable[[dict], dict]
    sync_external_input: Callable[[dict], Awaitable[dict]]
    sync_bluetooth_input: Callable[[dict], Awaitable[dict]]
    get_stdin_input: Callable[[], Any]
    note_source_selection: Callable[[str], Any]
    pause_app_playback: Callable[[], Awaitable[None]]
    sync_peak_monitor: Callable[[dict], Awaitable[None]]
    sync_peak_monitor_for_stdin: Callable[[dict], Awaitable[None]]
    # Deferred bluetoothctl actions recorded by the Bluetooth sync; run
    # after the source-transition lock is released.
    finish_bluetoothctl_actions: Callable[[], Awaitable[None]] | None = None


class SourceTransitions:
    """Single owner of source switches and of the overview clients see."""

    def __init__(self, deps: SourceTransitionDependencies) -> None:
        self._deps = deps

    @property
    def feed(self) -> SourceOverviewFeed:
        return self._deps.feed

    async def _build(self) -> dict:
        return await asyncio.to_thread(self._deps.build_overview)

    async def sync_source_mode_state(self, overview: dict, *, full_build: bool = True) -> dict:
        """Push a source overview to clients, then arm the peak monitor for it.

        Callers hold the source-transition lock, so the overview reflects
        committed state and its revision follows commit order. The push runs
        first: a failed peak-monitor restart must not leave clients on a
        stale source state (footer VU gating, source switcher, tabs).
        """
        stamped = await self.feed.publish(overview, full_build=full_build)
        await self._deps.sync_peak_monitor(overview)
        return stamped

    @asynccontextmanager
    async def fresh_overview_under_lock(self) -> AsyncIterator[dict | None]:
        """Hold the lock and yield a fresh overview, or None.

        The overview is built outside the lock and only counts when no other
        holder overlapped it (validated_build): its revision is assigned at
        record time, so a build read before a switch would outrank the
        switch's push. None means every attempt overlapped a holder; the
        newest recorded overview is then the committed state and each caller
        decides how to use it. Only while nothing was recorded yet, it builds
        once under the lock instead. A poll may still wait for a running
        switch, but a switch never waits for a poll's build.
        """
        async with validated_build(self._deps.get_lock(), self._deps.build_overview) as overview:
            if overview is None and self.feed.latest is None:
                overview = await self._build()
            yield overview

    async def read_overview(self) -> dict:
        """Client read (settings poll, page load, reconnect)."""
        async with self.fresh_overview_under_lock() as overview:
            if overview is None:
                return self.feed.latest
            return self.feed.record(overview)

    async def _publish_stdin_state(self, overview: dict, *, full_build: bool) -> None:
        await self.feed.publish(overview, full_build=full_build)
        await self._deps.sync_peak_monitor_for_stdin(overview)

    async def publish_stdin_state(self) -> None:
        """STDIN ``on_state_changed`` hook: push the live STDIN state to clients.

        While the newest full build is recent, only the STDIN parts are
        merged into it; otherwise the overview is rebuilt outside the lock.
        Publishing under the lock keeps a pre-switch snapshot from overtaking
        an in-flight source switch. The peak monitor follows STDIN only.
        """
        async with self._deps.get_lock():
            base = self.feed.fresh_latest(STDIN_OVERVIEW_BASE_MAX_AGE_SECONDS)
            if base is not None:
                await self._publish_stdin_state(self._deps.with_current_stdin_status(base), full_build=False)
                return
        async with self.fresh_overview_under_lock() as overview:
            if overview is not None:
                await self._publish_stdin_state(overview, full_build=True)
            else:
                await self._publish_stdin_state(
                    self._deps.with_current_stdin_status(self.feed.latest), full_build=False)

    async def _finish_bluetoothctl_actions(self) -> None:
        finish = self._deps.finish_bluetoothctl_actions
        if finish is not None:
            await finish()

    async def save_selection(self, mode: str, input_key: str | None) -> dict:
        """Client switch: one committed change under the lock, never half-done.

        A cancelled caller (client gone) keeps the lock until the change has
        finished, so routing and generations are never left half-committed.
        The bluetoothctl actions recorded by the Bluetooth sync run after
        the lock is released: their commands can hang on a vanished BlueZ
        daemon and must not stall the next source transition.
        """
        try:
            async with self._deps.get_lock():
                return await finish_then_cancel(self.apply_selection(mode, input_key), what="Source transition")
        finally:
            await self._finish_bluetoothctl_actions()

    async def _set_selected_stdin(self, selected: bool) -> None:
        stdin = self._deps.get_stdin_input()
        if stdin is not None:
            await stdin.set_selected(selected)

    async def _restore_selection(self, previous: dict) -> dict:
        restored = await asyncio.to_thread(
            self._deps.save_selection,
            str(previous.get("mode") or SOURCE_MODE_APP_PLAYBACK),
            previous.get("selected_input_key"),
        )
        restored = await self._deps.sync_external_input(restored)
        restored = await self._deps.sync_bluetooth_input(restored)
        try:
            await self._set_selected_stdin(restored.get("mode") == SOURCE_MODE_STDIN_INPUT)
        except Exception:
            logger.exception("Failed to re-sync STDIN after source-mode rollback")
        return restored

    async def apply_selection(self, mode: str, input_key: str | None) -> dict:
        """Run one source-mode/input change: routing commit, pause, peak sync.

        The caller holds the source-transition lock with cancellation
        deferred (save_selection): routing, generations, transports and peak
        state are always left consistent, never half-committed. The source
        generation (and the playback intent generation) advances exactly
        when the committed (mode, input) selection changed, before the line
        source pause, so in-flight playback transitions are discarded and the
        pause never stops a newer commit.
        """
        previous = self._deps.load_selection()
        previous_mode = str(previous.get("mode") or SOURCE_MODE_APP_PLAYBACK)
        generation_bumped = False
        try:
            result = await asyncio.to_thread(self._deps.save_selection, mode, input_key)
            if previous_mode == SOURCE_MODE_STDIN_INPUT and result.get("mode") != SOURCE_MODE_STDIN_INPUT:
                await self._set_selected_stdin(False)
            result = await self._deps.sync_external_input(result)
            result = await self._deps.sync_bluetooth_input(result)
        except BaseException:
            try:
                await self._restore_selection(previous)
            except BaseException:
                logger.exception("Failed to restore previous source selection after routing failure")
            raise
        new_mode = str(result.get("mode") or SOURCE_MODE_APP_PLAYBACK)
        selected = result.get("selected_input")
        new_key = selected.get("key") if isinstance(selected, dict) else None
        if new_mode != previous_mode or (new_key or None) != (previous.get("selected_input_key") or None):
            self._deps.note_source_selection(new_mode)
            generation_bumped = True
        try:
            if result.get("mode") in _LINE_SOURCE_MODES:
                await self._deps.pause_app_playback()
            if result.get("mode") == SOURCE_MODE_STDIN_INPUT:
                if self._deps.get_stdin_input() is None:
                    raise RuntimeError("STDIN input is unavailable")
                await self._set_selected_stdin(True)
                result = await self._build()
            await self._deps.sync_peak_monitor(result)
        except BaseException:
            try:
                restored = await self._restore_selection(previous)
                if generation_bumped:
                    self._deps.note_source_selection(str(restored.get("mode") or SOURCE_MODE_APP_PLAYBACK))
            except BaseException:
                logger.exception("Failed to restore previous source selection after routing failure")
            raise
        return await self.feed.publish(result)

    async def reapply_persisted(self) -> None:
        """Startup: re-apply the persisted selection under the lock.

        Same lock as every later source change, so the STDIN hook queued by
        the STDIN service's start runs after the selection is re-applied.
        """
        async with self._deps.get_lock():
            applied = await self._build()
            applied = await self._deps.sync_external_input(applied)
            applied = await self._deps.sync_bluetooth_input(applied)
            if self._deps.get_stdin_input() is not None:
                try:
                    await self._set_selected_stdin(applied.get("mode") == SOURCE_MODE_STDIN_INPUT)
                    applied = await self._build()
                except Exception as exc:
                    logger.warning("Failed to apply persisted STDIN selection: %s", exc)
            mode = applied.get("mode")
            if mode == SOURCE_MODE_EXTERNAL_INPUT:
                selected = applied.get("selected_input") or applied.get("current_input") or {}
                logger.info("Re-applied persisted external-input monitoring: %s",
                            selected.get("label") or "unknown input")
            elif mode == SOURCE_MODE_BLUETOOTH_INPUT:
                logger.info("Re-applied persisted Bluetooth input mode")
            elif mode == SOURCE_MODE_STDIN_INPUT:
                logger.info("Re-applied persisted STDIN input mode")
            await self.sync_source_mode_state(applied)
        await self._finish_bluetoothctl_actions()
