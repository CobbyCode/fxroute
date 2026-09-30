# SPDX-License-Identifier: AGPL-3.0-only
"""Own the local PCM listener, one writer session, and source selection."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import select
import socket
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable
from uuid import uuid4

from audio.stdin_pipewire import PipeWirePcmSink
from audio.stdin_protocol import CHUNK_BYTES, MAX_HEADER_BYTES, FrameAssembler, PcmSpec, encode_event, parse_spec
from common.process_stop import run_stop_shielded

logger = logging.getLogger(__name__)


@dataclass
class StdinInputDependencies:
    sink_factory: Callable[[PcmSpec, str], PipeWirePcmSink]
    on_state_changed: Callable[[], Awaitable[None]]
    monotonic: Callable[[], float] = time.monotonic
    adapter_error: Callable[[], str | None] = lambda: None


class _StreamError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class _Session:
    spec: PcmSpec
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    task: asyncio.Task
    id: str = field(default_factory=lambda: uuid4().hex)
    sink: PipeWirePcmSink | None = None
    state: str = "connected"
    routed: bool = False
    frames_received: int = 0
    last_frame_at: float | None = None
    stop_code: str = "service-stopped"
    children: list[asyncio.Task] = field(default_factory=list)


class StdinInputService:
    def __init__(self, deps: StdinInputDependencies):
        self._deps = deps
        self._lock = asyncio.Lock()
        self._selection = asyncio.Event()
        self._selected = False
        self._measurement_active = False
        self._server: asyncio.Server | None = None
        self._session: _Session | None = None
        self._clients: set[asyncio.Task] = set()
        self._notify_task: asyncio.Task | None = None
        self._notify_dirty = False
        self._stopped = False
        self._path: Path | None = None
        self._inode: int | None = None
        self._error: dict | None = None

    def snapshot(self) -> dict[str, object]:
        session = self._session
        available = self._server is not None and self._server.is_serving()
        state = "waiting" if available else "unavailable"
        if session:
            state = session.state
            if state in {"ready", "streaming"}:
                recent = session.last_frame_at is not None and self._deps.monotonic() - session.last_frame_at <= 1
                state = "streaming" if self._selected and session.routed and recent else "ready"
        elif available and self._error:
            state = "error"
        spec = session.spec if session else None
        return {
            "available": available, "selectable": available, "state": state,
            "selected": self._selected, "session_id": session.id if session else None,
            **{key: getattr(spec, key) if spec else None for key in ("format", "rate", "channels", "left", "right")},
            "routed": bool(session and session.routed and self._selected),
            "frames_received": session.frames_received if session else 0,
            "error": dict(self._error) if self._error else None,
            "measurement_active": self._measurement_active,
        }

    def _notify(self) -> None:
        if self._stopped:
            # stop() already ran its cancellation pass; a later notify (the
            # measurement release fires after the service stops) would spawn
            # a task nobody awaits and that outlives the shutdown.
            return
        self._notify_dirty = True
        if self._notify_task is None or self._notify_task.done():
            self._notify_task = asyncio.create_task(self._publish(), name="stdin-state-notify")

    async def _publish(self) -> None:
        while self._notify_dirty:
            self._notify_dirty = False
            try:
                await self._deps.on_state_changed()
            except Exception:
                logger.exception("STDIN state notification failed")

    @staticmethod
    def _prepare_socket(path: Path) -> socket.socket:
        path.parent.mkdir(mode=0o700, exist_ok=True)
        parent = path.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid():
            raise RuntimeError("STDIN runtime directory must belong to the audio user")
        path.parent.chmod(0o700)
        if path.exists() or path.is_symlink():
            previous = path.lstat()
            if previous.st_uid != os.getuid() or not stat.S_ISSOCK(previous.st_mode):
                raise RuntimeError("STDIN socket path is occupied by another file")
            with socket.socket(socket.AF_UNIX) as probe:
                probe.settimeout(0.2)
                try:
                    probe.connect(str(path))
                except ConnectionRefusedError:
                    if path.lstat().st_ino != previous.st_ino:
                        raise RuntimeError("STDIN socket changed during startup")
                    path.unlink()
                else:
                    raise RuntimeError("STDIN listener is already running")
        listener = socket.socket(socket.AF_UNIX)
        try:
            listener.bind(str(path))
            path.chmod(0o600)
            listener.setblocking(False)
            return listener
        except BaseException:
            listener.close()
            raise

    async def start(self, path: Path) -> None:
        self._stopped = False
        listener = None
        try:
            error = self._deps.adapter_error()
            if error:
                raise RuntimeError(error)
            listener = self._prepare_socket(path)
            self._path, self._inode = path, path.lstat().st_ino
            self._server = await asyncio.start_unix_server(self._accept, sock=listener, limit=MAX_HEADER_BYTES)
            self._error = None
        except (OSError, RuntimeError) as exc:
            if listener is not None:
                listener.close()
            self._error = {"code": "unavailable", "message": str(exc)}
            logger.warning("STDIN listener unavailable: %s", exc)
        self._notify()

    async def _send(self, writer: asyncio.StreamWriter, event: str, **fields) -> None:
        writer.write(encode_event(event, **fields))
        await asyncio.wait_for(writer.drain(), 1)

    async def _wait_selected(self, session: _Session) -> bool:
        if self._selected:
            return True
        selected = asyncio.create_task(self._selection.wait())
        closed = asyncio.create_task(session.reader.read(1))
        try:
            done, _ = await asyncio.wait((selected, closed), return_when=asyncio.FIRST_COMPLETED)
            if closed in done:
                if closed.result():
                    raise _StreamError("protocol-error", "Wait for ready before sending PCM")
                return False
            return True
        finally:
            for task in (selected, closed):
                task.cancel()
            await asyncio.gather(selected, closed, return_exceptions=True)

    @staticmethod
    def _peer_closed(writer: asyncio.StreamWriter) -> bool:
        connection = writer.get_extra_info("socket")
        if connection is None or connection.fileno() < 0:
            return writer.is_closing()
        # Backpressure can pause socket reads before buffered PCM reaches EOF.
        # Poll observes peer shutdown without consuming or discarding PCM.
        poller = select.poll()
        poller.register(connection.fileno(), select.POLLHUP | select.POLLRDHUP | select.POLLERR)
        return bool(poller.poll(0))

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._clients.add(task)
        session = None
        try:
            try:
                header = await asyncio.wait_for(reader.readuntil(b"\n"), 5)
                if len(header) > MAX_HEADER_BYTES:
                    raise ValueError("PCM header is too long")
                spec = parse_spec(json.loads(header))
            except asyncio.IncompleteReadError:
                return
            except (ValueError, UnicodeError, TimeoutError, asyncio.LimitOverrunError) as exc:
                raise _StreamError("invalid-header", "Invalid PCM header: " + str(exc)) from exc
            previous = None
            async with self._lock:
                previous = self._session
                if previous is not None:
                    # Peer shutdown also frees a backpressured session whose
                    # buffered PCM has not reached reader EOF or adapter drain.
                    if not (previous.reader.at_eof() or previous.state == "draining"
                            or self._peer_closed(previous.writer)):
                        raise _StreamError("busy", "Another PCM writer is connected")
                if self._measurement_active:
                    raise _StreamError("measurement-active", "A measurement session owns the audio path")
                session = _Session(spec, reader, writer, task)
                self._session = session
                self._error = None
            if previous is not None:
                # Retire the dead session before its replacement starts so the
                # two streams never hold the DSP ingress links at the same
                # time.  Its cleanup skips the session identity check and
                # leaves the new session untouched.
                previous.stop_code = "superseded"
                previous.task.cancel()
                await asyncio.gather(previous.task, return_exceptions=True)
            await self._send(writer, "accepted", session_id=session.id)
            self._notify()
            if not await self._wait_selected(session):
                await self._send(writer, "done")
                return
            async with self._lock:
                if not self._selected or self._measurement_active:
                    raise _StreamError("source-changed", "STDIN is no longer selected")
                session.sink = self._deps.sink_factory(spec, session.id)
                await session.sink.start()
                session.routed = True
                session.state = "ready"
            await self._send(writer, "ready")
            self._notify()
            pump = asyncio.create_task(self._pump(session), name="stdin-pcm-pump")
            watch = asyncio.create_task(self._watch(session), name="stdin-link-watch")
            session.children = [pump, watch]
            done, _ = await asyncio.wait(session.children, return_when=asyncio.FIRST_COMPLETED)
            if watch in done:
                await watch
            await pump
            await self._send(writer, "done")
        except asyncio.CancelledError:
            if session:
                try:
                    await self._send(writer, "error", code=session.stop_code,
                                     message=session.stop_code.replace("-", " ").capitalize())
                except (OSError, TimeoutError):
                    pass
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, _StreamError) else "adapter-failed"
            if session and self._session is session:
                self._error = {"code": code, "message": str(exc) or code}
            try:
                await self._send(writer, "error", code=code, message=str(exc)[:1024] or code)
            except (OSError, TimeoutError):
                pass
        finally:
            await run_stop_shielded(self._cleanup(session, writer), cleanup_log="STDIN session cleanup failed")
            self._clients.discard(task)

    async def _pump(self, session: _Session) -> None:
        assembler = FrameAssembler(session.spec.frame_bytes)
        while data := await session.reader.read(CHUNK_BYTES):
            frames = assembler.feed(data)
            if frames:
                was_active = self.snapshot()["state"] == "streaming"
                await session.sink.write(frames)
                session.frames_received += len(frames) // session.spec.frame_bytes
                session.last_frame_at = self._deps.monotonic()
                if not was_active:
                    self._notify()
        try:
            assembler.finish()
        except ValueError as exc:
            raise _StreamError("truncated-frame", str(exc)) from exc
        session.state = "draining"
        self._notify()
        await session.sink.finish()

    async def _watch(self, session: _Session) -> None:
        previous = self.snapshot()["state"]
        while True:
            await asyncio.sleep(0.5)
            if session.state == "draining":
                continue
            try:
                async with asyncio.timeout(2):
                    if not await session.sink.links_present():
                        async with self._lock:
                            session.routed = False
                            self._notify()
                            if self._session is not session or not self._selected or self._measurement_active:
                                raise RuntimeError("STDIN no longer owns the route")
                            await session.sink.repair_links()
                            session.routed = True
                            self._notify()
            except (OSError, RuntimeError, TimeoutError) as exc:
                raise _StreamError("routing-lost", "STDIN links could not be restored") from exc
            current = self.snapshot()["state"]
            if current != previous:
                self._notify()
                previous = current

    async def _cleanup(self, session: _Session | None, writer: asyncio.StreamWriter) -> None:
        try:
            if session:
                for child in session.children:
                    child.cancel()
                await asyncio.gather(*session.children, return_exceptions=True)
                if session.sink:
                    await session.sink.abort()
        finally:
            async with self._lock:
                if session is not None and self._session is session:
                    self._session = None
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 1)
            except (OSError, TimeoutError):
                pass
            self._notify()

    async def _stop_current(self, code: str) -> None:
        async with self._lock:
            session = self._session
            if session:
                session.stop_code = code
                session.routed = False
                session.task.cancel()
        if session:
            await asyncio.gather(session.task, return_exceptions=True)

    async def set_selected(self, selected: bool) -> None:
        async with self._lock:
            self._selected = selected
            if selected:
                self._selection.set()
            else:
                self._selection.clear()
        if not selected:
            await self._stop_current("source-changed")
        self._notify()

    async def set_measurement_active(self, active: bool) -> None:
        async with self._lock:
            self._measurement_active = active
        if active:
            await self._stop_current("measurement-active")
        self._notify()

    async def active_for_power(self) -> bool:
        session = self._session
        if session is None or self.snapshot()["state"] != "streaming":
            return False
        try:
            async with asyncio.timeout(1):
                present = await session.sink.links_present()
            return bool(present and self._session is session and self._selected)
        except (OSError, RuntimeError, TimeoutError):
            return False

    async def stop(self) -> None:
        self._stopped = True
        server, self._server = self._server, None
        if server:
            server.close()
        await self._stop_current("service-stopped")
        clients = list(self._clients)
        for task in clients:
            task.cancel()
        await asyncio.gather(*clients, return_exceptions=True)
        if server:
            await server.wait_closed()
        if self._path and self._inode is not None:
            try:
                if self._path.lstat().st_ino == self._inode:
                    self._path.unlink()
            except FileNotFoundError:
                pass
            self._inode = None
        if self._notify_task:
            self._notify_task.cancel()
            await asyncio.gather(self._notify_task, return_exceptions=True)
            self._notify_task = None
