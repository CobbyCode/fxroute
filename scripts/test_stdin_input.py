#!/usr/bin/env python3
"""Real Unix-socket sessions with a controlled PCM sink."""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.stdin_input import StdinInputDependencies, StdinInputService


class MemorySink:
    def __init__(self, spec, session_id):
        self.spec = spec
        self.frames = bytearray()
        self.finished = self.aborted = False
        self.linked = True
        self.repair_error = False
        self.allow_write = asyncio.Event()
        self.allow_write.set()
        # Held open by the supersede tests to keep a crashed session inside
        # its drain window while the replacement connects.
        self.finish_gate = asyncio.Event()
        self.finish_gate.set()

    async def start(self):
        return None

    async def write(self, frames):
        await self.allow_write.wait()
        self.frames.extend(frames)

    async def finish(self):
        await self.finish_gate.wait()
        self.finished = True

    async def abort(self):
        self.aborted = True
        self.allow_write.set()

    async def links_present(self):
        return self.linked and not self.aborted

    async def repair_links(self):
        if self.repair_error:
            raise RuntimeError("Missing DSP ingress")
        self.linked = True


async def ignore_change():
    return None


class ShutdownNotifyTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_notify_after_stop_is_dropped(self):
        """A notify after stop() must not spawn a task that outlives shutdown.

        The measurement slot is released after the STDIN service stops, so
        set_measurement_active(False)/set_selected(False) run post-stop.
        """
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "shutdown.sock"
        calls = []

        async def record_change():
            calls.append(1)

        service = StdinInputService(StdinInputDependencies(
            lambda spec, session_id: MemorySink(spec, session_id), record_change))
        await service.start(path)
        await asyncio.sleep(0)  # flush the start-up notification
        await service.stop()
        await asyncio.sleep(0)
        calls.clear()
        await service.set_measurement_active(False)
        await service.set_selected(False)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertIsNone(service._notify_task)
        self.assertEqual(calls, [])


class ReceiverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "stdin.sock"
        self.sinks = []
        self.writers = []
        self.now = 100.0

        def factory(spec, session_id):
            sink = MemorySink(spec, session_id)
            self.sinks.append(sink)
            return sink

        self.service = StdinInputService(StdinInputDependencies(
            factory, ignore_change, monotonic=lambda: self.now))
        await self.service.start(self.path)

    async def asyncTearDown(self):
        await self.service.stop()
        for writer in self.writers:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
        self.temp.cleanup()

    async def connect(self, **overrides):
        reader, writer = await asyncio.open_unix_connection(self.path)
        self.writers.append(writer)
        header = dict(version=1, format="s16le", rate=48000, channels=2) | overrides
        writer.write(json.dumps(header).encode() + b"\n")
        await writer.drain()
        return reader, writer

    async def event(self, reader):
        return json.loads(await asyncio.wait_for(reader.readline(), 3))

    async def start_writer(self, **metadata):
        await self.service.set_selected(True)
        reader, writer = await self.connect(**metadata)
        self.assertEqual((await self.event(reader))["event"], "accepted")
        self.assertEqual((await self.event(reader))["event"], "ready")
        return reader, writer

    async def test_selection_gates_pcm_and_eof_keeps_listener_alive(self):
        reader, writer = await self.connect()
        self.assertEqual((await self.event(reader))["event"], "accepted")
        self.assertEqual(self.sinks, [])
        self.assertEqual(self.service.snapshot()["state"], "connected")
        await self.service.set_selected(True)
        self.assertEqual((await self.event(reader))["event"], "ready")
        payload = b"\x01\x00\x02\x00" * 100
        writer.write(payload)
        await writer.drain()
        writer.write_eof()
        self.assertEqual((await self.event(reader))["event"], "done")
        self.assertEqual(bytes(self.sinks[0].frames), payload)
        self.assertTrue(self.sinks[0].finished)
        await asyncio.wait_for(reader.read(), 2)
        self.assertEqual(self.service.snapshot()["state"], "waiting")
        self.assertTrue(self.service.snapshot()["selected"])
        self.assertTrue(self.service.snapshot()["available"])

    async def test_second_writer_cannot_replace_first(self):
        reader, writer = await self.start_writer()
        session_id = self.service.snapshot()["session_id"]
        second, _ = await self.connect()
        self.assertEqual((await self.event(second))["code"], "busy")
        self.assertEqual(self.service.snapshot()["session_id"], session_id)
        writer.write_eof()
        self.assertEqual((await self.event(reader))["event"], "done")

    async def buffered_child_writer(self):
        await self.service.set_selected(True)
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-c", r"""
import json, socket, sys, time
sock = socket.socket(socket.AF_UNIX)
sock.connect(sys.argv[1])
sock.sendall(b'{"version":1,"format":"s16le","rate":48000,"channels":2}\n')
events = sock.makefile('rb')
assert json.loads(events.readline())['event'] == 'accepted'
assert json.loads(events.readline())['event'] == 'ready'
print('ready', flush=True)
assert sys.stdin.readline().strip() == 'send'
sock.sendall(b'\x01\x00\x02\x00' * 32768)
print('queued', flush=True)
time.sleep(60)
""", str(self.path), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

        async def stop_child():
            if child.returncode is None:
                child.kill()
            await child.communicate()

        self.addAsyncCleanup(stop_child)
        self.assertEqual(await asyncio.wait_for(child.stdout.readline(), 3), b"ready\n")
        self.sinks[0].allow_write.clear()
        child.stdin.write(b"send\n")
        await child.stdin.drain()
        self.assertEqual(await asyncio.wait_for(child.stdout.readline(), 3), b"queued\n")
        await asyncio.sleep(0.05)
        return child

    async def test_live_buffered_writer_cannot_be_superseded(self):
        await self.buffered_child_writer()
        session_id = self.service.snapshot()["session_id"]
        second, _ = await self.connect()
        self.assertEqual((await self.event(second))["code"], "busy")
        self.assertEqual(self.service.snapshot()["session_id"], session_id)
        self.assertFalse(self.sinks[0].aborted)

    async def test_killed_buffered_writer_is_superseded_before_reader_eof(self):
        child = await self.buffered_child_writer()
        first_session = self.service._session
        child.kill()
        await child.wait()
        await asyncio.sleep(0.37)
        self.assertFalse(first_session.reader.at_eof())
        self.assertNotEqual(self.service.snapshot()["state"], "draining")

        second_reader, second_writer = await self.connect()
        accepted = await self.event(second_reader)
        self.assertEqual(accepted["event"], "accepted", accepted)
        self.assertEqual((await self.event(second_reader))["event"], "ready")
        self.assertNotEqual(accepted["session_id"], first_session.id)
        self.assertTrue(self.sinks[0].aborted)
        self.assertEqual(self.sinks[0].frames, b"")
        payload = b"\x03\x00\x04\x00" * 10
        second_writer.write(payload)
        await second_writer.drain()
        second_writer.write_eof()
        self.assertEqual((await self.event(second_reader))["event"], "done")
        self.assertEqual(bytes(self.sinks[1].frames), payload)

    async def test_crashed_writer_is_superseded_without_a_busy_rejection(self):
        """A killed writer must not lock out the pipefail restart.

        Regression: the client crash left the session draining the adapter,
        and the restart that followed was rejected with ``busy`` for the
        whole drain window, so the pipeline could never reconnect.
        """
        _, writer = await self.start_writer()
        first_sink = self.sinks[0]
        first_sink.finish_gate.clear()
        writer.close()  # the client is gone in both directions
        async with asyncio.timeout(2):
            while self.service.snapshot()["state"] != "draining":
                await asyncio.sleep(0.01)

        second_reader, second_writer = await self.connect()
        self.assertEqual((await self.event(second_reader))["event"], "accepted")
        self.assertEqual((await self.event(second_reader))["event"], "ready")
        self.assertTrue(first_sink.aborted, "the superseded session was retired")
        self.assertIsNotNone(self.service.snapshot()["session_id"])
        payload = b"\x01\x00\x02\x00" * 10
        second_writer.write(payload)
        await second_writer.drain()
        async with asyncio.timeout(2):
            while bytes(self.sinks[1].frames) != payload:
                await asyncio.sleep(0.01)

    async def test_writer_crash_mid_frame_never_returns_busy(self):
        """Crashing mid-frame must not leave a busy slot either."""
        _, writer = await self.start_writer(format="s24le", channels=8, left=5, right=6)
        writer.write(b"\x01\x02\x03")
        await writer.drain()
        writer.close()
        async with asyncio.timeout(2):
            while self.service.snapshot()["state"] in {"ready", "streaming"}:
                await asyncio.sleep(0.01)
        second_reader, _ = await self.connect()
        self.assertEqual((await self.event(second_reader))["event"], "accepted")

    async def test_fragmented_packed_frames_and_truncated_eof(self):
        reader, writer = await self.start_writer(format="s24le", channels=8, left=5, right=6)
        writer.write(bytes(range(25)))
        await writer.drain()
        writer.write_eof()
        result = await self.event(reader)
        self.assertEqual(result["code"], "truncated-frame")
        await asyncio.wait_for(reader.read(), 2)
        self.assertEqual(bytes(self.sinks[0].frames), bytes(range(24)))
        self.assertTrue(self.sinks[0].aborted)

    async def test_source_change_interrupts_backpressure_without_replaying(self):
        reader, writer = await self.start_writer()
        self.sinks[0].allow_write.clear()
        writer.write(b"\0" * 65536)
        await writer.drain()
        await asyncio.wait_for(self.service.set_selected(False), 2)
        self.assertEqual((await self.event(reader))["code"], "source-changed")
        self.assertTrue(self.sinks[0].aborted)
        await self.service.set_selected(True)
        self.assertEqual(self.service.snapshot()["state"], "waiting")
        self.assertEqual(self.sinks[0].frames, b"")

    async def test_measurement_ends_current_stream_and_blocks_new_writer(self):
        reader, _ = await self.start_writer()
        await self.service.set_measurement_active(True)
        self.assertEqual((await self.event(reader))["code"], "measurement-active")
        second, _ = await self.connect()
        self.assertEqual((await self.event(second))["code"], "measurement-active")
        await self.service.set_measurement_active(False)
        third, _ = await self.start_writer()
        self.assertTrue(self.service.snapshot()["routed"])

    async def test_power_requires_recent_frames_and_live_links(self):
        reader, writer = await self.start_writer()
        self.assertFalse(await self.service.active_for_power())
        writer.write(b"\0" * 400)
        await writer.drain()
        async with asyncio.timeout(2):
            while not self.service.snapshot()["frames_received"]:
                await asyncio.sleep(0.01)
        self.assertTrue(await self.service.active_for_power())
        self.sinks[0].linked = False
        self.assertFalse(await self.service.active_for_power())
        self.sinks[0].linked = True
        self.now += 2
        self.assertFalse(await self.service.active_for_power())
        writer.write_eof()
        self.assertEqual((await self.event(reader))["event"], "done")

    async def test_link_failure_terminates_an_idle_writer(self):
        reader, _ = await self.start_writer()
        self.sinks[0].linked = False
        self.sinks[0].repair_error = True
        self.assertEqual((await self.event(reader))["code"], "routing-lost")
        await asyncio.wait_for(reader.read(), 2)
        self.assertTrue(self.sinks[0].aborted)

    async def test_oversized_header_does_not_reserve_writer(self):
        reader, writer = await asyncio.open_unix_connection(self.path)
        self.writers.append(writer)
        writer.write(b"x" * 4097 + b"\n")
        await writer.drain()
        self.assertEqual((await self.event(reader))["code"], "invalid-header")
        await self.start_writer()

    async def test_shutdown_cleans_idle_clients_and_owned_socket(self):
        reader, _ = await self.start_writer()
        await asyncio.wait_for(self.service.stop(), 2)
        self.assertEqual((await self.event(reader))["code"], "service-stopped")
        self.assertFalse(self.path.exists())
        self.assertTrue(self.sinks[0].aborted)

    async def test_second_listener_never_unlinks_a_live_socket(self):
        other = StdinInputService(StdinInputDependencies(MemorySink, ignore_change))
        try:
            await other.start(self.path)
            self.assertFalse(other.snapshot()["available"])
            await other.stop()
            await self.start_writer()
        finally:
            await other.stop()


if __name__ == "__main__":
    unittest.main()
