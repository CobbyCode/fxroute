# SPDX-License-Identifier: AGPL-3.0-only
"""Hardware media keys through FXRoute's own MPRIS player.

Desktops deliver Play/Pause, Next and Previous keys to an MPRIS player on the
session bus (GNOME: gsd-media-keys). ``playback/mpris_helper.py`` owns
FXRoute's player there and claims the keys back from other players (e.g. a
paused Spotify Desktop). This module keeps the helper running, publishes the
state of the active FXRoute source to it and routes its commands to the same
global transport as the footer controls: the committed playback owner is
controlled, and nothing else is started. Line inputs (external, Bluetooth,
STDIN) have no transport, so media keys do nothing there.

No imports from ``main``: the state reader and the transport actions are
injected by the composition root.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

logger = logging.getLogger(__name__)

HELPER_PATH = Path(__file__).resolve().parent / "mpris_helper.py"
APP_PLAYBACK_MODE = "app-playback"
STREAMING_OWNERS = frozenset({"spotify", "qobuz"})
PLAYBACK_STATUSES = ("Playing", "Paused", "Stopped")

STATE_POLL_SECONDS = 1.0
RESTART_DELAY_SECONDS = 2.0
# A helper that lived this long restarts after RESTART_DELAY_SECONDS again;
# one that keeps dying sooner backs off up to UNAVAILABLE_RETRY_SECONDS.
STABLE_RUN_SECONDS = 30.0
# Exit status of the helper when the session bus or its bindings are
# missing (mpris_helper.EXIT_UNAVAILABLE); retried rarely and quietly.
HELPER_EXIT_UNAVAILABLE = 3
UNAVAILABLE_RETRY_SECONDS = 60.0


def mpris_state(
    *,
    source_mode: str | None,
    owner: str | None,
    player_state: dict | None,
    track: dict | None,
    last_track: dict | None,
    spotify_state: dict | None,
    qobuz_state: dict | None,
    queue_count: int,
) -> dict:
    """Describe the active FXRoute source as the MPRIS player state."""
    if (source_mode or APP_PLAYBACK_MODE) != APP_PLAYBACK_MODE:
        return {
            "source": source_mode, "transport": False, "status": "Stopped",
            "title": "", "artist": "", "album": "",
            "can_play": False, "can_pause": False, "can_go_next": False, "can_go_previous": False,
        }
    if owner in STREAMING_OWNERS:
        data = (spotify_state if owner == "spotify" else qobuz_state) or {}
        status = data.get("status") if data.get("status") in PLAYBACK_STATUSES else "Stopped"
        title, artist, album = (str(data.get(key) or "") for key in ("title", "artist", "album"))
        # Same condition as the footer's streaming transport buttons.
        has_media = bool(data.get("available") and (title or artist or album or status != "Stopped"))
        return {
            "source": owner, "transport": True, "status": status,
            "title": title, "artist": artist, "album": album,
            "can_play": has_media and status != "Playing",
            "can_pause": status == "Playing",
            "can_go_next": has_media,
            "can_go_previous": has_media,
        }
    player_state = player_state or {}
    loaded = bool(player_state.get("current_file")) and not player_state.get("ended")
    if loaded and player_state.get("paused"):
        status = "Paused"
    elif loaded and player_state.get("playing"):
        status = "Playing"
    else:
        status = "Stopped"
    shown = track or last_track or {}
    return {
        "source": owner or (shown.get("source") if shown else None),
        "transport": True,
        "status": status,
        "title": str(shown.get("title") or ""),
        "artist": str(shown.get("artist") or ""),
        "album": str(shown.get("album") or ""),
        # The toggle resumes the loaded track or replays the last one.
        "can_play": status != "Playing" and bool(track or last_track),
        "can_pause": status == "Playing",
        "can_go_next": queue_count > 1,
        "can_go_previous": queue_count > 1,
    }


def route_command(command: str, state: dict) -> str | None:
    """Map an MPRIS command to a global transport action, or None."""
    if not state.get("transport"):
        return None
    playing = state.get("status") == "Playing"
    if command == "toggle":
        return "toggle" if playing or state.get("can_play") else None
    if command == "play":
        return "toggle" if not playing and state.get("can_play") else None
    if command in ("pause", "stop"):
        return "toggle" if playing else None
    if command == "next":
        return "next" if state.get("can_go_next") else None
    if command == "previous":
        return "previous" if state.get("can_go_previous") else None
    return None


@dataclass
class MediaKeyDependencies:
    """Live services the media-key bridge needs."""

    get_state: Callable[[], dict]
    run_action: Callable[[str], Awaitable[Any]]


class MediaKeyBridge:
    """Run the MPRIS helper, feed it state and execute its commands."""

    def __init__(
        self,
        deps: MediaKeyDependencies,
        *,
        command: Sequence[str] | None = None,
        poll_seconds: float = STATE_POLL_SECONDS,
        restart_seconds: float = RESTART_DELAY_SECONDS,
        unavailable_retry_seconds: float = UNAVAILABLE_RETRY_SECONDS,
    ) -> None:
        self._deps = deps
        self._command = list(command) if command else [str(HELPER_PATH)]
        self._poll_seconds = poll_seconds
        self._restart_seconds = restart_seconds
        self._unavailable_retry_seconds = unavailable_retry_seconds
        self.task: asyncio.Task | None = None
        self.action_task: asyncio.Task | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._wake = asyncio.Event()
        self._unavailable_reported = False

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name="media-key-bridge")

    async def stop(self) -> None:
        tasks = [task for task in (self.task, self.action_task) if task is not None]
        self.task = None
        self.action_task = None
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._terminate()

    async def _terminate(self) -> None:
        proc, self._process = self._process, None
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

    async def _run(self) -> None:
        delay = self._restart_seconds
        while True:
            started = time.monotonic()
            returncode = await self._run_helper()
            if returncode == HELPER_EXIT_UNAVAILABLE:
                await asyncio.sleep(self._unavailable_retry_seconds)
                continue
            if time.monotonic() - started >= STABLE_RUN_SECONDS:
                delay = self._restart_seconds
            else:
                delay = min(delay * 2, self._unavailable_retry_seconds)
            await asyncio.sleep(delay)

    def _report_unavailable(self, reason: str) -> None:
        if self._unavailable_reported:
            logger.debug("MPRIS media-key helper still unavailable: %s", reason)
            return
        self._unavailable_reported = True
        logger.warning("MPRIS media-key helper unavailable; media keys stay with the desktop: %s", reason)

    async def _run_helper(self) -> int | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            self._report_unavailable(str(exc))
            return HELPER_EXIT_UNAVAILABLE
        self._process = proc
        workers = [
            asyncio.create_task(self._read_commands(proc), name="media-key-commands"),
            asyncio.create_task(self._read_log(proc), name="media-key-helper-log"),
            asyncio.create_task(self._push_states(proc), name="media-key-state"),
        ]
        try:
            returncode = await proc.wait()
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            if self._process is proc:
                self._process = None
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        if returncode == HELPER_EXIT_UNAVAILABLE:
            self._report_unavailable(f"helper exited with status {returncode}")
        else:
            logger.warning("MPRIS media-key helper exited with status %s; restarting", returncode)
        return returncode

    async def _read_log(self, proc: asyncio.subprocess.Process) -> None:
        while line := await proc.stderr.readline():
            text = line.decode(errors="replace").strip()
            if text:
                logger.info("%s", text)
                if "owns org.mpris" in text:
                    self._unavailable_reported = False

    async def _read_commands(self, proc: asyncio.subprocess.Process) -> None:
        while line := await proc.stdout.readline():
            try:
                command = json.loads(line).get("command")
            except (ValueError, AttributeError):
                continue
            if isinstance(command, str):
                self.handle_command(command)

    def handle_command(self, command: str) -> None:
        """Start one command; a press during a running action is dropped."""
        if self.action_task is not None and not self.action_task.done():
            logger.info("Media key %s ignored: the previous media key action is still running", command)
            return
        self.action_task = asyncio.create_task(self._execute(command), name="media-key-action")

    async def _execute(self, command: str) -> None:
        try:
            state = self._deps.get_state()
        except Exception as exc:
            logger.warning("Media key %s ignored: state unavailable: %s", command, exc)
            return
        action = route_command(command, state)
        logger.info(
            "Media key %s: source=%s status=%s action=%s",
            command, state.get("source"), state.get("status"), action or "none",
        )
        if action is None:
            return
        try:
            await self._deps.run_action(action)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("Media key %s: %s failed: %s", command, action, exc)
        finally:
            self._wake.set()

    async def _push_states(self, proc: asyncio.subprocess.Process) -> None:
        last_line = None
        while True:
            try:
                line = json.dumps(self._deps.get_state(), sort_keys=True)
            except Exception as exc:
                logger.debug("Media key state unavailable: %s", exc)
                line = None
            if line is not None and line != last_line:
                proc.stdin.write(line.encode() + b"\n")
                await proc.stdin.drain()
                last_line = line
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_seconds)
            except asyncio.TimeoutError:
                pass
