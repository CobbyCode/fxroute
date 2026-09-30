# SPDX-License-Identifier: AGPL-3.0-only
"""External-input monitoring loopback routing and link recovery.

Owns the external-input loopback source name and the link
connect/disconnect behavior, moved out of ``main.py``.  No imports from
``main``: the source overview reader is injected by the composition root.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from audio import pw_link
from audio.input_links import input_links_present
from audio.samplerate import SOURCE_MODE_EXTERNAL_INPUT
from audio.source_monitor import UnavailabilityConfirmation, run_source_monitor_loop

logger = logging.getLogger(__name__)
EXTERNAL_INPUT_MONITOR_INTERVAL_SECONDS = 3

OverviewReader = Callable[[], dict[str, Any]]

# Fallback stereo channels when the overview carries no pair info (legacy
# overview shape).  Never a mono duplication: both sides always resolve to
# distinct channels of one stereo pair.
_LEGACY_CHANNELS = ("FL", "FR")


def _pair_channels(current_input: dict[str, Any]) -> tuple[str, str]:
    left = str(current_input.get("left_channel") or "").strip() or _LEGACY_CHANNELS[0]
    right = str(current_input.get("right_channel") or "").strip() or _LEGACY_CHANNELS[1]
    if left == right:
        left, right = _LEGACY_CHANNELS
    return left, right


@dataclass
class ExternalInputRoutingDependencies:
    """Live services the external-input routing needs."""

    get_audio_source_overview: OverviewReader
    get_persisted_source_mode: Callable[[], str] | None = None
    get_source_transition_lock: Callable[[], asyncio.Lock] | None = None
    sync_peak_monitor_for_source_mode_state: Callable[[dict[str, Any]], Awaitable[None]] | None = None


class ExternalInputRouting:
    """Single owner of the external-input loopback link and its state."""

    def __init__(self, deps: ExternalInputRoutingDependencies) -> None:
        self._deps = deps
        self.loopback_source_name: str | None = None
        self.loopback_selection_key: str | None = None
        self._active_channels: tuple[str, str] = _LEGACY_CHANNELS
        self._pending_channels: tuple[str, str] | None = None
        self._sync_lock = asyncio.Lock()
        self.monitor_task: asyncio.Task | None = None
        # Observed once per monitor tick; the source overview reads
        # confirmed() and remembered_input() for a missing selection.
        self.availability = UnavailabilityConfirmation()
        self._remembered_input: dict[str, Any] | None = None

    def remembered_input(self, key: str) -> dict[str, Any] | None:
        """Last linked entry for ``key``, to label it while it is missing."""
        entry = self._remembered_input
        return dict(entry) if entry is not None and entry.get("key") == key else None

    def _candidate_source_ports(self, source_name: str, channel: str) -> tuple[str, ...]:
        return (
            f"{source_name}:capture_{channel}",
            f"{source_name}:output_{channel}",
        )

    @property
    def active_channels(self) -> tuple[str, str]:
        """Return the stereo channels of the established loopback link."""
        return (self._active_channels[0], self._active_channels[1])

    async def _disconnect_source(
        self,
        source_name: str | None,
        left_channel: str | None = None,
        right_channel: str | None = None,
    ) -> None:
        normalized = (source_name or "").strip()
        if not normalized:
            return
        if left_channel is None or right_channel is None:
            pending = self._pending_channels
            if pending is not None:
                left_channel, right_channel = pending
            else:
                left_channel, right_channel = self._active_channels
        for channel, sink_side in ((left_channel, "FL"), (right_channel, "FR")):
            sink_port = f"fxroute_dsp_sink:playback_{sink_side}"
            await pw_link.disconnect_ports(
                self._candidate_source_ports(normalized, str(channel or "").strip() or sink_side),
                sink_port,
            )

    async def disable(self) -> None:
        previous_source = self.loopback_source_name
        previous_channels = self._active_channels
        self.loopback_source_name = None
        self.loopback_selection_key = None
        self._active_channels = _LEGACY_CHANNELS
        self._pending_channels = None
        if previous_source:
            await self._disconnect_source(
                previous_source,
                left_channel=previous_channels[0],
                right_channel=previous_channels[1],
            )

    async def _ensure_loopback(
        self,
        source_name: str,
        left_channel: str = _LEGACY_CHANNELS[0],
        right_channel: str = _LEGACY_CHANNELS[1],
        selection_key: str | None = None,
    ) -> None:
        normalized = (source_name or "").strip()
        if not normalized:
            raise RuntimeError("Missing source name for external-input monitoring")
        left = (left_channel or "").strip() or _LEGACY_CHANNELS[0]
        right = (right_channel or "").strip() or _LEGACY_CHANNELS[1]
        if left == right:
            raise RuntimeError("External input requires two distinct stereo channels")
        identity = (selection_key or "").strip() or normalized
        channels = ((left, "FL"), (right, "FR"))
        if (
            self.loopback_selection_key == identity
            and self.loopback_source_name == normalized
            and self._active_channels == (left, right)
            and await input_links_present(normalized, channels)
        ):
            return
        await self.disable()
        self._pending_channels = (left, right)
        try:
            for channel, sink_side in ((left, "FL"), (right, "FR")):
                source_ports = self._candidate_source_ports(normalized, channel)
                sink_port = f"fxroute_dsp_sink:playback_{sink_side}"
                await pw_link.connect_ports(source_ports, sink_port)
            if not await input_links_present(normalized, channels):
                raise RuntimeError("External input links missing after reconnect")
        except BaseException:
            try:
                await self._disconnect_source(normalized)
            finally:
                self._pending_channels = None
            raise
        self._pending_channels = None
        self.loopback_source_name = normalized
        self.loopback_selection_key = identity
        self._active_channels = (left, right)
        logger.info(
            "Enabled direct external-input monitoring from %s (%s->L, %s->R) to fxroute_dsp_sink",
            normalized, left, right,
        )

    async def sync(self, source_overview: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self._sync_lock:
            return await self._sync_unlocked(source_overview)

    async def _sync_unlocked(self, source_overview: dict[str, Any] | None = None) -> dict[str, Any]:
        overview = source_overview or await asyncio.to_thread(self._deps.get_audio_source_overview)
        if not self._external_persisted():
            # Left external input: an old outage must not count later.
            self.availability.reset()
        if overview.get("mode") != SOURCE_MODE_EXTERNAL_INPUT:
            await self.disable()
            return overview
        input_state = overview.get("external_input_state")
        if input_state == "unconfirmed":
            # One failed probe: keep the working loopback until confirmed.
            return overview
        if input_state == "unavailable":
            # Confirmed loss of the saved input: unlink, never substitute.
            await self.disable()
            return overview
        current_input = overview.get("selected_input") or overview.get("current_input") or {}
        source_name = current_input.get("source_key") or current_input.get("name")
        if not source_name:
            await self.disable()
            return overview
        left, right = _pair_channels(current_input)
        await self._ensure_loopback(
            str(source_name),
            left,
            right,
            selection_key=str(current_input.get("key") or ""),
        )
        self._remembered_input = dict(current_input)
        return overview

    async def _monitor_once(self) -> None:
        """One tick outside the loop: build, then act without lock handling."""
        await self._act(await asyncio.to_thread(self._deps.get_audio_source_overview))

    async def _act(self, overview: dict[str, Any]) -> None:
        self._observe_availability(overview)
        if overview.get("mode") == SOURCE_MODE_EXTERNAL_INPUT:
            if overview.get("external_input_state") == "unconfirmed":
                # Keep the loopback and the published state until a later
                # probe confirms the loss or reports the input again.
                return
            overview = await self.sync(overview)
        elif self.loopback_source_name is not None:
            await self.sync(overview)
        elif not self._external_persisted():
            return
        # Otherwise external input stays selected, the overview fell back
        # (no input present) and no loopback was ever established (linking
        # failed, or started without the interface): publish that state
        # like any other fallback. Unchanged ticks are deduplicated by the
        # shared publish path.
        if self._deps.sync_peak_monitor_for_source_mode_state is not None:
            await self._deps.sync_peak_monitor_for_source_mode_state(overview)

    def _observe_availability(self, overview: dict[str, Any]) -> None:
        """Record this tick's raw probe: is the saved input present?"""
        if self._external_persisted():
            self.availability.observe(overview.get("external_input_state") == "available")
        else:
            self.availability.reset()

    def _external_persisted(self) -> bool:
        mode_provider = self._deps.get_persisted_source_mode
        return mode_provider is not None and mode_provider() == SOURCE_MODE_EXTERNAL_INPUT

    async def run_monitor_loop(self) -> None:
        await run_source_monitor_loop(
            name="External input",
            interval=EXTERNAL_INPUT_MONITOR_INTERVAL_SECONDS,
            idle=self._idle,
            build=lambda: self._deps.get_audio_source_overview(),
            act=self._act,
            lock_provider=self._deps.get_source_transition_lock,
        )

    def _idle(self) -> bool:
        mode_provider = self._deps.get_persisted_source_mode
        return (
            mode_provider is not None
            and mode_provider() != SOURCE_MODE_EXTERNAL_INPUT
            and self.loopback_source_name is None
        )

    async def stop(self) -> None:
        task = self.monitor_task
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if self.monitor_task is task:
                self.monitor_task = None
        await self.disable()
