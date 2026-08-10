#!/usr/bin/env python3
"""samplerate._run_command must be bounded against hung external commands."""

import pathlib
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import samplerate


class SamplerateCommandTimeoutTests(unittest.TestCase):
    def test_successful_command_passes_timeout(self):
        result = subprocess.CompletedProcess([], 0, stdout="42\n", stderr="")
        with mock.patch("samplerate.subprocess.run", return_value=result) as run:
            self.assertEqual(samplerate._run_command(["wpctl", "status"]), "42\n")
        run.assert_called_once()
        self.assertEqual(
            run.call_args.kwargs.get("timeout"), samplerate.COMMAND_TIMEOUT_SECONDS
        )

    def test_nonzero_exit_raises_runtime_error(self):
        result = subprocess.CompletedProcess([], 1, stdout="", stderr="boom")
        with mock.patch("samplerate.subprocess.run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                samplerate._run_command(["pactl", "info"])

    def test_hung_command_raises_timeout_error(self):
        with mock.patch(
            "samplerate.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["bluetoothctl", "info"], 5.0),
        ):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                samplerate._run_command(["bluetoothctl", "info", "AA:BB:CC"])

    def test_error_fallback_paths_still_work(self):
        # The fallback helpers parse failures into notes instead of raising.
        self.assertEqual(samplerate._parse_active_rate(""), None)
        self.assertEqual(
            samplerate._parse_default_sink(""),
            {"id": None, "name": None, "description": None},
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
