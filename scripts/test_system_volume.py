#!/usr/bin/env python3
"""system_volume: live reads stay live; only the status path is cached."""

import asyncio
import logging
import pathlib
import subprocess
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import audio.system_volume as system_volume


def _completed(returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class SystemVolumeCommandTests(unittest.TestCase):
    def setUp(self):
        system_volume._status_volume_cache = None

    def tearDown(self):
        system_volume._status_volume_cache = None

    def test_successful_read_parses_percent(self):
        with mock.patch(
            "audio.system_volume.subprocess.run",
            return_value=_completed(0, "Volume: 0.42\n"),
        ) as run:
            self.assertEqual(system_volume.get_output_volume(), 42)
        run.assert_called_once()
        self.assertIn("timeout", run.call_args.kwargs)

    def test_get_output_volume_stays_live(self):
        with mock.patch(
            "audio.system_volume.subprocess.run",
            return_value=_completed(0, "Volume: 0.25\n"),
        ) as run:
            self.assertEqual(system_volume.get_output_volume(), 25)
            self.assertEqual(system_volume.get_output_volume(), 25)
        # Both calls must reach the hardware: no read caching for the live API.
        self.assertEqual(run.call_count, 2)

    def test_get_node_volume_stays_live(self):
        with mock.patch(
            "audio.system_volume.subprocess.run",
            return_value=_completed(0, "Volume: 0.60\n"),
        ) as run:
            self.assertEqual(system_volume.get_node_volume("mic_source"), 60)
            self.assertEqual(system_volume.get_node_volume("mic_source"), 60)
        self.assertEqual(run.call_count, 2)

    def test_node_volume_is_never_cached(self):
        # A node read must not touch the status cache at all.
        with mock.patch(
            "audio.system_volume.subprocess.run",
            return_value=_completed(0, "Volume: 0.60\n"),
        ):
            system_volume.get_node_volume("mic_source")
        self.assertIsNone(system_volume._status_volume_cache)

    def test_nonzero_exit_raises_system_volume_error(self):
        with mock.patch(
            "audio.system_volume.subprocess.run",
            return_value=_completed(1, "", "sink does not exist"),
        ):
            with self.assertRaises(system_volume.SystemVolumeError) as raised:
                system_volume.get_output_volume()
        self.assertIn("sink does not exist", str(raised.exception))

    def test_timeout_raises_system_volume_error(self):
        with mock.patch(
            "audio.system_volume.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["wpctl"], 3.0),
        ):
            with self.assertRaises(system_volume.SystemVolumeError) as raised:
                system_volume.get_output_volume()
        self.assertIn("timed out", str(raised.exception))

    def test_parsing_is_unchanged(self):
        for output, expected in [
            ("Volume: 0.00\n", 0),
            ("Volume: 0.50\n", 50),
            ("Volume: 1.00\n", 100),
            ("Volume: 0.999\n", 100),
        ]:
            with mock.patch(
                "audio.system_volume.subprocess.run",
                return_value=_completed(0, output),
            ):
                self.assertEqual(system_volume.get_output_volume(), expected)

    def test_unclamped_read_reports_above_100(self):
        # PipeWire can be set above 100% externally (unity); the safety read
        # must report the real percent instead of under-estimating 100.
        with mock.patch(
            "audio.system_volume.subprocess.run",
            return_value=_completed(0, "Volume: 1.25\n"),
        ) as run:
            self.assertEqual(system_volume.get_output_volume_unclamped(), 125)
        run.assert_called_once()

    def test_unclamped_read_stays_live_and_clamped_api_is_unchanged(self):
        with mock.patch(
            "audio.system_volume.subprocess.run",
            return_value=_completed(0, "Volume: 1.25\n"),
        ) as run:
            self.assertEqual(system_volume.get_output_volume_unclamped(), 125)
            self.assertEqual(system_volume.get_output_volume_unclamped(), 125)
            # Exactly two reads: the safety read is never cached either.
            self.assertEqual(run.call_count, 2)

    def test_unclamped_read_rounds_percent(self):
        with mock.patch(
            "audio.system_volume.subprocess.run",
            return_value=_completed(0, "Volume: 1.048\n"),
        ):
            self.assertEqual(system_volume.get_output_volume_unclamped(), 105)

    def test_volume_percent_to_linear_gain_default_still_clamps_above_100(self):
        self.assertEqual(system_volume.volume_percent_to_linear_gain(100), 1.0)
        self.assertEqual(system_volume.volume_percent_to_linear_gain(150), 1.0)

    def test_volume_percent_to_linear_gain_unclamped_keeps_real_gain(self):
        # Safety semantics: a >100% master must not be under-estimated.
        self.assertEqual(
            system_volume.volume_percent_to_linear_gain(150, clamp_upper=False),
            1.5 ** 3,
        )
        self.assertEqual(
            system_volume.volume_percent_to_linear_gain(125, clamp_upper=False),
            1.25 ** 3,
        )
        self.assertEqual(
            system_volume.volume_percent_to_linear_gain(-5, clamp_upper=False),
            0.0,
        )
        self.assertEqual(
            system_volume.volume_percent_to_linear_gain(50, clamp_upper=False),
            0.125,
        )

    def test_set_writes_then_reads_back_fresh(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            if args[1] == "get-volume":
                return _completed(0, "Volume: 0.37\n")
            return _completed(0, "")

        with mock.patch("audio.system_volume.subprocess.run", side_effect=fake_run):
            self.assertEqual(system_volume.set_output_volume(37), 37)
        self.assertEqual(
            [args[1] for args in calls], ["set-volume", "get-volume"]
        )

    def test_readback_failure_marks_set_as_already_applied(self):
        def fake_run(args, **kwargs):
            if args[1] == "get-volume":
                return _completed(0, "unreadable")
            return _completed(0, "")

        with mock.patch("audio.system_volume.subprocess.run", side_effect=fake_run):
            with self.assertRaises(system_volume.SystemVolumeReadbackError) as raised:
                system_volume.set_output_volume(37)
        self.assertTrue(raised.exception.volume_write_applied)
        self.assertEqual(system_volume.get_status_volume(), 37)

    def test_unexpected_readback_failure_marks_set_as_already_applied(self):
        calls = 0

        def fake_run(args, **kwargs):
            nonlocal calls
            calls += 1
            if args[1] == "get-volume":
                raise OSError("readback transport failed")
            return _completed(0, "")

        with mock.patch("audio.system_volume.subprocess.run", side_effect=fake_run):
            with self.assertRaises(system_volume.SystemVolumeReadbackError) as raised:
                system_volume.set_output_volume(37)
        self.assertTrue(raised.exception.volume_write_applied)
        self.assertEqual(calls, 2)


class SystemVolumeStatusCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        system_volume._status_volume_cache = None
        await system_volume.stop_volume_read_monitor()

    def test_empty_cache_never_spawns_wpctl(self):
        system_volume._status_volume_cache = None
        with mock.patch("audio.system_volume.subprocess.run") as run:
            self.assertEqual(system_volume.get_status_volume(), 100)
            self.assertEqual(system_volume.get_status_volume(42), 42)
        run.assert_not_called()

    def test_status_cache_returns_last_known_value(self):
        system_volume._publish_status_volume(37, 10.0)
        with mock.patch("audio.system_volume.subprocess.run") as run:
            self.assertEqual(system_volume.get_status_volume(), 37)
        run.assert_not_called()

    def test_successful_set_updates_status_cache_immediately(self):
        def fake_run(args, **kwargs):
            if args[1] == "get-volume":
                return _completed(0, "Volume: 0.80\n")
            return _completed(0, "")

        with mock.patch("audio.system_volume.subprocess.run", side_effect=fake_run):
            self.assertEqual(system_volume.set_output_volume(80), 80)
        with mock.patch("audio.system_volume.subprocess.run") as run:
            self.assertEqual(system_volume.get_status_volume(), 80)
            run.assert_not_called()

    def test_stale_concurrent_monitor_read_cannot_overwrite_newer_set(self):
        # Monitor read started at t=1.0 (old value), set readback at t=1.2.
        system_volume._publish_status_volume(37, 1.0)
        system_volume._publish_status_volume(55, 1.2)
        # The stale monitor read completes afterwards with its old value.
        system_volume._publish_status_volume(37, 1.0)
        self.assertEqual(system_volume.get_status_volume(), 55)
        # A monitor read that started after the set may publish its value.
        system_volume._publish_status_volume(37, 1.5)
        self.assertEqual(system_volume.get_status_volume(), 37)


    async def test_monitor_publishes_external_change(self):
        system_volume._publish_status_volume(30, 1.0)
        with mock.patch(
            "audio.system_volume.subprocess.run",
            return_value=_completed(0, "Volume: 0.70\n"),
        ) as run:
            task = system_volume.start_volume_read_monitor()
            self.assertIs(task, system_volume._volume_monitor_task)
            for _ in range(100):
                if system_volume.get_status_volume() == 70:
                    break
                await asyncio.sleep(0.02)
        self.assertEqual(system_volume.get_status_volume(), 70)
        run.assert_called()

    def test_monitor_observes_selected_sink_levels_off_the_hot_status_path(self):
        observations = []
        with mock.patch.object(system_volume, '_output_level_observer',
                               lambda: observations.append('observed'), create=True), \
                mock.patch.object(system_volume, 'get_output_volume', return_value=26):
            self.assertEqual(system_volume._read_status_volume()[0], 26)
            system_volume.get_status_volume()
        self.assertEqual(observations, ['observed'])

    async def _run_monitor_for(self, ticks, run):
        """Run the monitor loop for ``ticks`` refreshes with the sleep between them skipped."""
        real_sleep = asyncio.sleep
        sleeps = []

        async def sleep(delay):
            sleeps.append(delay)
            if len(sleeps) >= ticks:
                raise asyncio.CancelledError
            await real_sleep(0)

        with mock.patch("audio.system_volume.subprocess.run", side_effect=run), \
                mock.patch("audio.system_volume._selected_output_name", return_value="alsa_output.test"), \
                mock.patch("audio.system_volume.asyncio.sleep", sleep), \
                self.assertLogs("audio.system_volume", level="INFO") as logs:
            with self.assertRaises(asyncio.CancelledError):
                await system_volume._volume_monitor_loop()
            logger = logging.getLogger("audio.system_volume")
            logger.info("end of run")
        return [r for r in logs.records if r.getMessage() != "end of run"]

    async def test_missing_sink_is_one_compact_warning_per_outage(self):
        system_volume._status_volume_cache = (37, 0.0)
        missing = _completed(1, stderr="Failed to get sink information: No such entity")
        records = await self._run_monitor_for(5, lambda *a, **k: missing)
        self.assertEqual(len(records), 1, "one warning for five failed refreshes")
        self.assertEqual(records[0].levelname, "WARNING")
        self.assertIsNone(records[0].exc_info, "no traceback")
        self.assertIn("No such entity", records[0].getMessage())
        self.assertEqual(system_volume.get_status_volume(), 37, "the last value stays")

    async def test_returning_sink_ends_the_outage_with_one_info_and_a_new_one_warns_again(self):
        system_volume._status_volume_cache = (37, 0.0)
        missing = _completed(1, stderr="Failed to get sink information: No such entity")
        present = _completed(0, "Volume: aux0: 45875 /  70% / -9.29 dB\n")
        replies = iter([missing, missing, present, present, missing, missing])
        records = await self._run_monitor_for(6, lambda *a, **k: next(replies))
        self.assertEqual([r.levelname for r in records], ["WARNING", "INFO", "WARNING"], [r.getMessage() for r in records])
        self.assertIn("output sink is missing", records[0].getMessage())
        self.assertRegex(records[1].getMessage(), r"output sink is back after \d+ s")
        self.assertIn("output sink is missing", records[2].getMessage())
        self.assertEqual(system_volume.get_status_volume(), 70)

    async def test_other_refresh_errors_keep_reporting_with_traceback(self):
        system_volume._status_volume_cache = (37, 0.0)
        broken = _completed(1, stderr="Connection refused")
        records = await self._run_monitor_for(3, lambda *a, **k: broken)
        self.assertEqual(len(records), 3)
        for record in records:
            self.assertEqual(record.getMessage(), "Volume read monitor refresh failed")
            self.assertIsInstance(record.exc_info[1], system_volume.SystemVolumeError)

    def test_missing_sink_is_its_own_error_type_with_the_same_message(self):
        with mock.patch("audio.system_volume._selected_output_name", return_value="alsa_output.test"), \
                mock.patch("audio.system_volume.subprocess.run",
                           return_value=_completed(1, stderr="Failed to get sink information: No such entity")):
            with self.assertRaises(system_volume.SystemVolumeSinkMissingError) as raised:
                system_volume.get_output_volume()
        self.assertIsInstance(raised.exception, system_volume.SystemVolumeError)
        self.assertEqual(str(raised.exception), "Failed to get sink information: No such entity")
        with mock.patch("audio.system_volume._selected_output_name", return_value="alsa_output.test"), \
                mock.patch("audio.system_volume.subprocess.run", return_value=_completed(1, stderr="boom")):
            with self.assertRaises(system_volume.SystemVolumeError) as other:
                system_volume.get_output_volume()
        self.assertNotIsInstance(other.exception, system_volume.SystemVolumeSinkMissingError)

    async def test_stopped_monitor_does_not_publish_its_late_worker_read(self):
        system_volume._status_volume_cache = (37, 0.0)
        started = threading.Event()
        release = threading.Event()
        returned = threading.Event()

        def delayed_read(args, **kwargs):
            started.set()
            if not release.wait(2):
                raise RuntimeError("Test did not release the volume read")
            returned.set()
            return _completed(0, "Volume: 0.70\n")

        try:
            with mock.patch("audio.system_volume.subprocess.run", side_effect=delayed_read):
                system_volume.start_volume_read_monitor()
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                await system_volume.stop_volume_read_monitor()
                release.set()
                self.assertTrue(await asyncio.to_thread(returned.wait, 2))
                await asyncio.sleep(0.02)
            self.assertEqual(system_volume.get_status_volume(), 37)
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main(verbosity=2)
