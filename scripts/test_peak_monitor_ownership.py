#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Peak monitor process ownership: stop/restart must fully complete the
child/task lifecycle before publishing new state or freeing ownership.

- Stopping a running monitor reaps the child and joins the task; no
  child/task is left behind and no further state is published.
- Cancelling stop() mid-reap still reaps the child and joins the task
  (shielded drain) before the cancellation propagates.
- A superseded run (old generation) can no longer publish state over a
  newer run or after ownership ends.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import dsp.peak_monitor as peak_monitor
from dsp.peak_monitor import DSPPeakMonitor, MonitorTarget

TARGET = MonitorTarget("fxroute_dsp", 42, "Output Level")


class _FakeProc:
    """Fake pw-record child with a real communicate() contract."""

    def __init__(self, exit_on_terminate=True):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode = None
        self.terminated = False
        self.killed = False
        self._exited = asyncio.Event()
        self._exit_on_terminate = exit_on_terminate

    def terminate(self):
        self.terminated = True
        if self._exit_on_terminate:
            self.returncode = 0
            self._exited.set()

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._exited.set()

    async def wait(self):
        await self._exited.wait()
        return self.returncode

    async def communicate(self):
        await self._exited.wait()
        return (b"", b"")


class PeakMonitorOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task() and task.get_name() == "fxroute-dsp-peak-monitor" \
                    and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    def _idle_monitor(self, emits):
        async def _collect(snapshot):
            emits.append(dict(snapshot))

        monitor = DSPPeakMonitor(on_change=_collect)
        monitor._discover_target = AsyncMock(return_value=None)
        return monitor

    async def test_stop_during_running_monitor_reaps_child_and_task(self):
        emits = []
        monitor = self._idle_monitor(emits)
        await monitor.start()
        task = monitor._task
        proc = _FakeProc()
        monitor._proc = proc
        await monitor.stop()
        self.assertTrue(proc.terminated)
        self.assertIsNotNone(proc.returncode)
        self.assertTrue(task.done())
        self.assertIsNone(monitor._task)
        self.assertIsNone(monitor._proc)
        self.assertFalse(monitor._running)

    async def test_cancelled_stop_still_reaps_child_before_propagating(self):
        emits = []
        monitor = self._idle_monitor(emits)
        proc = _FakeProc(exit_on_terminate=False)
        monitor._proc = proc

        async def stuck():
            await asyncio.sleep(10)

        monitor._task = asyncio.create_task(stuck(), name="fxroute-dsp-peak-monitor")
        with patch.object(peak_monitor, "PEAK_MONITOR_COMMAND_TERMINATE_GRACE_SECONDS", 0.05):
            stop_task = asyncio.create_task(monitor.stop())
            await asyncio.sleep(0.02)
            # Ownership is held while the shielded drain is in flight.
            self.assertIs(monitor._proc, proc)
            self.assertIsNotNone(monitor._task)
            stop_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await stop_task
        self.assertTrue(proc.terminated)
        self.assertTrue(proc.killed)
        self.assertIsNotNone(proc.returncode)
        self.assertIsNone(monitor._proc)
        self.assertIsNone(monitor._task)

    async def test_stale_generation_cannot_publish_after_stop(self):
        emits = []
        monitor = self._idle_monitor(emits)
        monitor._running = True
        monitor._target = TARGET
        old_generation = monitor._generation
        await monitor._emit_if_changed(force=True, generation=old_generation)
        self.assertEqual(len(emits), 1)
        await monitor.stop()
        # A rescheduled old task retrying its emit must be dropped.
        await monitor._emit_if_changed(force=True, generation=old_generation)
        self.assertEqual(len(emits), 1)
        self.assertFalse(monitor.snapshot()["available"])

    async def test_new_run_publishes_after_old_run_stopped(self):
        emits = []
        monitor = self._idle_monitor(emits)
        monitor._running = True
        monitor._target = TARGET
        old_generation = monitor._generation
        await monitor._emit_if_changed(force=True, generation=old_generation)
        await monitor.stop()
        await monitor.start()
        self.assertGreater(monitor._generation, old_generation)
        monitor._target = TARGET
        await monitor._emit_if_changed(force=True)
        self.assertEqual(len(emits), 2)
        self.assertTrue(emits[-1]["available"])
        await monitor.stop()
        self.assertEqual(len(emits), 2)

    async def test_restart_completes_old_lifecycle_before_starting_new(self):
        emits = []
        monitor = self._idle_monitor(emits)
        await monitor.start()
        first_task = monitor._task
        first_generation = monitor._generation
        proc = _FakeProc()
        monitor._proc = proc
        await monitor.restart()
        self.assertTrue(first_task.done())
        self.assertIsNotNone(proc.returncode)
        self.assertIsNot(monitor._task, first_task)
        self.assertFalse(monitor._task.done())
        self.assertGreater(monitor._generation, first_generation)
        await monitor.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
