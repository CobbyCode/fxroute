# SPDX-License-Identifier: AGPL-3.0-only
"""External-input monitoring loopback routing.

Owns the external-input loopback source name and the link
connect/disconnect behavior, moved out of ``main.py``.  No imports from
``main``: the source overview reader is injected by the composition root.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from audio import pw_link
from audio.samplerate import SOURCE_MODE_EXTERNAL_INPUT

logger = logging.getLogger(__name__)

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


class ExternalInputRouting:
    """Single owner of the external-input loopback link and its state."""

    def __init__(self, deps: ExternalInputRoutingDependencies) -> None:
        self._deps = deps
        self.loopback_source_name: str | None = None
        self.loopback_selection_key: str | None = None
        self._active_channels: tuple[str, str] = _LEGACY_CHANNELS
        self._pending_channels: tuple[str, str] | None = None

    def _candidate_source_ports(self, source_name: str, channel: str) -> tuple[str, ...]:
        return (
            f"{source_name}:capture_{channel}",
            f"{source_name}:output_{channel}",
        )

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
        if self.loopback_selection_key == identity or (
            selection_key is None and self.loopback_selection_key is None
            and self.loopback_source_name == normalized
            and self._active_channels == (left, right)
        ):
            return
        await self.disable()
        self._pending_channels = (left, right)
        try:
            for channel, sink_side in ((left, "FL"), (right, "FR")):
                source_ports = self._candidate_source_ports(normalized, channel)
                sink_port = f"fxroute_dsp_sink:playback_{sink_side}"
                await pw_link.connect_ports(source_ports, sink_port)
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
        overview = source_overview or self._deps.get_audio_source_overview()
        if overview.get("mode") != SOURCE_MODE_EXTERNAL_INPUT:
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
        return overview
