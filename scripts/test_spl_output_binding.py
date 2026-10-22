#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""An SPL measurement belongs to the output it started on; the panel reads that output's calibration."""

import asyncio
import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import measurement.spl_calibration as spl_calibration
import test_spl_pw_record_capture as support
from dsp.manager import DSPManager
from fastapi import HTTPException

RATE = 48_000
OUTPUT_A = {"id": "output-a", "label": "Output A"}
OUTPUT_B = {"id": "output-b", "label": "Output B"}


def record(identity, adjustment):
    return {"outputProfileId": identity, "requiredAdjustmentDb": adjustment, "measuredSplDb": 83.0 - adjustment,
            "createdAt": "2026-10-01T10:00:00+00:00"}


class OutputBindingTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.calibration = support.calibration_file(self.directory)
        self.manager = DSPManager(home=self.directory / "home")
        extras = self.manager.load_global_extras()
        extras["loudness"]["params"]["calibrationProfiles"] = {
            "output-a": record("output-a", 4.0), "output-b": record("output-b", -6.0)}
        self.manager.save_global_extras(extras)
        original = spl_calibration._runtime.dependencies
        self.addCleanup(setattr, spl_calibration._runtime, "dependencies", original)
        self.addCleanup(setattr, spl_calibration._runtime, "operation", None)
        deps = support.FakeDependencies(support.FakeMeasurementStore(self.calibration))
        deps.require_dsp_manager = lambda: self.manager
        spl_calibration.configure_runtime(spl_calibration.SplCalibrationDependencies(
            get_measurement_store=deps.get_measurement_store, get_measurement_session=deps.get_measurement_session,
            get_dsp_manager=deps.get_dsp_manager, require_dsp_manager=deps.require_dsp_manager,
            get_output_volume=deps.get_output_volume, set_output_volume=deps.set_output_volume,
            read_measurement_settings=deps.read_measurement_settings,
            measurement_entry_preflight=deps.measurement_entry_preflight, run_dsp_mutation=deps.run_dsp_mutation,
            external_dsp_mutation_scope=deps.external_dsp_mutation_scope,
            canonical_volume_write_lock=deps.canonical_volume_write_lock))
        spl_calibration._runtime.operation = None
        self.noise_started = []
        self.recorders = []

    def steady_capture(self, path):
        mono = (0.05 * np.sqrt(2.0) * np.sin(2.0 * np.pi * 1000.0 * np.arange(6 * RATE) / RATE) * 32767).astype(np.int16)
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(2)
            wav.setsampwidth(2)
            wav.setframerate(RATE)
            wav.writeframes(np.stack([mono, mono], axis=1).tobytes())

    def measure(self, profiles):
        """The automatic measurement with the output identity answering from `profiles`, one per lookup."""
        def popen(argv, **_kwargs):
            self.steady_capture(Path(argv[-1]))
            recorder = support.LiveRecorder(argv)
            self.recorders.append(recorder)
            return recorder

        sequence = iter(profiles)
        with mock.patch("tempfile.gettempdir", return_value=str(self.directory)), \
                mock.patch.object(spl_calibration, "_spl_output_profile", side_effect=lambda: next(sequence)), \
                mock.patch.object(spl_calibration.subprocess, "Popen", side_effect=popen), \
                mock.patch.object(spl_calibration.subprocess, "run", side_effect=support.pactl_result), \
                mock.patch.object(spl_calibration, "_start_spl_calibration_noise",
                                  side_effect=lambda operation: self.noise_started.append(operation) or {"status": "playing"}):
            return asyncio.run(spl_calibration.measure_spl_automatically())

    def test_the_result_names_the_output_the_measurement_ran_on(self):
        result = self.measure([OUTPUT_A, OUTPUT_A])
        self.assertEqual(result["output_profile"], OUTPUT_A)
        self.assertEqual(len(self.noise_started), 1)

    def test_an_output_change_during_the_run_rejects_the_run(self):
        with self.assertRaises(HTTPException) as caught:
            self.measure([OUTPUT_A, OUTPUT_B])
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("The output changed during the measurement. Repeat the measurement.", caught.exception.detail)
        self.assertIsNone(spl_calibration._runtime.operation)
        self.assertEqual(list(self.directory.glob("fxroute-spl-*.wav")), [])

    def test_an_output_that_vanishes_during_the_run_rejects_the_run(self):
        with self.assertRaises(HTTPException) as caught:
            self.measure([OUTPUT_A, {"id": "", "label": ""}])
        self.assertIn("The output changed during the measurement", caught.exception.detail)

    def test_without_an_output_the_run_ends_before_any_recorder_or_noise_starts(self):
        with self.assertRaises(HTTPException) as caught:
            self.measure([{"id": "", "label": ""}])
        self.assertEqual((caught.exception.status_code, caught.exception.detail), (409, "No audio output device is selected"))
        self.assertEqual((self.recorders, self.noise_started), ([], []))
        self.assertIsNone(spl_calibration._runtime.operation)

    def test_the_manual_noise_start_names_its_output(self):
        async def run():
            class Request:
                async def json(self):
                    return {"enabled": True}

            async def register(operation):
                operation.registration_attempted = True

            def start(operation):
                operation.noise_process = support.LiveRecorder(["pw-play"])
                return {"status": "playing"}

            with mock.patch.object(spl_calibration, "_spl_output_profile", return_value=OUTPUT_B), \
                    mock.patch.object(spl_calibration, "_register_operation", side_effect=register), \
                    mock.patch.object(spl_calibration, "_start_spl_calibration_noise", side_effect=start), \
                    mock.patch.object(spl_calibration.subprocess, "run", side_effect=support.pactl_result):
                try:
                    return await spl_calibration.set_spl_calibration_noise(Request())
                finally:
                    operation = spl_calibration._runtime.operation
                    if operation is not None:
                        operation.cancel_requested = True
                        operation.noise_process = None
                        await spl_calibration._cleanup_operation_shielded(operation)

        result = asyncio.run(run())
        self.assertEqual((result["status"], result["output_profile"]), ("playing", OUTPUT_B))

    def test_the_manual_noise_start_refuses_without_an_output(self):
        class Request:
            async def json(self):
                return {"enabled": True}

        with mock.patch.object(spl_calibration, "_spl_output_profile", return_value={"id": "", "label": ""}), \
                mock.patch.object(spl_calibration, "_start_spl_calibration_noise") as start:
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(spl_calibration.set_spl_calibration_noise(Request()))
        self.assertEqual(caught.exception.status_code, 409)
        start.assert_not_called()
        self.assertIsNone(spl_calibration._runtime.operation)

    def test_the_panel_reads_the_calibration_of_the_output_it_shows(self):
        expected = {OUTPUT_A["id"]: 4.0, OUTPUT_B["id"]: -6.0, "output-c": None}
        for identity, adjustment in expected.items():
            with self.subTest(output=identity):
                profile = {"id": identity, "label": identity}
                with mock.patch.object(spl_calibration, "_spl_output_profile", return_value=profile), \
                        mock.patch.object(spl_calibration, "_spl_auto_capability", return_value={"available": False}), \
                        mock.patch.object(spl_calibration, "_unrestored_audio_status", return_value=None):
                    status = asyncio.run(spl_calibration.get_spl_calibration())
                self.assertEqual(status["output_profile"], profile)
                calibration = status["loudness"]["params"]["calibration"]
                self.assertEqual(calibration.get("requiredAdjustmentDb"), adjustment)
                self.assertEqual(sorted(status["loudness"]["params"]["calibrationProfiles"]), ["output-a", "output-b"])

    def test_a_legacy_calibration_without_an_output_is_not_shown_for_any_output(self):
        stored = json.loads(self.manager.global_extras_file.read_text())
        stored["loudness"]["params"]["calibration"] = {"requiredAdjustmentDb": 9.0, "measuredSplDb": 74.0}
        self.manager.global_extras_file.write_text(json.dumps(stored))
        shown = {}
        for profile in (OUTPUT_A, {"id": "output-c", "label": "C"}):
            with mock.patch.object(spl_calibration, "_spl_output_profile", return_value=profile), \
                    mock.patch.object(spl_calibration, "_spl_auto_capability", return_value={"available": False}), \
                    mock.patch.object(spl_calibration, "_unrestored_audio_status", return_value=None):
                params = asyncio.run(spl_calibration.get_spl_calibration())["loudness"]["params"]
            shown[profile["id"]] = params["calibration"].get("requiredAdjustmentDb")
            self.assertEqual(params["legacyCalibration"]["requiredAdjustmentDb"], 9.0)
        self.assertEqual(shown, {"output-a": 4.0, "output-c": None})


if __name__ == "__main__":
    unittest.main()
