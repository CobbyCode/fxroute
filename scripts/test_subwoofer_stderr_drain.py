#!/usr/bin/env python3
"""Native 2.1 helper stderr must be drained continuously into a bounded tail."""

import asyncio
import pathlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from subwoofer_runtime import HELPER_STDERR_TAIL_LIMIT, Subwoofer21Runtime


class StderrProcess:
    """Fake helper process with an asyncio stderr stream."""

    def __init__(self, stderr=None):
        self.pid = 4242
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.stderr = stderr if stderr is not None else asyncio.StreamReader()

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class SubwooferStderrDrainTests(unittest.IsolatedAsyncioTestCase):
    def _runtime(self, process):
        runtime = Subwoofer21Runtime(helper_binary=Path("/bin/true"))
        runtime._process = process
        return runtime

    def _pending_non_current_tasks(self):
        current = asyncio.current_task()
        return [task for task in asyncio.all_tasks() if task is not current and not task.done()]

    async def test_drain_consumes_more_than_a_pipe_buffer_without_stalling(self):
        process = StderrProcess()
        runtime = self._runtime(process)
        runtime._start_helper_stderr_drain()

        # More than 2x the default kernel pipe buffer (64 KiB) in one burst.
        line = b"stage1 diagnostic line 0123456789\n" * 20
        for _ in range(160):
            process.stderr.feed_data(line)
        process.stderr.feed_eof()

        for _ in range(100):
            if runtime._helper_stderr_drain_task is None:
                break
            await asyncio.sleep(0.01)
        self.assertIsNone(runtime._helper_stderr_drain_task)

        tail = runtime._helper_stderr_tail
        self.assertLessEqual(len(tail), HELPER_STDERR_TAIL_LIMIT)
        # The tail ends with the last emitted diagnostics.
        self.assertTrue(tail.endswith(b"0123456789\n"))
        self.assertNotEqual(len(tail), len(line) * 160)

    async def test_last_error_message_is_diagnostically_available(self):
        process = StderrProcess()
        runtime = self._runtime(process)
        runtime._start_helper_stderr_drain()
        process.stderr.feed_data(
            b"ignore this early noise\n"
            b"FATAL: cannot open PipeWire connection\n"
        )
        process.stderr.feed_eof()
        for _ in range(100):
            if runtime._helper_stderr_drain_task is None:
                break
            await asyncio.sleep(0.01)

        self.assertIn("FATAL: cannot open PipeWire connection", await runtime._read_helper_stderr())

    async def test_stop_helper_cancels_and_drains_stderr_task(self):
        process = StderrProcess()
        runtime = self._runtime(process)
        runtime._start_helper_stderr_drain()
        drain_task = runtime._helper_stderr_drain_task
        self.assertIsNotNone(drain_task)

        await runtime._stop_helper()

        self.assertIsNone(runtime._helper_stderr_drain_task)
        self.assertTrue(drain_task.done())
        self.assertTrue(drain_task.cancelled())
        self.assertEqual(self._pending_non_current_tasks(), [])

    async def test_drain_ends_by_itself_on_helper_exit(self):
        process = StderrProcess()
        runtime = self._runtime(process)
        runtime._start_helper_stderr_drain()
        drain_task = runtime._helper_stderr_drain_task

        process.stderr.feed_data(b"clean shutdown\n")
        process.stderr.feed_eof()
        for _ in range(100):
            if runtime._helper_stderr_drain_task is None:
                break
            await asyncio.sleep(0.01)

        self.assertIsNone(runtime._helper_stderr_drain_task)
        self.assertTrue(drain_task.done())
        self.assertIn("clean shutdown", await runtime._read_helper_stderr())
        self.assertEqual(self._pending_non_current_tasks(), [])

    async def test_no_stderr_stream_means_no_drain_task(self):
        process = StderrProcess()
        process.stderr = None
        runtime = self._runtime(process)
        runtime._start_helper_stderr_drain()
        self.assertIsNone(runtime._helper_stderr_drain_task)
        self.assertEqual(await runtime._read_helper_stderr(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
