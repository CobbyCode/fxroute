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
    # Memoized gate sink for the current gate-ownership episode; the
    # coordinator invalidates this at episode boundaries.
    _resolved_gate_sink: str | None = None

    async def read_hardware_mute(self) -> bool:
        # The gate sink is stable for one gate-ownership episode; resolving it
        # once per episode avoids repeating the full status pipeline at every
        # mute readback boundary.  The coordinator invalidates this memo at
        # episode boundaries (_close_gate/_restore_gate).
        if self._resolved_gate_sink is None:
            self._resolved_gate_sink = await asyncio.to_thread(
                _hardware_sink_for_transition, self._deps
            )
        return await asyncio.to_thread(
            _read_hardware_sink_mute, self._resolved_gate_sink
        )

    async def set_hardware_mute(self, muted: bool, transition_id: str) -> None:
        if self._resolved_gate_sink is None:
            self._resolved_gate_sink = await asyncio.to_thread(
                _hardware_sink_for_transition, self._deps
            )
        await asyncio.to_thread(
            _set_hardware_sink_mute, self._resolved_gate_sink, muted
        )
        logger.info(
            "Playback transition output gate set: output=%s muted=%s transition_id=%s",
            self._resolved_gate_sink,
            muted,
            transition_id,
        )

    def invalidate_gate_sink_resolution(self) -> None:
        """Drop the memoized gate sink so the next boundary re-resolves it."""
        self._resolved_gate_sink = None

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

