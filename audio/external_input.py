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


@dataclass
class ExternalInputRoutingDependencies:
    """Live services the external-input routing needs."""

    get_audio_source_overview: OverviewReader


class ExternalInputRouting:
    """Single owner of the external-input loopback link and its state."""

    def __init__(self, deps: ExternalInputRoutingDependencies) -> None:
        self._deps = deps
        self.loopback_source_name: str | None = None

    async def _disconnect_source(self, source_name: str | None) -> None:
        normalized = (source_name or "").strip()
        if not normalized:
            return
        for channel in ("FL", "FR"):
            sink_port = f"fxroute_dsp_sink:playback_{channel}"
            await pw_link.disconnect_ports((f"{normalized}:capture_{channel}",), sink_port)

    async def disable(self) -> None:
        previous_source = self.loopback_source_name
        await self._disconnect_source(previous_source)
        self.loopback_source_name = None

    async def _ensure_loopback(self, source_name: str) -> None:
        normalized = (source_name or "").strip()
        if not normalized:
            raise RuntimeError("Missing source name for external-input monitoring")
        if self.loopback_source_name == normalized:
            return
        await self.disable()
        try:
            for channel in ("FL", "FR"):
                source_port = f"{normalized}:capture_{channel}"
                sink_port = f"fxroute_dsp_sink:playback_{channel}"
                await pw_link.connect_ports((source_port,), sink_port)
        except BaseException:
            await self._disconnect_source(normalized)
            raise
        self.loopback_source_name = normalized
        logger.info("Enabled direct external-input monitoring from %s to fxroute_dsp_sink", normalized)

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
        await self._ensure_loopback(str(source_name))
        return overview
