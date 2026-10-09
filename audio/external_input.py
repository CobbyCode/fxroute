# SPDX-License-Identifier: AGPL-3.0-only
"""External-input monitoring loopback routing and link recovery.

Owns the external-input loopback source name and the link
connect/disconnect behavior, moved out of ``main.py``.  No imports from
``main``: the source overview reader is injected by the composition root.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable

from audio import pw_link
from audio.input_links import input_links_present
from dsp.runtime import mapped_ingress_edges
from audio.samplerate import SOURCE_MODE_EXTERNAL_INPUT
from audio.source_monitor import UnavailabilityConfirmation, run_source_monitor_loop

logger = logging.getLogger(__name__)
EXTERNAL_INPUT_MONITOR_INTERVAL_SECONDS = 3

OverviewReader = Callable[[], dict[str, Any]]

# Fallback stereo channels when the overview carries no pair info (legacy
# overview shape).  Never a mono duplication: both sides always resolve to
# distinct channels of one stereo pair.
_LEGACY_CHANNELS = ("FL", "FR")


def _pair_channels(current_input: dict[str, Any], right_source: str | None = None) -> tuple[str, str]:
    left = str(current_input.get("left_channel") or "").strip() or _LEGACY_CHANNELS[0]
    right = str(current_input.get("right_channel") or "").strip() or _LEGACY_CHANNELS[1]
    if left == right and right_source is None:
        # One node cannot feed both sides from one channel; two nodes can.
        left, right = _LEGACY_CHANNELS
    return left, right


def _right_source(current_input: dict[str, Any], source_name: str) -> str | None:
    """Capture node of the right channel when it is not the node of the left one."""
    right_source = str(current_input.get("right_source") or "").strip()
    return right_source if right_source and right_source != source_name else None


@dataclass
class ExternalInputRoutingDependencies:
    """Live services the external-input routing needs."""

    get_audio_source_overview: OverviewReader
    get_persisted_source_mode: Callable[[], str] | None = None
    get_source_transition_lock: Callable[[], asyncio.Lock] | None = None
    sync_peak_monitor_for_source_mode_state: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    get_output_mode: Callable[[], str] | None = None
    # (source, left, right, ALSA card, optional right capture node)
    sync_digital_input: Callable[..., Awaitable[None]] | None = None
    stop_digital_input: Callable[[], Awaitable[None]] | None = None
    # Surround ingress of a custom input mapping: (capture port, engine input
    # port) edges, linked directly to the engine.
    sync_mapped_input: Callable[[tuple[tuple[str, str], ...]], Awaitable[None]] | None = None
    stop_mapped_input: Callable[[], Awaitable[None]] | None = None
    # Selected input entry: keep the digital carrier (the card delivers its
    # digital samples only on the incoming clock) or release it.
    sync_input_carrier: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    measurement_audio_graph_owned: Callable[[], bool] | None = None
    defer_input_resync: Callable[[], Awaitable[None]] | None = None


class ExternalInputRouting:
    """Single owner of the external-input loopback link and its state."""

    def __init__(self, deps: ExternalInputRoutingDependencies) -> None:
        self._deps = deps
        self.loopback_source_name: str | None = None
        self.loopback_selection_key: str | None = None
        self.digital_source_name: str | None = None
        self.digital_selection_key: str | None = None
        self.mapped_source_name: str | None = None
        # Links of the established mapped ingress, for the power-hint probe.
        self.mapped_ingress_links: tuple[tuple[str, str], ...] = ()
        self._active_channels: tuple[str, str] = _LEGACY_CHANNELS
        self._pending_channels: tuple[str, str] | None = None
        # Capture node of the right channel when it differs from the left one.
        self._active_right_source: str | None = None
        self._pending_right_source: str | None = None
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

    @property
    def active_right_source(self) -> str | None:
        """Capture node of the established right link when it is not the left node."""
        return self._active_right_source

    async def _disconnect_source(
        self,
        source_name: str | None,
        left_channel: str | None = None,
        right_channel: str | None = None,
        right_source: str | None = None,
    ) -> None:
        normalized = (source_name or "").strip()
        if not normalized:
            return
        if left_channel is None or right_channel is None:
            pending = self._pending_channels
            if pending is not None:
                left_channel, right_channel = pending
                right_source = self._pending_right_source
            else:
                left_channel, right_channel = self._active_channels
                right_source = self._active_right_source
        for source, channel, sink_side in (
            (normalized, left_channel, "FL"),
            (right_source or normalized, right_channel, "FR"),
        ):
            sink_port = f"fxroute_dsp_sink:playback_{sink_side}"
            await pw_link.disconnect_ports(
                self._candidate_source_ports(source, str(channel or "").strip() or sink_side),
                sink_port,
            )

    async def disable(self) -> None:
        if self.digital_source_name is not None and self._deps.stop_digital_input is not None:
            await self._deps.stop_digital_input()
        self.digital_source_name = None
        self.digital_selection_key = None
        if self.mapped_source_name is not None and self._deps.stop_mapped_input is not None:
            await self._deps.stop_mapped_input()
        self.mapped_source_name = None
        self.mapped_ingress_links = ()
        previous_source = self.loopback_source_name
        previous_channels = self._active_channels
        previous_right_source = self._active_right_source
        self.loopback_source_name = None
        self.loopback_selection_key = None
        self._active_channels = _LEGACY_CHANNELS
        self._active_right_source = None
        self._pending_channels = None
        self._pending_right_source = None
        if previous_source:
            await self._disconnect_source(
                previous_source,
                left_channel=previous_channels[0],
                right_channel=previous_channels[1],
                right_source=previous_right_source,
            )

    async def _ensure_loopback(
        self,
        source_name: str,
        left_channel: str = _LEGACY_CHANNELS[0],
        right_channel: str = _LEGACY_CHANNELS[1],
        selection_key: str | None = None,
        right_source: str | None = None,
    ) -> None:
        normalized = (source_name or "").strip()
        if not normalized:
            raise RuntimeError("Missing source name for external-input monitoring")
        left = (left_channel or "").strip() or _LEGACY_CHANNELS[0]
        right = (right_channel or "").strip() or _LEGACY_CHANNELS[1]
        right_node = (right_source or "").strip() or None
        if right_node == normalized:
            right_node = None
        if left == right and right_node is None:
            raise RuntimeError("External input requires two distinct stereo channels")
        identity = (selection_key or "").strip() or normalized
        legs = ((normalized, left, "FL"), (right_node or normalized, right, "FR"))
        if (
            self.loopback_selection_key == identity
            and self.loopback_source_name == normalized
            and self._active_channels == (left, right)
            and self._active_right_source == right_node
            and await self._loopback_links_present(legs)
        ):
            return
        await self.disable()
        self._pending_channels = (left, right)
        self._pending_right_source = right_node
        try:
            for source, channel, sink_side in legs:
                source_ports = self._candidate_source_ports(source, channel)
                sink_port = f"fxroute_dsp_sink:playback_{sink_side}"
                await pw_link.connect_ports(source_ports, sink_port)
            if not await self._loopback_links_present(legs):
                raise RuntimeError("External input links missing after reconnect")
        except BaseException:
            try:
                await self._disconnect_source(normalized)
            finally:
                self._pending_channels = None
                self._pending_right_source = None
            raise
        self._pending_channels = None
        self._pending_right_source = None
        self.loopback_source_name = normalized
        self.loopback_selection_key = identity
        self._active_channels = (left, right)
        self._active_right_source = right_node
        logger.info(
            "Enabled direct external-input monitoring from %s (%s->L) and %s (%s->R) to fxroute_dsp_sink",
            normalized, left, right_node or normalized, right,
        )

    @staticmethod
    async def _loopback_links_present(legs: tuple[tuple[str, str, str], ...]) -> bool:
        """Check every (source, channel, sink side) leg, one live-graph read per source."""
        channels_by_source: dict[str, list[tuple[str, str]]] = {}
        for source, channel, sink_side in legs:
            channels_by_source.setdefault(source, []).append((channel, sink_side))
        for source, channels in channels_by_source.items():
            if not await input_links_present(source, tuple(channels)):
                return False
        return True

    async def sync(self, source_overview: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self._sync_lock:
            return await self._sync_unlocked(source_overview)

    @asynccontextmanager
    async def measurement_entry_guard(self) -> AsyncIterator[None]:
        """Hold routing idle until measurement atomically claims graph ownership."""
        async with self._sync_lock:
            yield

    async def _sync_unlocked(self, source_overview: dict[str, Any] | None = None) -> dict[str, Any]:
        overview = source_overview or await asyncio.to_thread(self._deps.get_audio_source_overview)
        owns_graph = self._deps.measurement_audio_graph_owned
        if owns_graph is not None and owns_graph():
            if self._deps.defer_input_resync is not None:
                await self._deps.defer_input_resync()
            return overview
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
        right_source = _right_source(current_input, str(source_name))
        left, right = _pair_channels(current_input, right_source)
        surround = self._deps.get_output_mode is not None and self._deps.get_output_mode() == "surround"
        custom_mapping = current_input.get("custom_mapping")
        if (surround and not isinstance(custom_mapping, dict) and right_source is not None
                and self._deps.sync_digital_input is None):
            # A legacy caller without a decoder links split Front L/R directly.
            custom_mapping = {
                "suffixes": {"FL": left, "FR": right},
                "sources": {"FL": str(source_name), "FR": right_source},
            }
        if surround and isinstance(custom_mapping, dict):
            await self._sync_mapped(str(source_name), custom_mapping)
            self._active_channels = (left, right)
            self._remembered_input = dict(current_input)
            return overview
        if not isinstance(custom_mapping, dict) and (surround or self._deps.sync_digital_input is not None):
            if self._deps.sync_digital_input is None:
                await self.disable()
                raise RuntimeError("Automatic input ingress is unavailable")
            if self.loopback_source_name is not None or self.mapped_source_name is not None:
                await self.disable()
            try:
                args = (str(source_name), left, right, current_input.get("alsa_card"))
                if right_source:
                    await self._deps.sync_digital_input(*args, right_source)
                else:
                    await self._deps.sync_digital_input(*args)
            except BaseException:
                if self._deps.stop_digital_input is not None:
                    await self._deps.stop_digital_input()
                self.digital_source_name = None
                self.digital_selection_key = None
                raise
            self.digital_source_name = str(source_name)
            self.digital_selection_key = str(current_input.get("key") or "") or None
            self._active_channels = (left, right)
            self._active_right_source = right_source
            self._remembered_input = dict(current_input)
            return overview
        if self.digital_source_name is not None or self.mapped_source_name is not None:
            await self.disable()
        if self._deps.sync_input_carrier is not None:
            await self._deps.sync_input_carrier(dict(current_input))
        await self._ensure_loopback(
            str(source_name),
            left,
            right,
            selection_key=str(current_input.get("key") or ""),
            right_source=right_source,
        )
        self._remembered_input = dict(current_input)
        return overview

    async def _sync_mapped(self, source_name: str, custom_mapping: dict[str, Any]) -> None:
        """Link the mapped capture channels straight to the engine inputs."""
        edges = mapped_ingress_edges(
            source_name, custom_mapping.get("suffixes") or {}, custom_mapping.get("sources"))
        if self._deps.sync_mapped_input is None or not edges:
            await self.disable()
            raise RuntimeError("Mapped surround ingress is unavailable")
        if self.loopback_source_name is not None or self.digital_source_name is not None:
            await self.disable()
        try:
            await self._deps.sync_mapped_input(edges)
        except BaseException:
            if self._deps.stop_mapped_input is not None:
                await self._deps.stop_mapped_input()
            self.mapped_source_name = None
            self.mapped_ingress_links = ()
            raise
        self.mapped_source_name = source_name
        self.mapped_ingress_links = edges

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
        elif (self.loopback_source_name is not None or self.digital_source_name is not None
              or self.mapped_source_name is not None):
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
            and self.digital_source_name is None
            and self.mapped_source_name is None
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
