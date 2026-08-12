#!/usr/bin/env python3
"""WebSocket backpressure: bounded per-client queue and single send worker."""

from __future__ import annotations

import asyncio
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from main import ConnectionManager


class _FakeWebSocket:
    def __init__(self, *, hang: bool = False, fail: bool = False, delay: float = 0.0,
                 hang_close: bool = False, fail_close: bool = False):
        self.client_state = SimpleNamespace(name="CONNECTED")
        self.sent: list[str] = []
        self.hang = hang
        self.fail = fail
        self.delay = delay
        self.hang_close = hang_close
        self.fail_close = fail_close
        self.accepted = False
        self.active_sends = 0
        self.max_concurrent_sends = 0
        self.close_calls = 0
        self._hang_event = asyncio.Event()

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, data: str) -> None:
        self.active_sends += 1
        self.max_concurrent_sends = max(self.max_concurrent_sends, self.active_sends)
        try:
            if self.hang:
                await self._hang_event.wait()
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.fail:
                raise RuntimeError("socket broken")
            self.sent.append(data)
        finally:
            self.active_sends -= 1

    async def close(self) -> None:
        self.close_calls += 1
        if self.hang_close:
            await self._hang_event.wait()
        if self.fail_close:
            raise RuntimeError("close failed")

    def release(self) -> None:
        self._hang_event.set()


async def _wait_until(condition, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.005)
    return False


async def _connect(manager: ConnectionManager, websocket: _FakeWebSocket) -> None:
    await manager.connect(websocket)
    manager.mark_ready(websocket)


class ConnectionManagerBackpressureTests(unittest.IsolatedAsyncioTestCase):
    async def test_fast_client_not_blocked_by_hanging_client(self):
        manager = ConnectionManager()
        fast = _FakeWebSocket()
        hanging = _FakeWebSocket(hang=True)
        await _connect(manager, fast)
        await _connect(manager, hanging)

        started = time.monotonic()
        await manager.broadcast({"type": "test", "n": 1})
        broadcast_elapsed = time.monotonic() - started

        delivered = await _wait_until(lambda: len(fast.sent) == 1)
        self.assertTrue(delivered, "fast client must receive the broadcast")
        self.assertLess(broadcast_elapsed, 1.0, "broadcast must not wait for a stuck client")
        self.assertIn(hanging, manager.active_connections, "hanging client is still within its send window")

    async def test_hanging_client_times_out_and_is_removed(self):
        manager = ConnectionManager(send_timeout=0.05)
        hanging = _FakeWebSocket(hang=True)
        await _connect(manager, hanging)

        await manager.broadcast({"type": "test"})
        removed = await _wait_until(lambda: hanging not in manager.active_connections, timeout=3.0)
        self.assertTrue(removed, "timed-out client must be removed from the connection list")
        self.assertEqual(hanging.sent, [], "nothing was delivered to the stuck client")
        closed = await _wait_until(lambda: hanging.close_calls == 1)
        self.assertTrue(closed, "a server-dropped client must have its transport closed")
        await _wait_until(lambda: not manager._worker_tasks)
        self.assertEqual(manager._worker_tasks, set(), "no unowned worker/cleanup tasks may remain")

    async def test_send_exception_removes_client(self):
        manager = ConnectionManager()
        broken = _FakeWebSocket(fail=True)
        healthy = _FakeWebSocket()
        await _connect(manager, broken)
        await _connect(manager, healthy)

        await manager.broadcast({"type": "test"})
        removed = await _wait_until(lambda: broken not in manager.active_connections)
        self.assertTrue(removed, "client with a failing send must be removed")
        closed = await _wait_until(lambda: broken.close_calls == 1)
        self.assertTrue(closed, "send-exception client must have its transport closed")
        delivered = await _wait_until(lambda: len(healthy.sent) == 1)
        self.assertTrue(delivered, "healthy client is unaffected by the broken one")

    async def test_single_worker_never_sends_concurrently_on_one_socket(self):
        manager = ConnectionManager()
        slow = _FakeWebSocket(delay=0.02)
        await _connect(manager, slow)

        await asyncio.gather(*(
            manager.broadcast({"type": "overlap", "n": index})
            for index in range(4)
        ))
        drained = await _wait_until(lambda: len(slow.sent) == 4)
        self.assertTrue(drained, "all overlapping deliveries must complete")
        self.assertEqual(slow.max_concurrent_sends, 1, "one socket must never have parallel send_text")

    async def test_healthy_slow_client_keeps_order_and_stays_connected(self):
        manager = ConnectionManager(send_timeout=1.0)
        slow = _FakeWebSocket(delay=0.01)
        await _connect(manager, slow)

        for index in range(5):
            await manager.broadcast({"type": "ordered", "n": index})
            received = await _wait_until(lambda: len(slow.sent) == index + 1)
            self.assertTrue(received, f"message {index} must be delivered")

        self.assertEqual(
            [json.loads(raw)["n"] for raw in slow.sent],
            [0, 1, 2, 3, 4],
            "per-client FIFO ordering must be preserved",
        )
        self.assertIn(slow, manager.active_connections, "healthy slow client must not be dropped")

    async def test_non_connected_client_is_removed(self):
        manager = ConnectionManager()
        stale = _FakeWebSocket()
        stale.client_state.name = "DISCONNECTED"
        await _connect(manager, stale)

        await manager.broadcast({"type": "test"})
        removed = await _wait_until(lambda: stale not in manager.active_connections)
        self.assertTrue(removed, "client no longer CONNECTED must be pruned")
        self.assertEqual(stale.sent, [])

    async def test_init_is_always_first_and_never_parallel_to_broadcast(self):
        manager = ConnectionManager()
        slow = _FakeWebSocket(delay=0.05)
        await manager.connect(slow)
        init = json.dumps({"type": "init", "data": {"n": 0}})
        await manager.send_to_client(slow, init)
        manager.mark_ready(slow)

        broadcast_task = asyncio.create_task(manager.broadcast({"type": "update", "n": 1}))
        received = await _wait_until(lambda: len(slow.sent) == 2, timeout=3.0)
        await broadcast_task
        self.assertTrue(received, "init and broadcast must both be delivered")
        self.assertEqual(
            [json.loads(raw)["type"] for raw in slow.sent],
            ["init", "update"],
            "init must be delivered before any broadcast",
        )
        self.assertEqual(slow.max_concurrent_sends, 1, "init and broadcast must share the single worker")

    async def test_pong_shares_worker_and_keeps_deterministic_order(self):
        manager = ConnectionManager()
        slow = _FakeWebSocket(delay=0.03)
        await _connect(manager, slow)

        await manager.send_to_client(slow, json.dumps({"type": "pong"}))
        await manager.broadcast({"type": "update", "n": 1})
        received = await _wait_until(lambda: len(slow.sent) == 2)
        self.assertTrue(received, "pong and broadcast must both be delivered")
        self.assertEqual(
            [json.loads(raw)["type"] for raw in slow.sent],
            ["pong", "update"],
        )
        self.assertEqual(slow.max_concurrent_sends, 1)

    async def test_full_queue_disconnects_client_instead_of_silent_drop(self):
        manager = ConnectionManager(max_pending_sends=1)
        hanging = _FakeWebSocket(hang=True)
        await _connect(manager, hanging)

        await manager.broadcast({"type": "first"})
        in_send = await _wait_until(lambda: hanging.active_sends == 1)
        self.assertTrue(in_send, "worker is blocked inside the hanging send")
        sender = manager._senders[hanging]
        self.assertEqual(sender.queue.qsize(), 0)

        # The bounded queue (capacity 1) is now filled with a held message.
        self.assertTrue(sender.enqueue(json.dumps({"type": "held"})))
        self.assertEqual(sender.queue.qsize(), 1)

        with self.assertLogs("main", level="INFO") as captured:
            await manager.broadcast({"type": "second"})
        removed = await _wait_until(lambda: hanging not in manager.active_connections, timeout=3.0)
        self.assertTrue(
            removed,
            "a client whose bounded queue is full must be disconnected, not silently skipped",
        )
        closed = await _wait_until(lambda: hanging.close_calls == 1)
        self.assertTrue(closed, "an overloaded client must have its transport closed")
        self.assertEqual(hanging.sent, [], "stuck client never received a message")
        self.assertTrue(any("reason=send-queue-full" in line for line in captured.output))

    async def test_repeated_state_snapshots_coalesce_to_latest_pending_value(self):
        manager = ConnectionManager(max_pending_sends=1, send_timeout=1.0)
        slow = _FakeWebSocket(hang=True)
        await _connect(manager, slow)

        await manager.broadcast({"type": "playback", "n": 1})
        in_send = await _wait_until(lambda: slow.active_sends == 1)
        self.assertTrue(in_send, "first snapshot must enter the send worker")

        for index in range(2, 20):
            await manager.broadcast({"type": "playback", "n": index})

        self.assertIn(slow, manager.active_connections)
        self.assertEqual(manager._senders[slow].queue.qsize(), 1)
        slow.hang = False
        slow.release()
        delivered = await _wait_until(lambda: len(slow.sent) == 2)
        self.assertTrue(delivered)
        self.assertEqual(
            [json.loads(raw)["n"] for raw in slow.sent],
            [1, 19],
            "only the newest pending snapshot should follow the in-flight one",
        )

    async def test_distinct_events_still_disconnect_when_queue_is_full(self):
        manager = ConnectionManager(max_pending_sends=1, send_timeout=1.0)
        hanging = _FakeWebSocket(hang=True)
        await _connect(manager, hanging)

        await manager.broadcast({"type": "event-a"})
        self.assertTrue(await _wait_until(lambda: hanging.active_sends == 1))
        await manager.broadcast({"type": "event-b"})
        await manager.broadcast({"type": "event-c"})

        removed = await _wait_until(lambda: hanging not in manager.active_connections)
        self.assertTrue(removed)


    async def test_peer_disconnect_is_idempotent(self):
        manager = ConnectionManager()
        peer = _FakeWebSocket()
        await _connect(manager, peer)

        self.assertTrue(await manager.disconnect(peer))
        self.assertFalse(await manager.disconnect(peer), "second disconnect is a no-op")
        self.assertNotIn(peer, manager.active_connections)
        closed = await _wait_until(lambda: peer.close_calls == 1)
        self.assertTrue(closed, "the transport close runs exactly once")
        await _wait_until(lambda: not manager._worker_tasks)
        self.assertEqual(manager._worker_tasks, set())

    async def test_hanging_close_does_not_block_healthy_client(self):
        manager = ConnectionManager(send_timeout=0.3)
        bad = _FakeWebSocket(hang_close=True)
        healthy = _FakeWebSocket()
        await _connect(manager, bad)
        await _connect(manager, healthy)

        started = time.monotonic()
        await manager.disconnect(bad)
        drop_elapsed = time.monotonic() - started

        self.assertLess(drop_elapsed, 0.2, "disconnect must not wait for the stuck close")
        await manager.broadcast({"type": "test", "n": 1})
        delivered = await _wait_until(lambda: len(healthy.sent) == 1)
        self.assertTrue(delivered, "healthy client must be served while the close hangs")
        self.assertEqual(bad.close_calls, 1, "the close was started once")


if __name__ == "__main__":
    unittest.main()
