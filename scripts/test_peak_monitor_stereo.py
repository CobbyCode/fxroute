#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Focused regressions for per-channel (stereo L/R) peak/RMS metering.

The post-DSP peak monitor now deinterleaves the interleaved f32 stereo stream
so L and R are metered independently, while the existing joint ``vu_db`` and
``detected`` fields keep their pre-stereo semantics (joint peak = max over
both channels, joint RMS = RMS over all samples).  These tests cover channel
separation, partial-frame chunk boundaries, joint-value compatibility and
stop/reset of both channel states.
"""

import asyncio
import math
import struct
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dsp.peak_monitor as peak_monitor
from dsp.peak_monitor import DSPPeakMonitor, MonitorTarget

TARGET = MonitorTarget("fxroute_dsp", 42, "Output Level")


class _FakeProc:
    def __init__(self):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode = None

    def terminate(self):
        self.returncode = 0

    async def wait(self):
        return self.returncode


def _stereo_chunk(left: float, right: float) -> bytes:
    return struct.pack("<2f", left, right) * (peak_monitor.READ_SIZE // 8)


class StereoMetricsTests(unittest.TestCase):
    def test_deinterleaves_channels(self):
        frames = struct.pack("<2f", 0.8, 0.2) * 10
        m = DSPPeakMonitor._stereo_metrics(frames)
        self.assertAlmostEqual(m.peak_l, 0.8, places=6)
        self.assertAlmostEqual(m.rms_l, 0.8, places=6)
        self.assertAlmostEqual(m.peak_r, 0.2, places=6)
        self.assertAlmostEqual(m.rms_r, 0.2, places=6)
        self.assertAlmostEqual(m.peak, 0.8, places=6)
        self.assertAlmostEqual(m.rms, math.sqrt((0.8 ** 2 + 0.2 ** 2) / 2), places=6)

    def test_joint_rms_is_over_all_samples_not_max_of_channels(self):
        frames = struct.pack("<2f", 0.8, 0.0) * 10
        m = DSPPeakMonitor._stereo_metrics(frames)
        self.assertAlmostEqual(m.rms_l, 0.8, places=6)
        self.assertAlmostEqual(m.rms_r, 0.0, places=6)
        self.assertLess(m.rms, m.rms_l)
        self.assertAlmostEqual(m.rms, 0.8 / math.sqrt(2), places=6)

    def test_skips_non_finite_per_channel(self):
        frames = struct.pack("<2f", 0.5, float("nan"))
        m = DSPPeakMonitor._stereo_metrics(frames)
        self.assertAlmostEqual(m.rms_l, 0.5, places=6)
        self.assertAlmostEqual(m.rms_r, 0.0, places=6)
        self.assertAlmostEqual(m.peak_l, 0.5, places=6)
        self.assertAlmostEqual(m.peak_r, 0.0, places=6)


class FrameAlignmentTests(unittest.TestCase):
    def test_single_frame_split_across_reads(self):
        monitor = DSPPeakMonitor()
        frame = struct.pack("<2f", 0.25, 0.75)
        self.assertEqual(monitor._align_stereo_frames(frame[:5]), b"")
        self.assertEqual(monitor._pending_frame_bytes, frame[:5])
        out = monitor._align_stereo_frames(frame[5:])
        self.assertEqual(out, frame)
        self.assertEqual(monitor._pending_frame_bytes, b"")
        m = DSPPeakMonitor._stereo_metrics(out)
        self.assertAlmostEqual(m.peak_l, 0.25, places=6)
        self.assertAlmostEqual(m.peak_r, 0.75, places=6)

    def test_misaligned_reads_never_swap_channels(self):
        monitor = DSPPeakMonitor()
        data = struct.pack("<2f", 0.5, 0.9) * 100
        collected = b""
        i = 0
        sizes = (1, 3, 5, 7, 2, 6)
        while i < len(data):
            n = sizes[i % len(sizes)]
            end = min(i + n, len(data))
            collected += monitor._align_stereo_frames(data[i:end])
            i = end
        self.assertEqual(monitor._pending_frame_bytes, b"")
        self.assertEqual(collected, data)
        m = DSPPeakMonitor._stereo_metrics(collected)
        self.assertAlmostEqual(m.peak_l, 0.5, places=6)
        self.assertAlmostEqual(m.rms_l, 0.5, places=6)
        self.assertAlmostEqual(m.peak_r, 0.9, places=6)
        self.assertAlmostEqual(m.rms_r, 0.9, places=6)


class PeakDetectionTests(unittest.TestCase):
    def test_consecutive_hits_and_hold_transition(self):
        now = 100.0
        hits, hold, transitioned = DSPPeakMonitor._update_peak_detection(0.5, now, 1, 0.0)
        self.assertEqual(hits, 0)
        self.assertEqual(hold, 0.0)
        self.assertFalse(transitioned)

        hits, hold, transitioned = DSPPeakMonitor._update_peak_detection(1.2, now, 0, 0.0)
        self.assertEqual(hits, 1)
        self.assertEqual(hold, 0.0)
        self.assertFalse(transitioned)

        hits, hold, transitioned = DSPPeakMonitor._update_peak_detection(1.2, now, 1, 0.0)
        self.assertEqual(hits, 2)
        self.assertGreater(hold, 0.0)
        self.assertTrue(transitioned)

        hits, hold, transitioned = DSPPeakMonitor._update_peak_detection(1.2, now + 0.01, 2, hold)
        self.assertEqual(hits, 3)
        self.assertFalse(transitioned)

        hits, hold, transitioned = DSPPeakMonitor._update_peak_detection(0.1, now + 0.02, 3, hold)
        self.assertEqual(hits, 0)
        self.assertFalse(transitioned)


class PeakMonitorStereoIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.monitor = DSPPeakMonitor()
        self.monitor._running = True
        self.monitor._target = TARGET
        self.proc = _FakeProc()
        self.patchers = [
            patch(
                "dsp.peak_monitor.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=self.proc),
            ),
            patch.object(self.monitor, "_link_capture_stream", new=AsyncMock()),
            patch(
                "dsp.peak_monitor.get_samplerate_status",
                return_value={"force_rate": 48000},
            ),
        ]
        for p in self.patchers:
            p.start()
        self.task = asyncio.create_task(self.monitor._capture_target(TARGET))
        await self._wait_for(lambda: self.monitor._proc is self.proc)

    async def asyncTearDown(self):
        for p in self.patchers:
            p.stop()
        self.monitor._running = False
        self.proc.returncode = 0
        self.proc.stdout.feed_eof()
        self.proc.stderr.feed_eof()
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, RuntimeError):
                pass

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

    async def test_only_left_loud_right_quiet(self):
        await self._feed(_stereo_chunk(0.8, 0.02))
        snap = self.monitor.snapshot()
        self.assertIsNotNone(snap["vu_db_l"])
        self.assertIsNotNone(snap["vu_db_r"])
        self.assertGreater(snap["vu_db_l"], snap["vu_db_r"])
        self.assertFalse(snap["detected"])

    async def test_only_right_loud_left_quiet(self):
        await self._feed(_stereo_chunk(0.02, 0.8))
        snap = self.monitor.snapshot()
        self.assertGreater(snap["vu_db_r"], snap["vu_db_l"])
        self.assertFalse(snap["detected"])

    async def test_different_lr_rms_values(self):
        await self._feed(_stereo_chunk(0.8, 0.4))
        snap = self.monitor.snapshot()
        self.assertGreater(snap["vu_db_l"], snap["vu_db_r"])
        # Joint RMS is over all samples, so it must sit between L and R and
        # never be a simple max(L, R).
        self.assertGreater(snap["vu_db_l"], snap["vu_db"])
        self.assertGreater(snap["vu_db"], snap["vu_db_r"])

    async def test_peak_only_left(self):
        await self._feed(_stereo_chunk(1.5, 0.1))
        await self._feed(_stereo_chunk(1.5, 0.1))
        await self._wait_for(lambda: self.monitor._hold_until_l > 0.0)
        snap = self.monitor.snapshot()
        self.assertTrue(snap["detected"])
        self.assertTrue(snap["detected_l"])
        self.assertFalse(snap["detected_r"])
        self.assertIsNotNone(snap["last_over_at_l"])
        self.assertIsNone(snap["last_over_at_r"])

    async def test_peak_only_right(self):
        await self._feed(_stereo_chunk(0.1, 1.5))
        await self._feed(_stereo_chunk(0.1, 1.5))
        await self._wait_for(lambda: self.monitor._hold_until_r > 0.0)
        snap = self.monitor.snapshot()
        self.assertTrue(snap["detected"])
        self.assertFalse(snap["detected_l"])
        self.assertTrue(snap["detected_r"])
        self.assertIsNone(snap["last_over_at_l"])
        self.assertIsNotNone(snap["last_over_at_r"])

    async def test_peak_on_both_channels(self):
        await self._feed(_stereo_chunk(1.5, 1.5))
        await self._feed(_stereo_chunk(1.5, 1.5))
        await self._wait_for(
            lambda: self.monitor._hold_until_l > 0.0 and self.monitor._hold_until_r > 0.0
        )
        snap = self.monitor.snapshot()
        self.assertTrue(snap["detected"])
        self.assertTrue(snap["detected_l"])
        self.assertTrue(snap["detected_r"])

    async def test_joint_vu_db_and_detected_stay_compatible(self):
        await self._feed(_stereo_chunk(0.5, 0.5))
        snap = self.monitor.snapshot()
        self.assertIsNotNone(snap["vu_db"])
        self.assertIsNotNone(snap["vu_db_l"])
        self.assertIsNotNone(snap["vu_db_r"])
        self.assertAlmostEqual(snap["vu_db"], snap["vu_db_l"], places=1)
        self.assertAlmostEqual(snap["vu_db"], snap["vu_db_r"], places=1)
        self.assertFalse(snap["detected"])
        self.assertFalse(snap["detected_l"])
        self.assertFalse(snap["detected_r"])

    async def test_stop_resets_both_channel_states(self):
        await self._feed(_stereo_chunk(1.5, 0.1))
        await self._feed(_stereo_chunk(1.5, 0.1))
        await self._wait_for(lambda: self.monitor._hold_until_l > 0.0)

        await self.monitor.stop()

        snap = self.monitor.snapshot()
        self.assertFalse(snap["available"])
        self.assertFalse(snap["detected"])
        self.assertFalse(snap["detected_l"])
        self.assertFalse(snap["detected_r"])
        self.assertIsNone(snap["vu_db"])
        self.assertIsNone(snap["vu_db_l"])
        self.assertIsNone(snap["vu_db_r"])
        self.assertIsNone(snap["last_over_at_l"])
        self.assertIsNone(snap["last_over_at_r"])


if __name__ == "__main__":
    unittest.main()
