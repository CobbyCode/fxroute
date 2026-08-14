#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Peak Monitor survival/relink across a native DSP effect-chain rebuild.

The native engine is terminated and relaunched on every effect-chain change
(helpers on/off).  PipeWire reuses the ``fxroute_dsp`` node id while the
object serial changes, so the monitor must detect the recreation by serial
and rearm the capture immediately instead of waiting for the no-data
timeout.  A fresh capture linked during the graph settle window must also
survive the normal no-data timeout (rebuild settle grace).
"""

import asyncio
import struct
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import peak_monitor
from peak_monitor import EasyEffectsPeakMonitor, MonitorTarget


class _FakeProc:
    def __init__(self):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode = None
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    async def wait(self):
        return self.returncode


def _audio_chunk(value: float = 0.5) -> bytes:
    return struct.pack("<f", value) * (peak_monitor.READ_SIZE // 4)


class PeakMonitorRebuildSurvivalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.emits = []
        self.monitor = EasyEffectsPeakMonitor(on_change=self._collect)
        self.monitor._running = True
        self.monitor._link_capture_stream = AsyncMock()
        self.procs = [_FakeProc(), _FakeProc()]
        self.created = []
        self.serial_state = {"value": 100}
        self.discovery_patcher = patch.object(
            self.monitor, "_discover_target",
            new=AsyncMock(side_effect=self._discover),
        )
        self.exec_patcher = patch(
            "peak_monitor.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=self._spawn),
        )
        self.interval_patcher = patch.object(peak_monitor, "TARGET_RECHECK_INTERVAL", 0.1)
        self.timeout_patcher = patch.object(peak_monitor, "CAPTURE_NO_DATA_TIMEOUT", 0.6)
        self.retry_patcher = patch.object(peak_monitor, "ERROR_RETRY_INTERVAL", 0.05)
        self.discovery_patcher.start()
        self.exec_patcher.start()
        self.interval_patcher.start()
        self.timeout_patcher.start()
        self.retry_patcher.start()

    async def asyncTearDown(self):
        for patcher in (self.discovery_patcher, self.exec_patcher,
                        self.interval_patcher, self.timeout_patcher, self.retry_patcher):
            patcher.stop()
        self.monitor._running = False
        for proc in self.procs:
            proc.returncode = 0
            proc.stdout.feed_eof()
            proc.stderr.feed_eof()
        if self.monitor._task and not self.monitor._task.done():
            self.monitor._task.cancel()
            try:
                await self.monitor._task
            except (asyncio.CancelledError, RuntimeError):
                pass

    async def _collect(self, snapshot):
        self.emits.append(dict(snapshot))

    async def _discover(self):
        return MonitorTarget(
            "fxroute_dsp", 77, "fxroute_dsp", serial=self.serial_state["value"],
        )

    async def _spawn(self, *args, **kwargs):
        proc = self.procs[len(self.created)]
        self.created.append(proc)
        return proc

    async def _wait_for(self, predicate, timeout: float = 3.0, interval: float = 0.01):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(interval)
        raise AssertionError("condition not met within %.1fs" % timeout)

    async def _feed(self, proc: _FakeProc):
        proc.stdout.feed_data(_audio_chunk())
        proc.stdout.feed_data(_audio_chunk())
        await self._wait_for(
            lambda: self.monitor._last_audio_sample_at is not None,
            timeout=1.0,
        )

    async def test_recapture_on_node_recreation_beats_no_data_timeout(self):
        monitor = self.monitor
        task = asyncio.create_task(monitor._run())
        monitor._task = task
        await self._wait_for(lambda: len(self.created) == 1)
        await self._feed(self.procs[0])
        self.assertEqual(monitor._target.serial, 100)
        self.assertTrue(monitor.snapshot()["vu_fresh"])

        # Effect-chain rebuild: the node is recreated with a new serial while
        # the id stays the same; the capture feed stops.
        self.serial_state["value"] = 101
        rebuilt_at = time.monotonic()

        await self._wait_for(lambda: len(self.created) >= 2, timeout=2.0)
        elapsed = time.monotonic() - rebuilt_at
        self.assertLess(
            elapsed, peak_monitor.CAPTURE_NO_DATA_TIMEOUT,
            "recapture waited for the no-data timeout instead of the serial check",
        )
        self.assertEqual(monitor._target.serial, 101)
        self.assertIs(monitor._proc, self.procs[1])

        await self._feed(self.procs[1])
        snapshot = monitor.snapshot()
        self.assertTrue(snapshot["available"])
        self.assertTrue(snapshot["vu_fresh"])
        self.assertEqual(snapshot["target"]["serial"], 101)

    async def test_fresh_capture_survives_settle_window_under_grace(self):
        monitor = self.monitor
        monitor._settle_until = time.monotonic() + 1.5
        task = asyncio.create_task(monitor._run())
        monitor._task = task
        await self._wait_for(lambda: len(self.created) == 1)

        # No audio data yet; the normal no-data timeout (0.6s) would fire,
        # but the rebuild settle grace must hold the capture alive.
        await asyncio.sleep(1.0)
        self.assertEqual(len(self.created), 1)
        self.assertIs(monitor._proc, self.procs[0])

        await self._feed(self.procs[0])
        self.assertTrue(monitor.snapshot()["vu_fresh"])

    async def test_expired_grace_falls_back_to_no_data_timeout(self):
        monitor = self.monitor
        monitor._settle_until = 0.0
        # Pre-set the target so the first-target grace does not re-arm.
        monitor._target = MonitorTarget("fxroute_dsp", 77, "fxroute_dsp", serial=100)
        task = asyncio.create_task(monitor._run())
        monitor._task = task
        await self._wait_for(lambda: len(self.created) == 1)
        await self._wait_for(lambda: len(self.created) >= 2, timeout=2.0)
        self.assertEqual(monitor._target.serial, 100)

    async def test_first_target_gets_settle_grace(self):
        monitor = self.monitor
        task = asyncio.create_task(monitor._run())
        monitor._task = task
        await self._wait_for(lambda: monitor._target is not None)
        self.assertGreater(monitor._settle_until, time.monotonic())
        await self._wait_for(lambda: len(self.created) == 1)

    async def test_degraded_settle_capture_rearms_once_after_grace(self):
        monitor = self.monitor
        # A capture armed during the settle window delivers data during the
        # grace but then develops periodic read timeouts after the graph
        # settled; the monitor must rearm once for a clean stream.
        monitor._settle_until = time.monotonic() + 0.8
        monitor._target = MonitorTarget("fxroute_dsp", 77, "fxroute_dsp", serial=100)
        task = asyncio.create_task(monitor._run())
        monitor._task = task
        await self._wait_for(lambda: len(self.created) == 1)

        # Feed during the grace (a clean phase), then stop so the post-grace
        # timeouts look like the degraded-stream gaps.
        async def feed_then_stop():
            deadline = time.monotonic() + 0.75
            while time.monotonic() < deadline:
                self.procs[0].stdout.feed_data(_audio_chunk())
                await asyncio.sleep(0.05)

        feeder = asyncio.create_task(feed_then_stop())
        await self._wait_for(
            lambda: len(self.created) >= 2, timeout=3.0)
        feeder.cancel()
        self.assertEqual(len(self.created), 2)
        self.assertEqual(monitor._target.serial, 100)
        self.assertIs(monitor._proc, self.procs[1])
        self.assertEqual(monitor._settle_until, 0.0)
        self.assertTrue(monitor._settle_rearmed)

        await self._feed(self.procs[1])
        self.assertTrue(monitor.snapshot()["vu_fresh"])

    async def test_clean_settle_capture_does_not_rearm(self):
        monitor = self.monitor
        monitor._settle_until = time.monotonic() + 0.6
        monitor._target = MonitorTarget("fxroute_dsp", 77, "fxroute_dsp", serial=100)
        task = asyncio.create_task(monitor._run())
        monitor._task = task
        await self._wait_for(lambda: len(self.created) == 1)

        # Continuous data before and after the grace expiry: a clean
        # negotiation must never be rearmed.
        async def feed_loop():
            while monitor._running:
                self.procs[0].stdout.feed_data(_audio_chunk())
                await asyncio.sleep(0.05)

        feeder = asyncio.create_task(feed_loop())
        try:
            await self._wait_for(
                lambda: monitor._last_audio_sample_at is not None, timeout=1.0)
            await asyncio.sleep(1.2)
            self.assertEqual(len(self.created), 1)
            self.assertIs(monitor._proc, self.procs[0])
            self.assertTrue(monitor.snapshot()["vu_fresh"])
        finally:
            feeder.cancel()

    def test_discovery_parses_node_serial(self):
        text = (
            "id 5, type PipeWire:Interface:Node/3\n"
            "\tobject.serial = \"2186115\"\n"
            "\tnode.name = \"fxroute_dsp\"\n"
            "\tmedia.type = \"Audio\"\n"
        )
        monitor = EasyEffectsPeakMonitor()
        with patch(
            "peak_monitor._run_bounded_command",
            new=AsyncMock(return_value=(0, text.encode(), b"")),
        ):
            target = asyncio.run(monitor._discover_target())
        self.assertEqual(target.source_id, 5)
        self.assertEqual(target.serial, 2186115)


if __name__ == "__main__":
    unittest.main()
