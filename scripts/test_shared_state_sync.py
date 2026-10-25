#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Successful output edits invalidate the shared state on every client."""
import asyncio
import json
import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from connection_manager import ConnectionManager
from fastapi import HTTPException


class Socket:
    client_state = SimpleNamespace(name="CONNECTED")

    def __init__(self):
        self.frames = []

    async def accept(self):
        pass

    async def send_text(self, data):
        self.frames.append(json.loads(data))

    async def close(self):
        pass


class SharedStateSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_committed_output_edit_reaches_two_clients(self):
        for kind in ("set_processing", "switch_mode"):
            with self.subTest(kind=kind):
                await self.check_mutation(kind)

    async def check_mutation(self, kind):
        manager = ConnectionManager()
        self.addAsyncCleanup(manager.shutdown)
        clients = [Socket(), Socket()]
        for client in clients:
            await manager.connect(client)
            manager.mark_ready(client)
        body = {"expected_revision": 7, "mutation": {"kind": kind, "mode": "surround"}}
        request = SimpleNamespace(json=AsyncMock(return_value=body))
        result = {"status": "ok", "revision": 8, "live_applied": True}
        with patch.object(main, "manager", manager), patch.object(
            main.output_application, "apply", AsyncMock(return_value=result)
        ), patch.object(main, "_hdmi_transition_scope", nullcontext), patch.object(
            main, "_source_transition_lock", asyncio.Lock
        ), patch.object(main, "_wait_for_hdmi_transport_change", AsyncMock(return_value=True)):
            self.assertEqual(await main.apply_audio_output_state(request), result)
            for _ in range(20):
                if all(client.frames for client in clients):
                    break
                await asyncio.sleep(0)
        for client in clients:
            self.assertEqual(client.frames, [{"type": "output_state", "data": {"revision": 8}}])

    async def test_rejected_edit_does_not_publish_a_commit(self):
        manager = ConnectionManager()
        self.addAsyncCleanup(manager.shutdown)
        client = Socket()
        await manager.connect(client)
        manager.mark_ready(client)
        request = SimpleNamespace(json=AsyncMock(return_value={"mutation": {"kind": "set_processing"}}))
        with patch.object(main, "manager", manager), patch.object(
            main.output_application, "apply", AsyncMock(side_effect=HTTPException(409, "conflict"))
        ):
            with self.assertRaises(HTTPException):
                await main.apply_audio_output_state(request)
        self.assertEqual(client.frames, [])

    async def test_cancelled_mode_request_publishes_its_successful_commit(self):
        started = asyncio.Event()
        committed = asyncio.Event()
        manager = ConnectionManager()
        self.addAsyncCleanup(manager.shutdown)
        client = Socket()
        await manager.connect(client)
        manager.mark_ready(client)

        async def apply(_body):
            started.set()
            await committed.wait()
            return {"status": "ok", "revision": 9}

        request = SimpleNamespace(json=AsyncMock(return_value={"mutation": {"kind": "switch_mode"}}))
        with patch.object(main, "manager", manager), patch.object(main.output_application, "apply", apply), patch.object(
            main, "_hdmi_transition_scope", nullcontext
        ), patch.object(main, "_source_transition_lock", asyncio.Lock), patch.object(
            main, "_wait_for_hdmi_transport_change", AsyncMock(return_value=True)
        ):
            task = asyncio.create_task(main.apply_audio_output_state(request))
            await started.wait()
            task.cancel()
            committed.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            for _ in range(20):
                if client.frames:
                    break
                await asyncio.sleep(0)
        self.assertEqual(client.frames, [{"type": "output_state", "data": {"revision": 9}}])


if __name__ == "__main__":
    unittest.main()
