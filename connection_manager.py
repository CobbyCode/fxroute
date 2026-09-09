# SPDX-License-Identifier: AGPL-3.0-only

"""WebSocket fan-out: bounded per-client delivery without head-of-line blocking.

Extracted verbatim from main.py (REFACTOR-013). Behavior is identical to
the previous inline implementation. This module owns the complete
client-delivery lifecycle: the bounded per-client queue with exactly one
send worker FIFO per socket (``_ClientSender``), send timeouts, coalescing
of repeated state snapshots to one pending slot per type, removal of slow
or failed clients, and the owned bounded background tasks for transport
close and failure cleanup (``ConnectionManager``).

It owns no application state: no playback, DSP, measurement or library
access. The only shared dependency is the ``WebSocket`` transport type and
the standard library. main.py keeps the single ``manager`` singleton
(application wiring, like ``runtime``) and every existing
``manager.broadcast(...)`` call site is unchanged.

Logging note: records are now emitted on the ``connection_manager``
channel instead of ``main`` (standard per-module logger); messages and
levels are unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import List

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class _ClientSender:
    """Bounded per-client delivery queue with exactly one send worker.

    One worker per client serializes all sends (FIFO by construction) and
    every send is bounded by a timeout.  When the bounded queue is full the
    caller treats the client as too slow; the worker failing (timeout, send
    error, vanished socket) marks the client for removal.
    """

    def __init__(
        self,
        websocket: WebSocket,
        *,
        timeout: float,
        max_pending: int,
    ) -> None:
        self.websocket = websocket
        self.timeout = timeout
        self.queue: "asyncio.Queue[tuple[str | None, str | None]]" = asyncio.Queue(maxsize=max_pending)
        self._coalesced: dict[str, str] = {}
        self.ready = False
        self.failed = False
        self.failure_reason = "send-worker-failed"
        self._task: "asyncio.Task | None" = None

    def enqueue(self, data: str, *, coalesce_key: str | None = None) -> bool:
        """Queue one payload; False when the bounded queue is full."""
        if coalesce_key is not None and coalesce_key in self._coalesced:
            self._coalesced[coalesce_key] = data
            return True
        try:
            self.queue.put_nowait((coalesce_key, None if coalesce_key else data))
        except asyncio.QueueFull:
            return False
        if coalesce_key is not None:
            self._coalesced[coalesce_key] = data
        return True

    async def close(self) -> None:
        """Cancel the worker and drain any queued payloads."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self.queue.task_done()
        self._coalesced.clear()

    async def run(self) -> None:
        while True:
            coalesce_key, data = await self.queue.get()
            try:
                if coalesce_key is not None:
                    data = self._coalesced.pop(coalesce_key)
                if data is None:
                    continue
                if self.websocket.client_state.name != "CONNECTED":
                    raise RuntimeError("websocket is no longer CONNECTED")
                await asyncio.wait_for(
                    self.websocket.send_text(data), timeout=self.timeout
                )
            except asyncio.TimeoutError:
                self.failed = True
                self.failure_reason = f"send-timeout:{self.timeout:.1f}s"
                return
            except Exception as exc:
                self.failed = True
                self.failure_reason = f"send-error:{exc}"
                return
            finally:
                self.queue.task_done()


class ConnectionManager:
    """Fan-out broadcasts without letting one slow client stall the others.

    Every connected client owns one bounded delivery queue and exactly one
    send worker; all sends for a socket (broadcasts, init, pong) go through
    that worker, so per-client ordering is FIFO and concurrent ``send_text``
    calls are impossible.  A stuck client only delays its own queue; a
    timeout or send error removes the client. Repeated state snapshots share
    one pending slot per type; a full queue of distinct events disconnects the
    overloaded client instead of silently dropping events.
    """

    def __init__(self, send_timeout: float = 5.0, max_pending_sends: int = 8):
        self.active_connections: List[WebSocket] = []
        self._list_lock = asyncio.Lock()
        self._senders: dict[WebSocket, _ClientSender] = {}
        self._worker_tasks: set[asyncio.Task] = set()
        self._send_timeout = max(0.05, send_timeout)
        self._max_pending_sends = max(1, max_pending_sends)
        self._coalesced_message_types = {
            "playback",
            "spotify",
            "dsp",
            "playback_peak_warning",
        }

    @property
    def is_idle(self) -> bool:
        """True when no clients are connected and no owned tasks are pending."""
        return not self.active_connections and not self._worker_tasks

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        sender = _ClientSender(
            websocket,
            timeout=self._send_timeout,
            max_pending=self._max_pending_sends,
        )
        async with self._list_lock:
            self.active_connections.append(websocket)
            self._senders[websocket] = sender
        self._spawn_worker(websocket, sender)
        logger.info(f"WebSocket connected: {len(self.active_connections)} active")

    async def _unregister(self, websocket: WebSocket) -> _ClientSender | None:
        async with self._list_lock:
            sender = self._senders.pop(websocket, None)
            if sender is not None:
                try:
                    self.active_connections.remove(websocket)
                except ValueError:
                    pass
        return sender

    async def disconnect(self, websocket: WebSocket, *, reason: str = "unspecified") -> bool:
        """Remove a client from the manager (idempotent).

        The WebSocket transport close runs in an owned, bounded background
        task so a slow or stuck close can never block producers or the
        broadcast hotpath.  Normal peer disconnects are unaffected: the close
        on an already-closed transport is a no-op.
        """
        sender = await self._unregister(websocket)
        if sender is None:
            return False
        await sender.close()
        self._schedule_transport_close(websocket)
        logger.info(
            "WebSocket disconnected: reason=%s active=%s",
            reason,
            len(self.active_connections),
        )
        return True

    def _schedule_transport_close(self, websocket: WebSocket) -> None:
        """Close the transport in a bounded owned task, never awaited inline."""

        async def close_transport() -> None:
            try:
                await asyncio.wait_for(
                    websocket.close(), timeout=self._send_timeout
                )
            except asyncio.TimeoutError:
                logger.debug(
                    f"WebSocket close timed out after {self._send_timeout:.1f}s"
                )
            except Exception as exc:
                logger.debug(f"WebSocket close failed: {exc}")

        task = asyncio.create_task(close_transport(), name="ws-client-close")
        self._worker_tasks.add(task)
        task.add_done_callback(self._worker_tasks.discard)

    async def send_to_client(
        self,
        websocket: WebSocket,
        data: str,
        *,
        coalesce_key: str | None = None,
    ) -> bool:
        """Queue one payload for a client's send worker.

        Returns False when the client is gone, its worker already failed, or
        its bounded queue is full; the full-queue case disconnects the client
        as too slow.
        """
        sender = self._senders.get(websocket)
        if sender is None or sender.failed:
            return False
        if not sender.enqueue(data, coalesce_key=coalesce_key):
            await self.disconnect(websocket, reason="send-queue-full")
            return False
        return True

    def mark_ready(self, websocket: WebSocket) -> bool:
        """Unlock normal broadcasts for a client whose init is queued first."""
        sender = self._senders.get(websocket)
        if sender is None:
            return False
        sender.ready = True
        return True

    def pending_count(self, websocket: WebSocket) -> int | None:
        """Queued payload count for one client; None when the client is unknown."""
        sender = self._senders.get(websocket)
        if sender is None:
            return None
        return sender.queue.qsize()

    def _spawn_worker(self, websocket: WebSocket, sender: _ClientSender) -> None:
        task = asyncio.create_task(sender.run(), name="ws-client-sender")
        sender._task = task
        self._worker_tasks.add(task)

        def finished(_task: asyncio.Task) -> None:
            self._worker_tasks.discard(_task)
            if _task.cancelled() or not sender.failed:
                return
            cleanup = asyncio.create_task(
                self.disconnect(
                    websocket,
                    reason=getattr(sender, "failure_reason", "send-worker-failed"),
                ),
                name="ws-client-failure-cleanup",
            )
            self._worker_tasks.add(cleanup)
            cleanup.add_done_callback(self._worker_tasks.discard)

        task.add_done_callback(finished)

    async def broadcast(self, message: dict) -> None:
        data = json.dumps(message)
        message_type = message.get("type")
        coalesce_key = (
            str(message_type)
            if message_type in self._coalesced_message_types
            else None
        )
        async with self._list_lock:
            connections = list(self.active_connections)
        for connection in connections:
            if connection.client_state.name != "CONNECTED":
                await self.disconnect(connection, reason="transport-not-connected")
                continue
            sender = self._senders.get(connection)
            if sender is None or not sender.ready:
                continue
            await self.send_to_client(
                connection,
                data,
                coalesce_key=coalesce_key,
            )

    async def drain_workers(self) -> None:
        """Wait until all owned background tasks finished (single pass, idempotent)."""
        pending = [task for task in set(self._worker_tasks) if not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def shutdown(self, *, reason: str = "server-shutdown") -> None:
        """Disconnect every client, then drain all owned worker/cleanup tasks.

        One failing client never aborts the shutdown of the others; per-client
        failures are only logged. Safe to call when already idle.
        """
        async with self._list_lock:
            connections = list(self.active_connections)
        for connection in connections:
            try:
                await self.disconnect(connection, reason=reason)
            except Exception:
                logger.exception("Failed to disconnect websocket client during shutdown")
        await self.drain_workers()
