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

Remote values are translated into master *deltas* (see
:mod:`playback.remote_volume`): the first value after a Connect activation is
the controller scale's anchor and never moves the master — live-verified on
.104, the Qobuz app pushes its own media volume as an absolute SetVolume right
after every activation, which previously hijacked the master (37 -> 100).

The parser only recognizes locked-mode lines, so in ``software`` mode this
watch is a silent no-op. The translator debounces the drag burst (a phone drag
emits one line per step) so the net delta of a gesture is applied exactly once.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from playback.remote_volume import RemoteVolumeDeltaTranslator

logger = logging.getLogger(__name__)

# Tail only new lines: journalctl -f already streams; -n 0 avoids replaying
# pre-start history (FXRoute must not apply volumes from before its start).
JOURNALCTL_COMMAND = [
    "journalctl", "--user", "-u", "qbzd.service", "-f", "-o", "cat", "-n", "0",
]
RESTART_BACKOFF_BASE_SECONDS = 1.0
RESTART_BACKOFF_MAX_SECONDS = 30.0
DEBOUNCE_SECONDS = 0.15

# qbzd 2.0.2 locked-mode line. The captured group is the remote volume in 0..1.
_IGNORED_VOLUME_RE = re.compile(
    r"volume_mode=locked:\s*ignoring remote SetVolume\(((?:0(?:\.\d+)?)|(?:1(?:\.0+)?))\)"
    r";\s*player stays at 100%"
)

# Connect session activation/deactivation. The app pushes its own media volume
# as an absolute remote SetVolume shortly after every activation; that value is
# a scale anchor, not user intent, so both transitions reset the anchor.
_SESSION_ACTIVE_RE = re.compile(r"SET_ACTIVE payload=\{\"active\":true\}")
_SESSION_INACTIVE_RE = re.compile(r"SET_ACTIVE payload=\{\"active\":false\}")

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


def is_session_activation(line: str | None) -> bool:
    """Return whether the line activates the Connect session on this device."""
    if not line:
        return False
    return _SESSION_ACTIVE_RE.search(line) is not None


def is_session_deactivation(line: str | None) -> bool:
    """Return whether the line deactivates the Connect session on this device."""
    if not line:
        return False
    return _SESSION_INACTIVE_RE.search(line) is not None


def is_software_volume_apply(line: str | None) -> bool:
    """Return whether the line shows qbzd applying volume itself (software mode)."""
    if not line:
        return False
    return _APPLIED_VOLUME_RE.search(line) is not None


# Historical name for the shared delta translator; the Qobuz bridge was the
# first consumer and tests reference it by this name.
QobuzRemoteVolumeTranslator = RemoteVolumeDeltaTranslator


@dataclass
class QobuzVolumeWatchDependencies:
    """Live services the qbzd journal volume watch needs."""

    is_active: Callable[[], bool]
    apply_volume_delta: Callable[[int], Awaitable[Any]]


class QobuzVolumeWatch:
    """Tail qbzd's journal and route locked-mode remote volume deltas to master."""

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
        self._translator = RemoteVolumeDeltaTranslator(
            is_active=deps.is_active,
            apply_volume_delta=deps.apply_volume_delta,
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
            if self._debounce_seconds > 0:
                await self._sleep(self._debounce_seconds)
            await self._translator.flush()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The drain runs as a detached task: a failing master write must be
            # observed here (never an unretrieved task exception) while the
            # watch loop stays alive for the next intent.
            logger.warning("Qobuz journal volume drain failed: %s", exc)
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
                if is_session_activation(text) or is_session_deactivation(text):
                    # A fresh Connect session re-anchors the controller scale;
                    # the app's post-activation sync push must not move master.
                    expect_ignore = False
                    self._translator.observe_activation()
                    continue
                percent = parse_ignored_volume(text)
                if percent is not None:
                    # Locked-mode pair: the engine ignore line right after the
                    # sink apply line proves qbzd stays at unity.
                    expect_ignore = False
                    if self._deps.is_active() and self._translator.submit(percent):
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
