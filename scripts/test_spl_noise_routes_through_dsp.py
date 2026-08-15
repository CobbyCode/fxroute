#!/usr/bin/env python3
"""SPL calibration noise must be routed through the native DSP chain.

Regression for the native-DSP migration break where the calibration noise
was played with plain pw-play autoconnect: the default sink is the hardware
output (not the DSP ingress), so the noise bypassed headroom, active filters,
crossover and the Protection Limiter and the measurement could not respond to
Headroom changes.  The noise node must be started with autoconnect disabled
and linked explicitly to fxroute_dsp_sink playback ports.
"""

import pathlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import measurement.spl_calibration as spl_calibration
from measurement.spl_calibration import (
    _SplCalibrationOperation,
    _start_spl_calibration_noise,
    SPL_NOISE_SINK_NAME,
)


class FakeDSPManager:
    temporary_runtime_transition_callback = object()

    def load_global_extras(self):
        return {
            "autogain": {"enabled": True, "params": {}},
            "loudness": {"enabled": True, "params": {}},
        }

    def apply_temporary_effects_runtime(self, previous, candidate):
        pass


class FakeDependencies:
    def require_dsp_manager(self):
        return FakeDSPManager()

    def get_dsp_manager(self):
        return FakeDSPManager()

    def get_output_volume(self):
        return 50

    def set_output_volume(self, _percent):
        pass

    def get_measurement_store(self):
        return None

    def get_measurement_session(self):
        return None

    def read_measurement_settings(self):
        return {}

    def measurement_entry_preflight(self, rate):
        pass

    def run_dsp_mutation(self, func):
        return func()


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


class FakeRunResult:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def operation(operation_id="1234abcd"):
    return _SplCalibrationOperation(
        id=operation_id, kind="manual-noise",
        session_job_id=f"spl-calibration:{operation_id}",
    )


def run_recorder(args, noise_dir):
    calls = {"args": None}

    def popen(*popen_args, **_kwargs):
        calls["args"] = popen_args
        return FakePwPlayProcess()

    noise_node = "fxroute-spl-noise-1234abcd"

    def run_command(command, **kwargs):
        if command[:2] == ["pw-link", "-o"]:
            return FakeRunResult(
                stdout=f"{noise_node}:output_FL\n{noise_node}:output_FR\n"
            )
        if command and command[0] == "pw-link" and command[1] != "-o":
            return FakeRunResult(returncode=0)
        return FakeRunResult()

    return popen, run_command, calls


class SplNoiseRoutingTests(unittest.TestCase):
    def setUp(self):
        spl_calibration.configure_runtime(spl_calibration.SplCalibrationDependencies(
            get_measurement_store=FakeDependencies().get_measurement_store,
            get_measurement_session=FakeDependencies().get_measurement_session,
            get_dsp_manager=FakeDependencies().get_dsp_manager,
            require_dsp_manager=FakeDependencies().require_dsp_manager,
            get_output_volume=FakeDependencies().get_output_volume,
            set_output_volume=FakeDependencies().set_output_volume,
            read_measurement_settings=FakeDependencies().read_measurement_settings,
            measurement_entry_preflight=FakeDependencies().measurement_entry_preflight,
            run_dsp_mutation=FakeDependencies().run_dsp_mutation,
        ))
        self.tmpdir = tempfile.TemporaryDirectory()
        self.noise_path = Path(self.tmpdir.name) / "fxroute-spl-calibration-pink-noise-v2.wav"
        self.noise_path.write_bytes(b"RIFF")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_noise_is_linked_into_the_dsp_sink(self):
        popen, run_command, calls = run_recorder([], self.tmpdir.name)
        op = operation("1234abcd")
        with patch("tempfile.gettempdir", return_value=self.tmpdir.name), \
             patch.object(spl_calibration.subprocess, "Popen", side_effect=popen), \
             patch.object(spl_calibration.subprocess, "run", side_effect=run_command):
            result = _start_spl_calibration_noise(op)

        self.assertEqual(result["status"], "playing")
        command = calls["args"][0]
        self.assertEqual(command[:5], [
            "pw-play", "-P", "node.autoconnect=false",
            "-P", "node.name=fxroute-spl-noise-1234abcd",
        ])
        self.assertIn("--target", command)
        self.assertEqual(command[command.index("--target") + 1], "0")
        self.assertIn("--volume=1.0", command)
        self.assertEqual(command[-1], str(self.noise_path))
        expected_links = [
            ("fxroute-spl-noise-1234abcd:output_FL", f"{SPL_NOISE_SINK_NAME}:playback_FL"),
            ("fxroute-spl-noise-1234abcd:output_FR", f"{SPL_NOISE_SINK_NAME}:playback_FR"),
        ]
        self.assertEqual(op.noise_links, expected_links)

    def test_missing_noise_ports_terminate_process_and_raise(self):
        op = operation()
        terminated = []

        class Process(FakePwPlayProcess):
            def terminate(self):
                terminated.append(True)

        with patch("tempfile.gettempdir", return_value=self.tmpdir.name), \
             patch.object(spl_calibration.subprocess, "Popen", return_value=Process()), \
             patch.object(
                 spl_calibration.subprocess, "run",
                 return_value=FakeRunResult(stdout=""),
             ):
            with self.assertRaises(RuntimeError):
                _start_spl_calibration_noise(op)
        self.assertEqual(terminated, [True])

    def test_link_failure_terminates_process_and_raises(self):
        op = operation()
        terminated = []

        class Process(FakePwPlayProcess):
            def terminate(self):
                terminated.append(True)

        noise_node = "fxroute-spl-noise-" + op.id[:8]

        def run_command(command, **kwargs):
            if command[:2] == ["pw-link", "-o"]:
                return FakeRunResult(
                    stdout=f"{noise_node}:output_FL\n{noise_node}:output_FR\n"
                )
            if command and command[0] == "pw-link" and command[1] != "-o":
                return FakeRunResult(returncode=1, stderr="Permission denied")
            return FakeRunResult()

        with patch("tempfile.gettempdir", return_value=self.tmpdir.name), \
             patch.object(spl_calibration.subprocess, "Popen", return_value=Process()), \
             patch.object(spl_calibration.subprocess, "run", side_effect=run_command):
            with self.assertRaises(RuntimeError) as raised:
                _start_spl_calibration_noise(op)
        self.assertIn("Could not link SPL calibration noise", str(raised.exception))
        self.assertEqual(terminated, [True])
        self.assertIsNone(op.noise_links)


class SplNoiseCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        spl_calibration.configure_runtime(spl_calibration.SplCalibrationDependencies(
            get_measurement_store=FakeDependencies().get_measurement_store,
            get_measurement_session=FakeDependencies().get_measurement_session,
            get_dsp_manager=FakeDependencies().get_dsp_manager,
            require_dsp_manager=FakeDependencies().require_dsp_manager,
            get_output_volume=FakeDependencies().get_output_volume,
            set_output_volume=FakeDependencies().set_output_volume,
            read_measurement_settings=FakeDependencies().read_measurement_settings,
            measurement_entry_preflight=FakeDependencies().measurement_entry_preflight,
            run_dsp_mutation=FakeDependencies().run_dsp_mutation,
        ))

    async def test_cleanup_removes_noise_links_best_effort(self):
        import asyncio
        from unittest.mock import patch

        op = operation()
        op.noise_links = [
            ("fxroute-spl-noise-1234abcd:output_FL", f"{SPL_NOISE_SINK_NAME}:playback_FL"),
            ("fxroute-spl-noise-1234abcd:output_FR", f"{SPL_NOISE_SINK_NAME}:playback_FR"),
        ]
        removed = []

        def run_command(command, **kwargs):
            if command[:2] == ["pw-link", "-d"]:
                removed.append(tuple(command[2:]))
            return FakeRunResult(returncode=0)

        with patch.object(spl_calibration.subprocess, "run", side_effect=run_command):
            await spl_calibration._cleanup_operation(op)
        self.assertEqual(len(removed), 2)
        self.assertIsNone(op.noise_links)

    async def test_cleanup_tolerates_missing_links(self):
        import asyncio
        from unittest.mock import patch

        op = operation()
        op.noise_links = [
            ("fxroute-spl-noise-1234abcd:output_FL", f"{SPL_NOISE_SINK_NAME}:playback_FL"),
        ]

        def run_command(command, **kwargs):
            return FakeRunResult(returncode=1, stderr="link does not exist")

        with patch.object(spl_calibration.subprocess, "run", side_effect=run_command):
            await spl_calibration._cleanup_operation(op)
        self.assertIsNone(op.noise_links)


if __name__ == "__main__":
    unittest.main()
