#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Runtime-level role-derived output mute masks (area measurement masking)."""

import asyncio
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dsp.runtime import DSPRuntime
from measurement.job_runner import MeasurementJobRunner


class OutputMaskRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.commands = []

    async def _runtime(self, outputs=8):
        runtime = DSPRuntime(None)
        if outputs is not None:
            runtime._config = SimpleNamespace(layout=[{"name": f"way_{index}"} for index in range(outputs)])

        async def control(command, *, reply):
            self.commands.append(command)
            return "ok\n"

        runtime._control = control
        return runtime

    async def test_area_mask_sets_and_clears_exactly_its_own_bits(self):
        runtime = await self._runtime()
        previous = await runtime.apply_output_mask(0b00001101)
        self.assertEqual(previous, 0)
        self.assertEqual(runtime._output_mask, 0b00001101)
        await runtime.clear_output_mask(0b00001101)
        self.assertEqual(runtime._output_mask, 0)
        self.assertEqual(self.commands, ["mute 13 1", "mute 13 0"])

    async def test_unrelated_mute_bits_survive_an_area_measurement(self):
        runtime = await self._runtime()
        # A legacy exact-sub mute owns other bits; masking only writes its own.
        runtime._output_mask = 0b1
        await runtime.apply_output_mask(0b1100)
        self.assertEqual(runtime._output_mask, 0b1101)
        await runtime.clear_output_mask(0b1100)
        self.assertEqual(runtime._output_mask, 0b1)

    async def test_mask_validation_rejects_zero_and_unexposed_outputs(self):
        runtime = await self._runtime(outputs=4)
        for mask in (0, -1, True, 1 << 32):
            with self.subTest(mask=mask), self.assertRaisesRegex(ValueError, "32-bit"):
                await runtime.apply_output_mask(mask)
        with self.assertRaisesRegex(ValueError, "does not expose"):
            await runtime.apply_output_mask(0b10000)
        with self.assertRaisesRegex(ValueError, "does not expose"):
            await runtime.clear_output_mask(0b10000)
        self.assertEqual(self.commands, [])
        self.assertEqual(runtime._output_mask, 0)

    async def test_mask_without_a_loaded_config_still_validates_the_value(self):
        runtime = await self._runtime(outputs=None)
        await runtime.apply_output_mask(1)
        self.assertEqual(self.commands, ["mute 1 1"])
        with self.assertRaisesRegex(ValueError, "32-bit"):
            await runtime.apply_output_mask(0)

    async def test_stop_resets_the_tracked_mask(self):
        runtime = await self._runtime()
        await runtime.apply_output_mask(0b11)
        await runtime.stop()
        self.assertEqual(runtime._output_mask, 0)

    async def test_poisoned_control_rejects_mask_and_exact_mute_noops(self):
        runtime = await self._runtime()
        runtime._output_mask = 1
        runtime._exact_sub_mute = True
        runtime._exact_sub_mute_mask = 1
        runtime._control_poisoned = True
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            await runtime.clear_output_mask(1)
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            await runtime.set_exact_sub_mute(False, mask=1)
        self.assertEqual(runtime._output_mask, 1)
        self.assertTrue(runtime._exact_sub_mute)



class DatagramEngine:
    """Control-socket engine double: applies a mute before it acknowledges."""

    def __init__(self, sock):
        self.sock = sock
        self.mask = 0
        self.received = []
        self.gates = {}
        self.holding = asyncio.Event()

    def hold(self, command):
        gate = asyncio.Event()
        self.gates[command] = gate
        return gate

    async def serve(self):
        loop = asyncio.get_running_loop()
        while True:
            data, address = await loop.sock_recvfrom(self.sock, 256)
            command = data.decode()
            self.received.append(command)
            tokens = command.split()
            reply = b"0\n" if tokens[:3] == ["gain", "db", "get"] else b"ok\n"
            if tokens[0] == "mute":
                bits = int(tokens[1])
                self.mask = self.mask | bits if tokens[2] == "1" else self.mask & ~bits
            gate = self.gates.pop(command, None)
            if gate is not None:
                self.holding.set()
                await gate.wait()
            self.sock.sendto(reply, address)


class CancelledMaskAcknowledgementTests(unittest.IsolatedAsyncioTestCase):
    """A cancel between the engine's mute and its acknowledgement stays consistent."""

    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory(prefix="mask-ack-")
        self.addCleanup(directory.cleanup)
        engine_sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        client_sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(engine_sock.close)
        self.addCleanup(client_sock.close)
        for sock in (engine_sock, client_sock):
            sock.setblocking(False)
        engine_path = Path(directory.name) / "engine"
        engine_sock.bind(str(engine_path))
        client_sock.bind(str(Path(directory.name) / "client"))
        self.engine = DatagramEngine(engine_sock)
        self.server = asyncio.create_task(self.engine.serve())
        self.runtime = DSPRuntime(None)
        self.runtime._control_socket = client_sock
        self.runtime._control_path = engine_path
        self.job = {"id": "side-take", "status": "queued", "output_mask": 0b1100,
                    "measurement_scope": "active_chain"}
        self.executed = []
        self.runner = MeasurementJobRunner(
            get_job=lambda _: self.job, persist_job=lambda _: None,
            public_result=lambda result: result, cleanup_job=lambda _: None,
            retain_history=lambda: None, utc_now=lambda: "now",
            is_terminal=lambda status: status in ("completed", "failed", "cancelled"),
            active_scope_enter=self.runtime.enter_active_measurement,
            active_scope_exit=self.runtime.exit_active_measurement,
            output_mask_apply=self.runtime.apply_output_mask,
            output_mask_clear=self.runtime.clear_output_mask)

    async def asyncTearDown(self):
        self.server.cancel()
        await asyncio.gather(self.server, return_exceptions=True)

    async def _cancel_while_held(self, command):
        gate = self.engine.hold(command)
        worker = self.runner.start(self.job["id"], self.job, lambda _: self.executed.append(True) or {})
        async with asyncio.timeout(3):
            await self.engine.holding.wait()
        worker.cancel()
        await asyncio.sleep(0.01)
        gate.set()
        async with asyncio.timeout(3):
            await asyncio.gather(worker, return_exceptions=True)
        return worker

    async def _assert_consistent(self):
        self.assertEqual(self.engine.mask, 0)
        self.assertEqual(self.runtime._output_mask, 0)
        self.assertFalse(self.runtime._measurement_scope_lock.locked())
        # Every acknowledgement was consumed by its own command: the next
        # exchange reads its own reply, not a stale "ok".
        self.assertEqual(await self.runtime.read_output_gain_db(), 0.0)

    async def test_cancel_before_the_mute_acknowledgement_unmutes_and_releases(self):
        await self._cancel_while_held("mute 12 1")
        self.assertEqual(self.job["status"], "cancelled")
        self.assertEqual(self.executed, [])
        self.assertEqual(self.engine.received[:2], ["mute 12 1", "mute 12 0"])
        await self._assert_consistent()

    async def test_cancel_before_the_unmute_acknowledgement_still_leaves_the_scope(self):
        worker = await self._cancel_while_held("mute 12 0")
        self.assertTrue(worker.cancelled())
        self.assertEqual(self.job["status"], "completed")
        self.assertEqual(self.executed, [True])
        await self._assert_consistent()
        async with asyncio.timeout(1):
            await self.runtime.enter_active_measurement()
        await self.runtime.exit_active_measurement(False)


class TimedOutControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_commit_error_poisoned_even_with_an_explicit_reply(self):
        with tempfile.TemporaryDirectory(prefix="commit-error-") as directory:
            engine = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self.addCleanup(engine.close)
            self.addCleanup(client.close)
            engine.setblocking(False)
            client.setblocking(False)
            path = Path(directory) / "engine"
            engine.bind(str(path))
            client.bind(str(Path(directory) / "client"))
            runtime = DSPRuntime(None)
            runtime._control_socket = client
            runtime._control_path = path

            async def answer():
                data, address = await asyncio.get_running_loop().sock_recvfrom(engine, 256)
                self.assertEqual(data, b"live commit")
                engine.sendto(b"error live commit timeout\n", address)

            task = asyncio.create_task(answer())
            try:
                with self.assertRaisesRegex(RuntimeError, "live commit timeout"):
                    await runtime._control("live commit", reply=True)
                await task
                self.assertEqual(runtime.snapshot()["control_state"], "unknown")
                with self.assertRaisesRegex(RuntimeError, "unknown"):
                    await runtime.read_output_gain_db()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_unexpected_mute_reply_never_confirms_mask(self):
        with tempfile.TemporaryDirectory(prefix="bad-ack-") as directory:
            engine = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self.addCleanup(engine.close)
            self.addCleanup(client.close)
            engine.setblocking(False)
            client.setblocking(False)
            path = Path(directory) / "engine"
            engine.bind(str(path))
            client.bind(str(Path(directory) / "client"))
            runtime = DSPRuntime(None)
            runtime._control_socket = client
            runtime._control_path = path

            async def answer():
                data, address = await asyncio.get_running_loop().sock_recvfrom(engine, 256)
                self.assertEqual(data, b"mute 1 1")
                engine.sendto(b"0\n", address)

            task = asyncio.create_task(answer())
            try:
                with self.assertRaisesRegex(RuntimeError, "reply"):
                    await runtime.apply_output_mask(1)
                await task
                self.assertEqual(runtime._output_mask, 0)
                self.assertIsNone(runtime.snapshot()["output_mask"])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_late_mute_ack_cannot_answer_next_command_or_confirm_mask(self):
        with tempfile.TemporaryDirectory(prefix="late-ack-") as directory:
            engine = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self.addCleanup(engine.close)
            self.addCleanup(client.close)
            engine.setblocking(False)
            client.setblocking(False)
            engine_path = Path(directory) / "engine"
            engine.bind(str(engine_path))
            client.bind(str(Path(directory) / "client"))
            runtime = DSPRuntime(None)
            runtime._control_socket = client
            runtime._control_path = engine_path
            runtime._config = SimpleNamespace(layout=[{"name": "L"}])
            received = asyncio.Event()

            async def late_engine():
                loop = asyncio.get_running_loop()
                data, address = await loop.sock_recvfrom(engine, 256)
                self.assertEqual(data, b"mute 1 1")
                received.set()
                await asyncio.sleep(0.09)
                try:
                    engine.sendto(b"ok\n", address)
                except OSError:
                    pass

            task = asyncio.create_task(late_engine())
            try:
                with patch("dsp.runtime.CONTROL_TIMEOUT_SECONDS", 0.03, create=True):
                    with self.assertRaises(asyncio.TimeoutError):
                        await runtime.apply_output_mask(1)
                await received.wait()
                await task
                runtime._config = None
                self.assertIsNone(runtime.snapshot()["output_mask"])
                with self.assertRaisesRegex(RuntimeError, "unknown|unavailable|rebuild"):
                    await runtime.read_output_gain_db()
                self.assertEqual(runtime._output_mask, 0)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_unmute_timeout_does_not_report_stale_mask_as_engine_state(self):
        with tempfile.TemporaryDirectory(prefix="unmute-timeout-") as directory:
            engine = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self.addCleanup(engine.close)
            self.addCleanup(client.close)
            engine.setblocking(False)
            client.setblocking(False)
            path = Path(directory) / "engine"
            engine.bind(str(path))
            client.bind(str(Path(directory) / "client"))
            server = DatagramEngine(engine)
            task = asyncio.create_task(server.serve())
            runtime = DSPRuntime(None)
            runtime._control_socket = client
            runtime._control_path = path
            try:
                await runtime.apply_output_mask(1)
                gate = server.hold("mute 1 0")
                with patch("dsp.runtime.CONTROL_TIMEOUT_SECONDS", 0.03):
                    with self.assertRaises(asyncio.TimeoutError):
                        await runtime.clear_output_mask(1)
                gate.set()
                await asyncio.sleep(0)
                self.assertEqual(server.mask, 0)
                self.assertEqual(runtime._output_mask, 1)
                self.assertIsNone(runtime.snapshot()["output_mask"])
                with self.assertRaisesRegex(RuntimeError, "unknown"):
                    await runtime.apply_output_mask(1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
