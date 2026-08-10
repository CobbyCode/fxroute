#!/usr/bin/env python3
"""SPL calibration pink-noise generation must not leave a partial noise
file behind on failure, and must keep the valid cache file on success."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import spl_calibration
from fastapi import HTTPException
from spl_calibration import _SplCalibrationOperation


class FakeEasyEffects:
    def get_active_plugin_property(self, plugin, index, name):
        if name == "outputGain":
            return "0.0"
        return "true"

    def set_active_plugin_property(self, *_args, **_kwargs):
        pass


class FakeDependencies:
    def require_easyeffects_manager(self):
        return FakeEasyEffects()

    def get_easyeffects_manager(self):
        return FakeEasyEffects()

    def get_output_volume(self):
        return 50

    def set_output_volume(self, _percent):
        pass


class FakePwPlayProcess:
    def __init__(self, *_args, **_kwargs):
        pass

    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


class SplNoiseTempCleanupTests(unittest.TestCase):
    def _operation(self):
        return _SplCalibrationOperation(
            id="op-1", kind="noise", session_job_id="spl-calibration:op-1"
        )

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.gettempdir = patch("tempfile.gettempdir", return_value=self.tmpdir.name)
        self.gettempdir.start()
        self.dependencies = patch.object(
            spl_calibration, "_dependencies", return_value=FakeDependencies()
        )
        self.dependencies.start()
        self.addCleanup(self.dependencies.stop)
        self.addCleanup(self.gettempdir.stop)
        self.addCleanup(self.tmpdir.cleanup)

    def _noise_path(self):
        return Path(self.tmpdir.name) / "fxroute-spl-calibration-pink-noise-v2.wav"

    def test_ffmpeg_failure_removes_partial_noise_file(self):
        def _fake_run(_args, **_kwargs):
            self._noise_path().write_bytes(b"partial")
            return type("Result", (), {"returncode": 1, "stderr": "boom"})()

        with patch.object(spl_calibration.subprocess, "run", _fake_run):
            with self.assertRaises(HTTPException):
                spl_calibration._start_spl_calibration_noise(self._operation())
        self.assertFalse(self._noise_path().exists())

    def test_ffmpeg_timeout_removes_partial_noise_file(self):
        def _fake_run(_args, **_kwargs):
            self._noise_path().write_bytes(b"partial")
            raise TimeoutError("ffmpeg hung")

        with patch.object(spl_calibration.subprocess, "run", _fake_run):
            with self.assertRaises(TimeoutError):
                spl_calibration._start_spl_calibration_noise(self._operation())
        self.assertFalse(self._noise_path().exists())

    def test_ffmpeg_success_keeps_cached_noise_file(self):
        def _fake_run(_args, **_kwargs):
            self._noise_path().write_bytes(b"valid")
            return type("Result", (), {"returncode": 0, "stderr": ""})()

        with patch.object(spl_calibration.subprocess, "run", _fake_run), \
                patch.object(spl_calibration.subprocess, "Popen", FakePwPlayProcess):
            result = spl_calibration._start_spl_calibration_noise(self._operation())
        self.assertEqual(result["status"], "playing")
        self.assertTrue(self._noise_path().exists())

    def test_existing_cache_file_is_reused_without_regeneration(self):
        self._noise_path().write_bytes(b"valid-cache")
        generated = []

        def _fake_run(args, **_kwargs):
            generated.append(args)
            return type("Result", (), {"returncode": 0, "stderr": ""})()

        with patch.object(spl_calibration.subprocess, "run", _fake_run), \
                patch.object(spl_calibration.subprocess, "Popen", FakePwPlayProcess):
            result = spl_calibration._start_spl_calibration_noise(self._operation())
        self.assertEqual(result["status"], "playing")
        self.assertEqual(generated, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
