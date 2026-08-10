#!/usr/bin/env python3
"""ffmpeg conversions reachable from async upload endpoints must be bounded."""

import pathlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from easyeffects import EasyEffectsManager, FFMPEG_CONVERSION_TIMEOUT_SECONDS


class EasyEffectsFfmpegTimeoutTests(unittest.TestCase):
    def _manager(self):
        manager = object.__new__(EasyEffectsManager)
        return manager

    def test_wav_to_irs_passes_timeout(self):
        manager = self._manager()
        result = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "in.wav"
            source.write_bytes(b"x")
            destination = Path(tempdir) / "out.irs"
            with mock.patch("easyeffects.subprocess.run", return_value=result) as run:
                manager._convert_wav_to_irs(source, destination)
            run.assert_called_once()
            self.assertEqual(
                run.call_args.kwargs.get("timeout"), FFMPEG_CONVERSION_TIMEOUT_SECONDS
            )

    def test_wav_to_irs_timeout_raises_runtime_error(self):
        manager = self._manager()
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "in.wav"
            source.write_bytes(b"x")
            with mock.patch(
                "easyeffects.subprocess.run",
                side_effect=subprocess.TimeoutExpired(["ffmpeg"], 60),
            ):
                with self.assertRaisesRegex(RuntimeError, "timed out"):
                    manager._convert_wav_to_irs(source, Path(tempdir) / "out.irs")

    def test_ir_pair_merge_timeout_raises_runtime_error(self):
        manager = self._manager()
        with tempfile.TemporaryDirectory() as tempdir:
            left = Path(tempdir) / "left.wav"
            right = Path(tempdir) / "right.wav"
            left.write_bytes(b"x")
            right.write_bytes(b"x")
            with mock.patch(
                "easyeffects.subprocess.run",
                side_effect=subprocess.TimeoutExpired(["ffmpeg"], 60),
            ):
                with self.assertRaisesRegex(RuntimeError, "timed out"):
                    manager._merge_ir_pair_to_irs(left, right, Path(tempdir) / "out.irs")


if __name__ == "__main__":
    unittest.main(verbosity=2)
