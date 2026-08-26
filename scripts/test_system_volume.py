#!/usr/bin/env python3
"""system_volume: live reads stay live; only the status path is cached."""

import asyncio
import pathlib
import subprocess
import sys
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
