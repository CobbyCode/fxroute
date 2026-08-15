# SPDX-License-Identifier: AGPL-3.0-only
"""Radio-stream reconnect bookkeeping and recovery scheduling.

Owns the reconnect task handle, attempt counter, the URL the counter belongs
to, and the reconnect window start, together with the schedule/delay behavior
moved out of ``main.py``.  No imports from ``main``: the Coordinator recovery
entry point and the playback state are injected by the composition root.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

RADIO_RECONNECT_DELAY_SECONDS = 2.0
RADIO_RECONNECT_MAX_ATTEMPTS = 5


@dataclass
class RadioReconnectDependencies:
    """Live services the radio reconnect scheduler needs."""

    get_player_instance: Callable[[], Any]
    get_playback_state: Callable[[], Any]
    request_coordinated_recovery: Callable[..., Any]


class RadioReconnect:
    """Single owner of the radio-stream reconnect bookkeeping and behavior."""

    def __init__(self, deps: RadioReconnectDependencies) -> None:
        self._deps = deps
        self.task: asyncio.Task | None = None
        self.attempts: int = 0
        self.url: str | None = None
        self.active_since: float = 0.0

    async def _reconnect_after_delay(
        self,
        track_info: dict,
        attempt: int,
        transition_generation: int | None,
    ) -> None:
        try:
            await asyncio.sleep(RADIO_RECONNECT_DELAY_SECONDS)
            expected_url = (track_info or {}).get("url")
            if not expected_url:
                return
            player_instance = self._deps.get_player_instance()
            if not player_instance or not player_instance._running:
                return
            playback_state = self._deps.get_playback_state()
            if not playback_state.transition_context_is_current(transition_generation):
                return
            if (
                not playback_state.current_track_info
                or playback_state.current_track_info.get("source") != "radio"
                or playback_state.current_track_info.get("url") != expected_url
            ):
                return
            state = player_instance.state
            if state.get("current_file") and not state.get("ended"):
                return
            logger.info(
                "Reconnecting radio stream after unexpected end: station=%s attempt=%s/%s",
                track_info.get("title") or track_info.get("id"),
                attempt,
                RADIO_RECONNECT_MAX_ATTEMPTS,
            )
            if not playback_state.transition_context_is_current(transition_generation):
                return
            await self._deps.request_coordinated_recovery(
                track_info,
                "radio-reconnect",
                reload_source=True,
            )
        except Exception as e:
            logger.warning("Radio stream reconnect failed: %s", e)
        finally:
            self.task = None

    def schedule(self, state: dict) -> None:
        playback_state = self._deps.get_playback_state()
        track_info = playback_state.current_track_info or {}
        track_url = track_info.get("url")
        if track_info.get("source") != "radio" or not track_url:
            return

        if state.get("current_file") and not state.get("ended"):
            if self.url != track_url:
                self.url = track_url
                self.attempts = 0
                self.active_since = time.monotonic()
            elif not self.active_since:
                self.active_since = time.monotonic()
            elif self.attempts and time.monotonic() - self.active_since >= 30.0:
                self.attempts = 0
            return

        self.active_since = 0.0
        if not (state.get("ended") and not state.get("current_file")):
            return

        if self.url != track_url:
            self.url = track_url
            self.attempts = 0
        if self.attempts >= RADIO_RECONNECT_MAX_ATTEMPTS:
            logger.warning(
                "Radio stream ended and reconnect limit reached: station=%s url=%s",
                track_info.get("title") or track_info.get("id"),
                track_url,
            )
            return
        if self.task and not self.task.done():
            return

        self.attempts += 1
        self.task = asyncio.create_task(
            self._reconnect_after_delay(
                dict(track_info),
                self.attempts,
                playback_state.capture_transition_epoch(),
            )
        )

    def reset(self) -> None:
        self.attempts = 0
        self.url = None
        self.active_since = 0.0

    async def stop(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
        self.task = None
