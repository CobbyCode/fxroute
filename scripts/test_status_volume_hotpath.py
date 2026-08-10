#!/usr/bin/env python3
"""The playback payload volume path must be non-blocking and cache-only."""

import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import main
import system_volume


class PlaybackPayloadVolumePathTests(unittest.TestCase):
    def setUp(self):
        self.original_manager = main.easyeffects_manager
        main.easyeffects_manager = None
        system_volume._status_volume_cache = None

    def tearDown(self):
        main.easyeffects_manager = self.original_manager
        system_volume._status_volume_cache = None

    def test_empty_cache_never_spawns_wpctl_in_payload_path(self):
        with mock.patch("system_volume.subprocess.run") as run:
            self.assertEqual(main.get_output_volume_safe(), 100)
            run.assert_not_called()

    def test_payload_path_uses_only_the_status_value(self):
        with mock.patch.object(main, "get_status_volume", return_value=42) as status, mock.patch.object(
            main, "get_output_volume", side_effect=AssertionError("live read must not run")
        ):
            self.assertEqual(main.get_output_volume_safe(), 42)
            status.assert_called_once()

    def test_stale_status_value_keeps_payload_non_blocking(self):
        system_volume._publish_status_volume(37, 1.0)
        with mock.patch("system_volume.subprocess.run") as run:
            self.assertEqual(main.get_output_volume_safe(), 37)
            run.assert_not_called()

    def test_default_is_used_when_monitor_never_succeeded(self):
        with mock.patch.object(main, "get_status_volume", return_value=100) as status:
            self.assertEqual(main.get_output_volume_safe(default=100), 100)
            status.assert_called_once_with(100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
