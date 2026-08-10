#!/usr/bin/env python3
"""Focused regressions for the peak monitor's emit/backpressure behaviour.

Sustained clipping must produce exactly one immediate detected=true state
change (not one broadcast per audio block), further clip hits must extend the
hold, hold expiry must emit exactly one detected=false, and a later clip must
trigger a fresh false->true transition while normal VU updates keep flowing.
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

TARGET = MonitorTarget("ee_soe_output_level", 42, "Output Level")


class _FakeProc:
    def __init__(self):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode = None

    async def wait(self):
        return self.returncode


class PeakMonitorEmitFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.emits = []
        self.monitor = EasyEffectsPeakMonitor(on_change=self._collect)
        self.monitor._running = True
        self.monitor._target = TARGET
        self.proc = _FakeProc()
        self.patcher = patch(
            "peak_monitor.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=self.proc),
        )
        self.patcher.start()
        self.monitor._link_capture_stream = AsyncMock()
        self.task = asyncio.create_task(self.monitor._capture_target(TARGET))
        await self._wait_for(lambda: len(self.emits) >= 1)

    async def asyncTearDown(self):
        self.proc.returncode = 0
        self.proc.stdout.feed_eof()
        self.proc.stderr.feed_eof()
        self.patcher.stop()
        self.monitor._running = False
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, RuntimeError):
                pass

    async def _collect(self, snapshot):
        self.emits.append(dict(snapshot))

    def _chunk(self, value: float) -> bytes:
        return struct.pack("<f", value) * (peak_monitor.READ_SIZE // 4)

    async def _wait_for(self, predicate, timeout: float = 2.0, interval: float = 0.005):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(interval)
        raise AssertionError("condition not met within %.1fs" % timeout)

    async def _feed(self, chunk: bytes):
        fed_at = time.monotonic()
        self.proc.stdout.feed_data(chunk)
        await self._wait_for(
            lambda: self.monitor._last_audio_sample_at is not None
            and self.monitor._last_audio_sample_at >= fed_at - 0.001
        )

    async def _wait_hold_settled(self, timeout: float = 1.0):
        deadline = time.monotonic() + timeout
        last = -1.0
        while time.monotonic() < deadline:
            current = self.monitor._hold_until
            if current > 0.0 and current == last:
                return current
            last = current
            await asyncio.sleep(0.005)
        raise AssertionError("hold did not settle within %.1fs" % timeout)

    def _transitions(self) -> list:
        out = []
        prev = None
        for emit in self.emits:
            detected = bool(emit["detected"])
            if prev is not None and detected != prev:
                out.append("false->true" if detected else "true->false")
            prev = detected
        return out

    async def test_sustained_clipping_emits_only_state_transitions(self):
        monitor = self.monitor
        self.assertFalse(self.emits[0]["detected"])
        self.assertTrue(self.emits[0]["available"])

        first_hit = self.monitor._hold_until
        self.assertEqual(first_hit, 0.0)

        await self._feed(self._chunk(1.5))
        await self._feed(self._chunk(1.5))
        await self._wait_for(lambda: self.emits[-1]["detected"])
        hold_after_transition = monitor._hold_until
        self.assertGreater(hold_after_transition, 0.0)
        self.assertTrue(self.emits[-1]["last_over_at"] is not None)
        self.assertEqual(len([e for e in self.emits if e["detected"]]), 1)
        self.assertEqual(self._transitions(), ["false->true"])

        await self._feed(self._chunk(1.5))
        self.assertGreater(monitor._hold_until, hold_after_transition)
        for _ in range(57):
            self.proc.stdout.feed_data(self._chunk(1.5))
        settled_hold = await self._wait_hold_settled()
        self.assertGreater(settled_hold, hold_after_transition)
        self.assertEqual(self._transitions(), ["false->true"])
        self.assertEqual(len([e for e in self.emits if e["detected"]]), 1)

        await self._wait_for(lambda: self._transitions().count("true->false") == 1)

        await self._feed(self._chunk(0.1))
        await self._feed(self._chunk(1.5))
        await asyncio.sleep(0.02)
        self.assertEqual(self._transitions(), ["false->true", "true->false"])
        self.assertEqual(len([e for e in self.emits if e["detected"]]), 1)

        await self._feed(self._chunk(1.5))
        await self._wait_for(lambda: self._transitions().count("false->true") == 2)
        self.assertEqual(len([e for e in self.emits if e["detected"]]), 2)
        self.assertTrue(self.emits[-1]["last_over_at"] is not None)

        targets = (0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.15, 0.1, 0.05, 0.0)
        for value in targets:
            await self._feed(self._chunk(value))
            await asyncio.sleep(0.03)
        await asyncio.sleep(0.15)

        last_true_index = max(
            i for i, e in enumerate(self.emits) if e["detected"]
        )
        after_peak = self.emits[last_true_index:]
        undetected_vu = [
            round(e["vu_db"], 1)
            for e in after_peak
            if not e["detected"] and e["vu_db"] is not None
        ]
        self.assertGreaterEqual(len(set(undetected_vu)), 2)
        self.assertGreaterEqual(len(undetected_vu), 2)

        self.assertEqual(
            self._transitions(),
            ["false->true", "true->false", "false->true", "true->false"],
        )
        self.assertEqual(len([e for e in self.emits if e["detected"]]), 2)

        self.proc.returncode = 0
        self.proc.stdout.feed_eof()
        self.proc.stderr.feed_eof()
        await asyncio.wait_for(self.task, timeout=2.0)
        self.assertTrue(self.task.done())
        self.assertIsNone(monitor._proc)
        self.assertEqual(monitor._consecutive_hits, 0)


if __name__ == "__main__":
    unittest.main()
