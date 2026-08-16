# SPDX-License-Identifier: AGPL-3.0-only

"""Hardware and DSP-sink mute operations of the runtime adapter.

Extracted from :class:`FxrouteTransitionRuntime`; runs on the composing
adapter instance and reads the attributes declared on the class below.
"""

from __future__ import annotations

import asyncio
import logging

from .deps import PlaybackRuntimeDependencies
from .helpers import (
    _hardware_sink_for_transition,
    _read_hardware_sink_mute,
    _read_sink_mute,
    _set_hardware_sink_mute,
    _set_sink_mute,
)

logger = logging.getLogger(__name__)


class _RuntimeMuteMixin:
    """Attributes provided by the composing adapter instance."""
    _deps: PlaybackRuntimeDependencies
    _output_key: str | None

    async def read_hardware_mute(self) -> bool:
        self._output_key = _hardware_sink_for_transition(self._deps)
        return await asyncio.to_thread(_read_hardware_sink_mute, self._output_key)

    async def set_hardware_mute(self, muted: bool, transition_id: str) -> None:
        output_key = self._output_key or _hardware_sink_for_transition(self._deps)
        self._output_key = output_key
        await asyncio.to_thread(_set_hardware_sink_mute, output_key, muted)
        logger.info(
            "Playback transition output gate set: output=%s muted=%s transition_id=%s",
            output_key,
            muted,
            transition_id,
        )

    async def read_sink_mute(self, sink_name: str) -> bool:
        return await asyncio.to_thread(_read_sink_mute, sink_name)

    async def set_sink_mute(
        self, sink_name: str, muted: bool, transition_id: str
    ) -> None:
        await asyncio.to_thread(_set_sink_mute, sink_name, muted)
        logger.info(
            "Playback transition explicit sink mute set: sink=%s muted=%s transition_id=%s",
            sink_name,
            muted,
            transition_id,
        )

