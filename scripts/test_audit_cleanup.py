#!/usr/bin/env python3
"""Caller cancellation survives cleanup after owned workers finish unwinding."""
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from connection_manager import ConnectionManager, _ClientSender
from measurement.speaker_sweep import JobSlot


class CleanupCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_manager_finishes_transport_and_all_clients_before_cancellation(self):
        for operation in ('disconnect', 'shutdown'):
            with self.subTest(operation=operation):
                manager = ConnectionManager()
                unwinding, release = asyncio.Event(), asyncio.Event()

                class Socket:
                    client_state = SimpleNamespace(name='CONNECTED')
                    close_calls = 0

                    async def accept(self):
                        pass

                    async def close(self):
                        self.close_calls += 1

                async def worker():
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        unwinding.set()
                        await release.wait()

                sockets = [Socket(), Socket()] if operation == 'shutdown' else [Socket()]
                for socket in sockets:
                    await manager.connect(socket)
                original = manager._senders[sockets[0]]._task
                original.cancel()
                await asyncio.gather(original, return_exceptions=True)
                replacement = asyncio.create_task(worker())
                manager._senders[sockets[0]]._task = replacement
                manager._worker_tasks.add(replacement)
                replacement.add_done_callback(manager._worker_tasks.discard)
                await asyncio.sleep(0)
                closing = asyncio.create_task(manager.shutdown() if operation == 'shutdown'
                                              else manager.disconnect(sockets[0]))
                try:
                    await asyncio.wait_for(unwinding.wait(), 1)
                    closing.cancel()
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await closing
                    self.assertEqual(manager.active_connections, [])
                    await manager.drain_workers()
                    self.assertTrue(all(socket.close_calls == 1 for socket in sockets))
                    self.assertTrue(all(task.done() for task in manager._worker_tasks))
                finally:
                    release.set()
                    await asyncio.gather(closing, return_exceptions=True)
                    await manager.shutdown()

    async def test_caller_cancellation_is_propagated_after_worker_drain(self):
        for kind in ("socket", "measurement"):
            with self.subTest(kind=kind):
                unwinding, release = asyncio.Event(), asyncio.Event()

                async def worker():
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        unwinding.set()
                        await release.wait()

                task = asyncio.create_task(worker())
                await asyncio.sleep(0)
                if kind == "socket":
                    owner = _ClientSender(None, timeout=1, max_pending=4)
                    owner._task = task
                    owner.enqueue("pending", coalesce_key="status")
                    close = owner.close
                else:
                    owner = JobSlot("Test")
                    owner.task = task
                    owner.job = {"status": "running", "cancel_requested": False}
                    close = owner.shutdown
                closing = asyncio.create_task(close())
                try:
                    await asyncio.wait_for(unwinding.wait(), 1)
                    closing.cancel()
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                    self.assertFalse(closing.done(), "Cleanup returned before worker drain")
                    closing.cancel()
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await closing
                    self.assertTrue(task.done())
                    if kind == "socket":
                        self.assertTrue(owner.queue.empty())
                        self.assertEqual(owner._coalesced, {})
                finally:
                    release.set()
                    await asyncio.gather(task, closing, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
