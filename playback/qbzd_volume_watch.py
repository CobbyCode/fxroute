# SPDX-License-Identifier: AGPL-3.0-only

"""Qobuz (qbzd) journal-driven remote volume-to-master coupling.

qbzd 2.0.2 decouples QConnect volume from its applied gain only when
``qconnect.volume_mode=locked``: remote ``SetVolume`` commands are ignored for
the engine ("player stays at 100%") while the connect session volume follows
the phone slider. There is no volume event on ``/api/events`` and no HTTP
endpoint carrying the session volume (live-verified on .104), so the phone
intent is observable only through the daemon journal line::

    [QConnect] volume_mode=locked: ignoring remote SetVolume(0.450); player stays at 100%

This module tails that line via ``journalctl --user -u qbzd.service -f`` and
maps each intent onto the canonical FXRoute master volume. qbzd's own gain is
never written here; the unity pin lives at the Qobuz claim/start path
(``set_volume(100)`` works in locked mode through the local control plane).

The parser only recognizes locked-mode lines, so in ``software`` mode this
watch is a silent no-op. The translator debounces the drag burst (a phone drag
emits one line per step) so the last value of a gesture is applied exactly once.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Tail only new lines: journalctl -f already streams; -n 0 avoids replaying
# pre-start history (FXRoute must not apply volumes from before its start).
JOURNALCTL_COMMAND = [
    "journalctl", "--user", "-u", "qbzd.service", "-f", "-o", "cat", "-n", "0",
]
RESTART_BACKOFF_BASE_SECONDS = 1.0
RESTART_BACKOFF_MAX_SECONDS = 30.0
DEBOUNCE_SECONDS = 0.15
PICKUP_TOLERANCE_PERCENT = 0
PICKUP_CAPTURE_WINDOW_PERCENT = 5

# qbzd 2.0.2 locked-mode line. The captured group is the remote volume in 0..1.
_IGNORED_VOLUME_RE = re.compile(
    r"volume_mode=locked:\s*ignoring remote SetVolume\(((?:0(?:\.\d+)?)|(?:1(?:\.0+)?))\)"
    r";\s*player stays at 100%"
)

# Software-mode line: qbzd applies the remote volume itself. The journal
# coupling needs locked mode, so seeing this line means the config drifted.
_APPLIED_VOLUME_RE = re.compile(r"Renderer command applied:\s*SetVolume\s*\{")

# Rate-limit the software-mode warning (the line repeats per drag step).
_WARN_EVERY_SECONDS = 300.0


def parse_ignored_volume(line: str | None) -> int | None:
    """Return the remote volume percent from a qbzd locked-mode journal line.

    Returns ``None`` for software-mode, malformed or unrelated lines so the
    watch stays a silent no-op unless qbzd is actually locked.
    """
    if not line:
        return None
    match = _IGNORED_VOLUME_RE.search(line)
    if match is None:
        return None
    percent = int(round(float(match.group(1)) * 100))
    return max(0, min(100, percent))


def is_software_volume_apply(line: str | None) -> bool:
    """Return whether the line shows qbzd applying volume itself (software mode)."""
    if not line:
        return False
    return _APPLIED_VOLUME_RE.search(line) is not None


class QobuzRemoteVolumeTranslator:
    """Apply remote volume only after a safe pickup of the canonical volume.

    The first remote value establishes the side of the canonical value and is
    observed only. A later movement must cross that value before writes are
    enabled. This state is provider-neutral even though Qobuz is its current
    journal source.
    """

    def __init__(
        self,
        is_active: Callable[[], bool],
        apply_volume: Callable[[int], Awaitable[Any]],
        get_canonical_volume: Callable[[], int] | None = None,
        tolerance: int = PICKUP_TOLERANCE_PERCENT,
        capture_window: int = PICKUP_CAPTURE_WINDOW_PERCENT,
    ) -> None:
        self.is_active = is_active
        self.apply_volume = apply_volume
        self.get_canonical_volume = get_canonical_volume
        self.tolerance = max(0, int(tolerance))
        self.capture_window = max(0, int(capture_window))
        self._pending: int | None = None
        self._pending_generation: int | None = None
        self._last_remote: int | None = None
        self._pickup_target: int | None = None
        self._picked_up = False
        self._armed_at_target = False
        self._last_canonical: int | None = None
        self._generation = 0

    @property
    def pending(self) -> int | None:
        return self._pending

    @property
    def picked_up(self) -> bool:
        return self._picked_up

    @property
    def pending_generation(self) -> int | None:
        return self._pending_generation

    def is_generation_current(self, generation: int | None) -> bool:
        return generation is not None and generation == self._generation

    def reset(self) -> None:
        """Forget a provider/ownership session without touching the master."""
        self._pending = None
        self._pending_generation = None
        self._last_remote = None
        self._pickup_target = None
        self._picked_up = False
        self._armed_at_target = False
        self._last_canonical = None
        self._generation += 1

    def canonical_volume_changed(self, percent: int) -> None:
        """Invalidate pickup on an unrelated canonical volume write."""
        value = max(0, min(100, int(round(float(percent)))))
        if self._last_canonical is not None and value != self._last_canonical:
            self.reset()
        self._last_canonical = value

    def canonical_volume_written(self, percent: int, generation: int | None) -> None:
        """Record a validated remote write without treating it as external."""
        if not self.is_generation_current(generation):
            return
        self._last_canonical = max(0, min(100, int(round(float(percent)))))

    def submit(self, percent: int) -> bool:
        """Record a remote volume intent; ``False`` when Qobuz does not own
        the playback context (e.g. radio is playing) and the intent is dropped.

        The owner is re-checked again in :meth:`flush` right before the master
        write: an owner switch inside the debounce window must discard the
        stale pending value instead of applying a Qobuz intent afterwards.
        """
        if not self.is_active():
            self.reset()
            return False
        percent = max(0, min(100, int(round(float(percent)))))
        if self.get_canonical_volume is None:
            # Kept for small standalone callers; the live integration always
            # supplies the canonical read and therefore always picks up.
            self._pending = percent
            return True
        canonical = max(0, min(100, int(self.get_canonical_volume())))
        if self._last_canonical is None:
            self._last_canonical = canonical
        elif canonical != self._last_canonical:
            self.reset()
            self._last_canonical = canonical
        if self._last_remote is None:
            self._last_remote = percent
            self._pickup_target = canonical
            if abs(percent - canonical) <= self.tolerance:
                # The initial value is already aligned: arm without writing,
                # then accept the first small movement in either direction.
                self._picked_up = True
                self._armed_at_target = True
            return False
        if self._armed_at_target:
            self._last_remote = percent
            if percent == self._pickup_target:
                return False
            if not (
                self._pickup_target - self.capture_window
                <= percent
                <= self._pickup_target + self.capture_window
            ):
                return False
            self._armed_at_target = False
            self._pending = percent
            self._pending_generation = self._generation
            return True
        if not self._picked_up:
            started_above = self._last_remote > self._pickup_target + self.tolerance
            crossed = (
                self._pickup_target - self.capture_window <= percent <= self._pickup_target + self.tolerance
                if started_above
                else self._pickup_target - self.tolerance <= percent <= self._pickup_target + self.capture_window
            )
            self._last_remote = percent
            if not crossed:
                return False
            self._picked_up = True
            if percent == self._pickup_target:
                return False
        self._last_remote = percent
        self._pending = percent
        self._pending_generation = self._generation
        return True

    async def flush(self) -> None:
        if self._pending is None:
            return
        if not self.is_active():
            # The owner changed inside the debounce window (e.g. to Tidal or
            # Spotify): the pending Qobuz intent is stale and must never touch
            # the FXRoute master anymore.
            self._pending = None
            return
        percent = self._pending
        generation = self._pending_generation
        self._pending = None
        self._pending_generation = None
        if self.get_canonical_volume is None:
            await self.apply_volume(percent)
        else:
            await self.apply_volume(percent, generation)
        self.canonical_volume_written(percent, generation)


@dataclass
class QobuzVolumeWatchDependencies:
    """Live services the qbzd journal volume watch needs."""

    is_active: Callable[[], bool]
    apply_volume: Callable[[int], Awaitable[Any]]
    get_canonical_volume: Callable[[], int] | None = None


class QobuzVolumeWatch:
    """Tail qbzd's journal and route locked-mode remote volume intents to master."""

    def __init__(
        self,
        deps: QobuzVolumeWatchDependencies,
        *,
        journal_command: list[str] | None = None,
        debounce_seconds: float = DEBOUNCE_SECONDS,
    ) -> None:
        self._deps = deps
        self._journal_command = list(journal_command or JOURNALCTL_COMMAND)
        self._debounce_seconds = debounce_seconds
        self._translator = QobuzRemoteVolumeTranslator(
            is_active=deps.is_active,
            apply_volume=deps.apply_volume,
            get_canonical_volume=deps.get_canonical_volume,
        )
        self.watch_task: asyncio.Task | None = None
        self._drain_task: asyncio.Task | None = None
        self._backoff_seconds = 0.0
        # None means "never warned"; 0.0 would suppress the first warning while
        # the monotonic uptime is still below the rate-limit window.
        self._software_warned_at: float | None = None

    def _warn_software_mode_once(self, now: float) -> None:
        if self._software_warned_at is not None and now - self._software_warned_at < _WARN_EVERY_SECONDS:
            return
        self._software_warned_at = now
        logger.warning(
            "qBZD applied a remote SetVolume itself: qconnect.volume_mode must be "
            "'locked' for the Qobuz phone slider to drive the FXRoute master "
            "(run: qbzd settings set qconnect.volume_mode locked && systemctl "
            "--user restart qbzd)"
        )

    def reset_pickup(self) -> None:
        self._translator.reset()

    def canonical_volume_changed(self, percent: int) -> None:
        self._translator.canonical_volume_changed(percent)

    def canonical_volume_written(self, percent: int, generation: int | None) -> None:
        self._translator.canonical_volume_written(percent, generation)

    def is_generation_current(self, generation: int | None) -> bool:
        return self._translator.is_generation_current(generation)

    async def _sleep(self, delay: float) -> None:
        await asyncio.sleep(delay)

    def _next_backoff(self) -> float:
        if self._backoff_seconds <= 0.0:
            self._backoff_seconds = RESTART_BACKOFF_BASE_SECONDS
        else:
            self._backoff_seconds = min(
                RESTART_BACKOFF_MAX_SECONDS,
                self._backoff_seconds + RESTART_BACKOFF_BASE_SECONDS,
            )
        return self._backoff_seconds

    def _schedule_drain(self) -> None:
        if self._drain_task is not None and not self._drain_task.done():
            return
        self._drain_task = asyncio.create_task(self._drain_pending(), name="qobuz-journal-volume-drain")

    async def _drain_pending(self) -> None:
        try:
            first_flush = True
            while True:
                if first_flush and self._debounce_seconds > 0:
                    await self._sleep(self._debounce_seconds)
                first_flush = False
                try:
                    await self._translator.flush()
                except Exception as exc:
                    # A later intent may have arrived while the failed write
                    # was in flight; keep this drain responsible for it.
                    logger.warning("Qobuz journal volume drain failed: %s", exc)
                if self._translator.pending is None:
                    break
        except asyncio.CancelledError:
            raise
        finally:
            self._drain_task = None

    async def run_watch_loop(self) -> None:
        logger.info("Qobuz qbzd journal volume watch loop entered")
        proc: asyncio.subprocess.Process | None = None
        expect_ignore = False
        while True:
            try:
                if proc is None or proc.returncode is not None:
                    proc = await asyncio.create_subprocess_exec(
                        *self._journal_command,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                line = await proc.stdout.readline()
                if not line:
                    # EOF: journalctl exited (e.g. after a journal rotation);
                    # respawn after a bounded backoff so a wedged journalctl
                    # cannot busy-loop the event loop.
                    proc = None
                    expect_ignore = False
                    await self._sleep(self._next_backoff())
                    continue
                self._backoff_seconds = 0.0
                text = line.decode("utf-8", errors="replace")
                percent = parse_ignored_volume(text)
                if percent is not None:
                    # Locked-mode pair: the engine ignore line right after the
                    # sink apply line proves qbzd stays at unity.
                    expect_ignore = False
                    if self._deps.is_active():
                        self._translator.submit(percent)
                        self._schedule_drain()
                elif is_software_volume_apply(text):
                    if expect_ignore:
                        # A second apply before any ignore line: real
                        # software-mode burst (qbzd attenuates itself).
                        self._warn_software_mode_once(time.monotonic())
                    expect_ignore = True
                elif expect_ignore:
                    # The apply line was not followed by its locked-mode
                    # ignore pair: qbzd applied the volume itself.
                    self._warn_software_mode_once(time.monotonic())
                    expect_ignore = False
            except asyncio.CancelledError:
                if proc is not None:
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass
                raise
            except Exception as exc:
                logger.warning("Qobuz qbzd journal volume watch pass failed: %s", exc)
                proc = None
                expect_ignore = False
                await self._sleep(self._next_backoff())

    async def stop(self) -> None:
        if self._drain_task is not None and not self._drain_task.done():
            self._drain_task.cancel()
        self._drain_task = None
        if self.watch_task is not None and not self.watch_task.done():
            self.watch_task.cancel()
        self.watch_task = None
