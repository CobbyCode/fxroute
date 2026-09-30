#!/usr/bin/env python3
"""Shared line-source monitor pieces: loss confirmation and the tick loop."""

import asyncio
import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audio.samplerate.overview as overview_mod
import audio.source_monitor as source_monitor
from audio.source_feed import SourceTransitionLock
from audio.source_monitor import UnavailabilityConfirmation, run_source_monitor_loop


class UnavailabilityConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.confirmation = UnavailabilityConfirmation(5.0, monotonic=lambda: self.now)

    def test_one_observed_failure_never_confirms(self):
        self.confirmation.observe(False)
        self.now += 600
        self.assertFalse(self.confirmation.confirmed())

    def test_two_failures_confirm_once_the_window_passed(self):
        self.confirmation.observe(False)
        self.now += 3
        self.confirmation.observe(False)
        self.assertFalse(self.confirmation.confirmed(), "3 s into the outage")
        self.now += 2
        self.assertTrue(self.confirmation.confirmed(), "5 s after the first failure")

    def test_an_available_observation_or_reset_starts_over(self):
        for clear in (lambda: self.confirmation.observe(True), self.confirmation.reset):
            self.confirmation.observe(False)
            self.now += 6
            self.confirmation.observe(False)
            self.assertTrue(self.confirmation.confirmed())
            clear()
            self.assertFalse(self.confirmation.confirmed())


class OverviewReaderTests(unittest.TestCase):
    """The overview reads the configured decision and never records probes."""

    def setUp(self):
        self.available = False
        probe = lambda: {"roles": {"bluetooth_input": {"selectable": self.available}}, "receiver_session": {}}
        for patcher in (
            patch.object(overview_mod, "_load_audio_source_selection",
                         return_value={"mode": "bluetooth-input", "selected_input_key": None}),
            patch.object(overview_mod, "_run_command", side_effect=lambda args: ""),
            patch.object(overview_mod, "get_bluetooth_audio_overview", side_effect=probe),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(overview_mod.configure_source_availability)

    def _mode(self):
        return overview_mod.get_audio_source_overview()["mode"]

    def test_without_a_configured_owner_the_raw_probe_decides(self):
        overview_mod.configure_source_availability()
        self.assertEqual(self._mode(), "app-playback")

    def test_the_owner_decides_when_a_loss_counts(self):
        confirmed = [False]
        overview_mod.configure_source_availability(bluetooth_loss_confirmed=lambda: confirmed[0])
        self.assertEqual(self._mode(), "bluetooth-input", "unconfirmed: stay selected")
        confirmed[0] = True
        self.assertEqual(self._mode(), "app-playback", "confirmed: fall back")
        self.available = True
        self.assertEqual(self._mode(), "bluetooth-input", "a present adapter always wins")


class MonitorLoopTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, *, idle, act, build=lambda: {"mode": "app-playback"}, lock_provider=None, sleeps=2):
        slept = []

        async def fake_sleep(delay):
            slept.append(delay)
            if len(slept) >= sleeps:
                raise asyncio.CancelledError

        with patch.object(source_monitor.asyncio, "sleep", fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await run_source_monitor_loop(name="Test", interval=3, idle=idle, build=build,
                                              act=act, lock_provider=lock_provider)
        return slept

    async def test_idle_ticks_are_skipped(self):
        ticks, builds = [], []

        async def act(_overview):
            ticks.append(1)

        slept = await self._run(idle=lambda: True, build=lambda: builds.append(1) or {}, act=act)
        self.assertEqual((builds, ticks, slept), ([], [], [3, 3]), "an idle tick builds nothing")

    async def test_ticks_build_outside_and_act_under_the_lock(self):
        lock = SourceTransitionLock()
        built_while_locked, acted_while_locked = [], []

        def build():
            built_while_locked.append(lock.locked())
            return {"mode": "bluetooth-input"}

        async def act(overview):
            acted_while_locked.append((lock.locked(), overview["mode"]))

        await self._run(idle=lambda: False, build=build, act=act, lock_provider=lambda: lock)
        self.assertEqual(built_while_locked, [False, False], "a slow probe never holds the lock")
        self.assertEqual(acted_while_locked, [(True, "bluetooth-input")] * 2)

    async def test_a_failing_tick_is_logged_and_the_loop_goes_on(self):
        async def act(_overview):
            raise RuntimeError("probe failed")

        with self.assertLogs("audio.source_monitor", level="DEBUG") as logs:
            slept = await self._run(idle=lambda: False, act=act)
        self.assertEqual(slept, [3, 3])
        self.assertIn("Test monitor loop check failed: probe failed", logs.output[0])

    async def test_a_run_of_skipped_ticks_is_reported_once_per_run(self):
        """Skipped ticks are reported once; a validated tick starts over."""
        acted = []
        script = iter([None] * 5 + [{"mode": "bluetooth-input"}] + [None] * 3)

        @asynccontextmanager
        async def scripted_build(_lock, _build):
            yield next(script)

        async def act(overview):
            acted.append(overview["mode"])

        with patch.object(source_monitor, "validated_build", scripted_build), patch.object(
            source_monitor, "SOURCE_MONITOR_SKIPPED_TICKS_REPORT", 3
        ), self.assertLogs("audio.source_monitor", level="INFO") as logs:
            await self._run(idle=lambda: False, act=act, lock_provider=lambda: object(), sleeps=9)
        reports = [line for line in logs.output if "Test monitor skipped 3 ticks in a row" in line]
        self.assertEqual(len(reports), 2, logs.output)
        self.assertEqual(acted, ["bluetooth-input"])

    async def test_cancellation_inside_a_tick_stops_the_loop(self):
        async def act(_overview):
            raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await run_source_monitor_loop(name="Test", interval=3, idle=lambda: False,
                                          build=lambda: {}, act=act, lock_provider=None)


if __name__ == "__main__":
    unittest.main()
