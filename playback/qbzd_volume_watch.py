# SPDX-License-Identifier: AGPL-3.0-only

"""Qobuz (qbzd) journal-driven remote volume-to-master coupling.

qbzd 2.0.2 decouples QConnect volume from its applied gain only when
``qconnect.volume_mode=locked``: remote ``SetVolume`` commands are ignored for
the engine ("player stays at 100%") while the connect session volume follows
the phone slider. There is no volume event on ``/api/events`` and no HTTP
endpoint carrying the session volume (live-verified on .104), so the phone
intent is observable only through the daemon journal line::

    [QConnect] volume_mode=locked: ignoring remote SetVolume(0.450); player stays at 100%

This module polls that line via cursor-anchored ``journalctl --user-unit``
one-shot queries and maps each intent onto the canonical FXRoute master
volume. qbzd's own gain is never written here; the unity pin lives at the
Qobuz claim/start path (``set_volume(100)`` works in locked mode through
the local control plane).

Remote values are translated with **pickup semantics** (see
:class:`playback.remote_volume.RemoteVolumePickupTranslator`): the
connect-time push (the phone's media volume, live-verified on .104 — a
deactivate/reactivate pushed 98% while the session slider sat at ~50%) only
anchors the controller scale and never writes; a gesture takes over the
master only when it crosses the current master level, and from the pickup on
the master tracks the controller value absolutely, so both displays show the
same number without any jump.

Contract history: the 2026-08-21 design anchored the push and applied deltas
forever — the controller and master displays never matched (phone 0% vs
master 13%). The 2026-08-28 absolute-adoption design fixed the matching but
reintroduced the connect-time hijack (master jumped to the pushed 98%). The
pickup contract keeps both properties: no connect-time write, absolute sync
after the gesture crosses the master level.

The parser only recognizes locked-mode lines, so in ``software`` mode this
watch is a silent no-op. The translator debounces the drag burst (a phone drag
emits one line per step) so each debounce window writes the latest value once.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from playback.remote_volume import RemoteVolumePickupTranslator

logger = logging.getLogger(__name__)

# One-shot journal query base. A `-f` streaming tail demonstrably goes blind
# on hosts with heavy journal rotation: it stays pinned to a stale file
# while new entries land elsewhere, with no EOF or error to recover from.
# Cursor-anchored polling re-resolves the files on every pass
# (rotation- and split-proof) and never replays pre-start history.
# --user-unit (not --user -u) resolves the unit's entries across split files.
JOURNALCTL_QUERY_BASE = [
    "journalctl", "--user-unit=qbzd.service", "-o", "cat", "--no-pager",
]
POLL_INTERVAL_SECONDS = 0.5
POLL_READ_TIMEOUT_SECONDS = 10.0
RESTART_BACKOFF_BASE_SECONDS = 1.0
RESTART_BACKOFF_MAX_SECONDS = 30.0
DEBOUNCE_SECONDS = 0.15
OWNER_STATE_POLL_INTERVAL_SECONDS = 0.1

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


# Historical name for the Qobuz volume translator; the Qobuz bridge was the
# first consumer and tests reference it by this name.
QobuzRemoteVolumeTranslator = RemoteVolumePickupTranslator


@dataclass
class QobuzVolumeWatchDependencies:
    """Live services the qbzd journal volume watch needs."""

    is_active: Callable[[], bool]
    apply_volume_value: Callable[[int], Awaitable[Any]]
    # Non-blocking read of the canonical master percent; the pickup rule
    # needs it to detect a gesture crossing the current master level.
    current_master: Callable[[], int]
    # Optional: notified when the journal shows this device becoming (True)
    # or ceasing to be (False) the actively selected Connect renderer.
    on_device_active: Callable[[bool], None] | None = None


class QobuzVolumeWatch:
    """Tail qbzd's journal and route locked-mode remote volumes to the master."""

    def __init__(
        self,
        deps: QobuzVolumeWatchDependencies,
        *,
        debounce_seconds: float = DEBOUNCE_SECONDS,
        poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self._deps = deps
        self._debounce_seconds = debounce_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._translator = RemoteVolumePickupTranslator(
            is_active=deps.is_active,
            apply_volume_value=deps.apply_volume_value,
            current_master=deps.current_master,
        )
        self.watch_task: asyncio.Task | None = None
        self._drain_task: asyncio.Task | None = None
        self._owner_monitor_task: asyncio.Task | None = None
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

    async def _monitor_owner_state(self) -> None:
        """Reset the remote scale when playback ownership changes silently."""
        try:
            previous = bool(self._deps.is_active())
        except Exception:
            previous = False
        while True:
            await asyncio.sleep(OWNER_STATE_POLL_INTERVAL_SECONDS)
            try:
                current = bool(self._deps.is_active())
            except Exception:
                continue
            if current != previous:
                previous = current
                self._translator.observe_activation()

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
            while True:
                if self._debounce_seconds > 0:
                    await self._sleep(self._debounce_seconds)
                try:
                    await self._translator.flush()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Keep the latest intent pending and retry after a bounded
                    # delay; a transient master-write failure must not strand it.
                    logger.warning("Qobuz journal volume drain failed: %s", exc)
                    await self._sleep(max(self._debounce_seconds, 0.5))
                    continue
                # A journal event can arrive while the async canonical write
                # above is still in flight. Drain that final delta before the
                # active drain task is released.
                if self._translator.pending is None:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The drain runs as a detached task: a failing master write must be
            # observed here (never an unretrieved task exception) while the
            # watch loop stays alive for the next intent.
            logger.warning("Qobuz journal volume drain failed: %s", exc)
        finally:
            self._drain_task = None

    async def _bootstrap_device_state(self) -> None:
        """Recover the last selection state from recent journal history.

        The tail starts at ``-n 0`` (no replay), so without this one-shot scan
        a fresh FXRoute start would not know whether the device is currently
        selected until the next switch happens.
        """
        if self._deps.on_device_active is None:
            return
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "journalctl", "--user-unit=qbzd.service",
                "-n", "500", "-o", "cat", "--no-pager",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            last: bool | None = None
            for line in stdout.decode("utf-8", errors="replace").splitlines():
                if is_session_activation(line):
                    last = True
                elif is_session_deactivation(line):
                    last = False
            if last is not None:
                self._deps.on_device_active(last)
        except (asyncio.TimeoutError, OSError) as exc:
            logger.debug("qbzd device-state bootstrap failed: %s", exc)
        finally:
            if proc is not None and proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    proc.kill()

    async def _poll_journal_lines(self, cursor: str | None) -> tuple[list[str], str | None]:
        """Return new qbzd journal lines since cursor (cursor-anchored poll).

        Each poll re-resolves the journal files, so rotation, split files or
        vacuumed archives can never strand the reader on a dead file the way
        a streaming ``-f`` tail does. A missing cursor anchors at now (no
        replay of pre-start history); an unusable cursor re-anchors the same
        way instead of failing the loop.
        """
        args = [*JOURNALCTL_QUERY_BASE, "--show-cursor"]
        if cursor is None:
            args += ["--lines=0"]
        else:
            args += ["--after-cursor", cursor]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=POLL_READ_TIMEOUT_SECONDS
            )
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
        if proc.returncode != 0:
            return [], None
        lines: list[str] = []
        new_cursor = cursor
        for raw in stdout.decode("utf-8", errors="replace").splitlines():
            if raw.startswith("-- cursor:"):
                new_cursor = raw.partition(":")[2].strip() or new_cursor
            else:
                lines.append(raw)
        return lines, new_cursor

    async def run_watch_loop(self) -> None:
        logger.info("Qobuz qbzd journal volume watch loop entered")
        await self._bootstrap_device_state()
        owner_monitor = asyncio.create_task(
            self._monitor_owner_state(),
            name="qobuz-volume-owner-monitor",
        )
        self._owner_monitor_task = owner_monitor
        expect_ignore = False
        cursor: str | None = None
        try:
            while True:
                try:
                    lines, cursor = await self._poll_journal_lines(cursor)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Qobuz qbzd journal poll failed: %s", exc)
                    expect_ignore = False
                    await self._sleep(self._next_backoff())
                    continue
                self._backoff_seconds = 0.0
                for text in lines:
                    if is_session_activation(text) or is_session_deactivation(text):
                        # A fresh Connect session re-anchors the controller scale;
                        # the app's post-activation sync push must not move master.
                        expect_ignore = False
                        self._translator.observe_activation()
                        if self._deps.on_device_active is not None:
                            self._deps.on_device_active(is_session_activation(text))
                        continue
                    percent = parse_ignored_volume(text)
                    if percent is not None:
                        # Locked-mode pair: the engine ignore line right after the
                        # sink apply line proves qbzd stays at unity.
                        expect_ignore = False
                        if self._translator.submit(percent):
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
                await self._sleep(self._poll_interval_seconds)
        finally:
            if self._owner_monitor_task is owner_monitor:
                self._owner_monitor_task = None
            if not owner_monitor.done():
                owner_monitor.cancel()
            await asyncio.gather(owner_monitor, return_exceptions=True)

    async def stop(self) -> None:
        drain_task = self._drain_task
        watch_task = self.watch_task
        owner_monitor_task = self._owner_monitor_task
        for task in (drain_task, watch_task, owner_monitor_task):
            if task is not None and not task.done():
                task.cancel()
        tasks = [
            task for task in (drain_task, watch_task, owner_monitor_task)
            if task is not None
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._translator.observe_activation()
        self._drain_task = None
        self._owner_monitor_task = None
        self.watch_task = None
