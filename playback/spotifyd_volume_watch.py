# SPDX-License-Identifier: AGPL-3.0-only

"""spotifyd remote volume-to-master coupling (poll-based bridge).

Spotify Connect remote volume arrives at spotifyd as an absolute value on the
controller's own scale. With spotifyd's default ``volume_controller`` that
value attenuates the stream *pre-DSP* (software volume), so the phone slider
changed the loudness working point instead of the FXRoute master. The config
therefore pins ``volume_controller = "none"``: the Connect session still tracks
and reports the remote value, but no source gain is ever applied — the
structural equivalent of qbzd's ``volume_mode=locked``.

This watch polls the reported value via playerctl while Spotify owns playback
and translates it into master deltas through the shared
:class:`playback.remote_volume.RemoteVolumeDeltaTranslator`: the first value
observed for a session is the controller scale's anchor and never moves the
master; every later change is a user gesture applied as a relative step.
MPRIS ``Volume`` has no reliable PropertiesChanged signal in spotifyd 0.4.x,
so polling is the observable; the poll only runs while Spotify owns playback.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from playback.remote_volume import RemoteVolumeDeltaTranslator
from streaming.spotify import mpris

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 0.5
DEBOUNCE_SECONDS = 0.15


async def resolve_spotifyd_player() -> str | None:
    """Return the running spotifyd MPRIS instance name, or ``None``.

    Only spotifyd is bridged: the desktop client manages its own volume UX and
    is out of scope here. No visible MPRIS instance means no active Connect
    session (spotifyd hides MPRIS while idle).
    """
    backend = await mpris.detect_backend()
    if backend != "spotifyd":
        return None
    player = await mpris.resolve_player_name(backend)
    return player if mpris.is_spotifyd_player(player) else None


async def read_source_volume(player: str) -> int | None:
    """Return spotifyd's reported Connect volume in percent (0..100)."""
    raw = await mpris._run(f"--player={player}", "volume", timeout=2.0)
    if raw is None:
        return None
    try:
        fraction = float(raw)
    except ValueError:
        return None
    return max(0, min(100, int(round(fraction * 100))))


@dataclass
class SpotifydVolumeWatchDependencies:
    """Live services the spotifyd volume watch needs."""

    is_active: Callable[[], bool]
    apply_volume_delta: Callable[[int], Awaitable[Any]]
    resolve_player: Callable[[], Awaitable[str | None]] = resolve_spotifyd_player
    read_source_volume: Callable[[str], Awaitable[int | None]] = read_source_volume


class SpotifydVolumeWatch:
    """Poll spotifyd's Connect volume and route deltas to the canonical master."""

    def __init__(
        self,
        deps: SpotifydVolumeWatchDependencies,
        *,
        poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
        debounce_seconds: float = DEBOUNCE_SECONDS,
    ) -> None:
        self._deps = deps
        self._poll_interval_seconds = poll_interval_seconds
        self._debounce_seconds = debounce_seconds
        self._translator = RemoteVolumeDeltaTranslator(
            is_active=deps.is_active,
            apply_volume_delta=deps.apply_volume_delta,
        )
        self.watch_task: asyncio.Task | None = None
        self._drain_task: asyncio.Task | None = None

    async def _sleep(self, delay: float) -> None:
        await asyncio.sleep(delay)

    def _schedule_drain(self) -> None:
        if self._drain_task is not None and not self._drain_task.done():
            return
        self._drain_task = asyncio.create_task(self._drain_pending(), name="spotifyd-volume-drain")

    async def _drain_pending(self) -> None:
        try:
            if self._debounce_seconds > 0:
                await self._sleep(self._debounce_seconds)
            await self._translator.flush()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Detached task: observe failures here so the watch loop survives
            # for the next intent and no exception stays unretrieved.
            logger.warning("spotifyd volume drain failed: %s", exc)
        finally:
            self._drain_task = None

    async def poll_once(self) -> bool:
        """Run one observation pass; return ``True`` when a delta is pending.

        Split out of the loop so tests can drive passes deterministically.
        """
        if not self._deps.is_active():
            # Ownership left: drop anchor and pending so a stale scale
            # can never leak into another owner's master writes.
            self._translator.observe_activation()
            return False
        player = await self._deps.resolve_player()
        if player is None:
            # No session (or no spotifyd at all): re-anchor on the
            # next observation instead of trusting a stale anchor.
            self._translator.observe_activation()
            return False
        value = await self._deps.read_source_volume(player)
        if value is None:
            return False
        return self._translator.submit(value)

    async def run_watch_loop(self) -> None:
        logger.info("spotifyd remote volume watch loop entered")
        while True:
            try:
                if await self.poll_once():
                    self._schedule_drain()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("spotifyd volume watch pass failed: %s", exc)
            await self._sleep(self._poll_interval_seconds)

    async def stop(self) -> None:
        if self._drain_task is not None and not self._drain_task.done():
            self._drain_task.cancel()
        self._drain_task = None
        if self.watch_task is not None and not self.watch_task.done():
            self.watch_task.cancel()
        self.watch_task = None
