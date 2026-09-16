#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Generated PEQ/FIR commits are gated on their source measurement's target.

A correction derived from a measurement may only be committed into the area
that measurement was taken in, through the processing it actually ran through.
The gate lives in the injected ``verify_measurement_commit`` hook; the routes
must consult it before anything is created or assigned, and must keep the
pre-gate path for a commit that names no source measurement or no bank.
"""

import dataclasses
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

import main
import dsp.api as dsp_api

OUTPUT_STATE = {
    "active_mode": "stereo",
    "modes": {"stereo": {"selected_bank": "main_l", "banks": {"global": {}, "main_l": {}}}},
}
BINDING_FIELDS = {"bank_mode": "stereo", "bank_id": "main_l", "expected_revision": 4}
EXTRAS = {"limiter": {"enabled": False}, "headroom": {"enabled": False}}
PEQ = {"enabled": True, "params": {"channelMode": "dual", "leftBands": [], "rightBands": []}}


class FakeOutputService:
    def load(self):
        return OUTPUT_STATE


class FakeUploadFile:
    def __init__(self, filename="sweep.wav"):
        self.filename = filename
        self._reads = 0

    async def read(self, size=-1):
        self._reads += 1
        return b"RIFF0000" if self._reads == 1 else b""


class FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class FakeDspManager:
    def __init__(self):
        self.calls = []

    def load_global_extras(self):
        return {}

    def get_status(self):
        return {"status": "ok", "active_preset": None}

    def create_peq_preset(self, name, peq, extras=None):
        self.calls.append(("create-peq", name))
        return {"name": name, "chain": []}

    def create_convolver_preset(self, name, ir_filename, extras=None):
        self.calls.append(("create-convolver", name))
        return {"name": name, "chain": []}

    def create_convolver_preset_with_upload(self, name, path, filename, extras=None):
        self.calls.append(("create-with-ir", name))
        return {"ir": {"name": filename}, "preset": {"name": name, "chain": []}}

    def create_convolver_preset_with_dual_uploads(self, name, *args, **kwargs):
        self.calls.append(("import-filter-dual", name))
        return {"preset": {"name": name, "chain": []}}


async def _raise_conflict(_message):
    raise HTTPException(status_code=409, detail="conflict")


class MeasurementCommitGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = FakeDspManager()
        self.verified = []
        self.assignments = []
        self.reject = ""

        def verify(measurement_id, binding):
            self.verified.append((measurement_id, dict(binding)))
            if self.reject:
                raise ValueError(self.reject)

        def assign(*, created_name, binding):
            self.assignments.append((created_name, dict(binding)))
            return {"assigned": True, "mode": binding["mode"], "bank_id": binding["bank_id"],
                    "revision": binding["expected_revision"]}

        patchers = [
            mock.patch.object(main, "_require_dsp_manager", return_value=self.manager),
            mock.patch.object(main, "dsp_manager", self.manager),
            mock.patch.object(dsp_api, "_require_output_state_service",
                              return_value=FakeOutputService()),
            mock.patch.object(dsp_api, "_finish_dsp_preset_mutation",
                              mock.AsyncMock(return_value={"status": "ok", "active_preset": None})),
            mock.patch.object(dsp_api, "_assign_created_preset_to_bank",
                              mock.AsyncMock(side_effect=assign)),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        deps = dataclasses.replace(main._make_dsp_api_deps(), verify_measurement_commit=verify)
        self.addCleanup(dsp_api.configure_dsp_api, main._make_dsp_api_deps())
        dsp_api.configure_dsp_api(deps)

    def peq_request(self, **overrides):
        payload = {"presetName": "Room correction", "peq": PEQ, **BINDING_FIELDS}
        payload.update(overrides)
        return FakeRequest(payload)

    async def test_peq_commit_is_rejected_when_the_measurement_moved(self):
        self.reject = "Measurement target no longer matches live processing: processing 'a' != 'b'"
        with self.assertRaises(HTTPException) as caught:
            await dsp_api.create_peq_preset(self.peq_request(source_measurement_id="measure-1"))

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail["code"], "measurement-target-mismatch")
        self.assertEqual(caught.exception.detail["source_measurement_id"], "measure-1")
        self.assertIn("live processing", caught.exception.detail["message"])
        # Nothing may be created or assigned before the gate passes.
        self.assertEqual(self.manager.calls, [])
        self.assertEqual(self.assignments, [])
        self.assertEqual(self.verified, [("measure-1", {"mode": "stereo", "bank_id": "main_l",
                                                        "expected_revision": 4})])

    async def test_peq_commit_is_created_and_assigned_when_the_target_matches(self):
        response = await dsp_api.create_peq_preset(self.peq_request(source_measurement_id="measure-1"))

        self.assertEqual(self.manager.calls, [("create-peq", "Room correction")])
        self.assertEqual(self.assignments, [("Room correction", {"mode": "stereo",
                                                                "bank_id": "main_l",
                                                                "expected_revision": 4})])
        self.assertTrue(response["bank"]["assigned"])
        self.assertEqual(self.verified, [("measure-1", {"mode": "stereo", "bank_id": "main_l",
                                                        "expected_revision": 4})])

    async def test_peq_commit_without_a_source_measurement_keeps_the_legacy_path(self):
        response = await dsp_api.create_peq_preset(self.peq_request())

        self.assertEqual(self.verified, [])
        self.assertEqual(self.manager.calls, [("create-peq", "Room correction")])
        self.assertTrue(response["bank"]["assigned"])

    async def test_commit_without_a_bank_binding_is_not_gated(self):
        # A preset that is not committed into an area has no target to match.
        await dsp_api.create_peq_preset(FakeRequest({
            "presetName": "Draft", "peq": PEQ, "source_measurement_id": "measure-1"}))

        self.assertEqual(self.verified, [])
        self.assertEqual(self.manager.calls, [("create-peq", "Draft")])
        self.assertEqual(self.assignments, [])

    async def test_create_with_ir_commit_is_gated(self):
        self.reject = "Measurement was captured for crossover area 'left_low', but this commit targets stereo area 'main_l'"
        with self.assertRaises(HTTPException) as caught:
            await dsp_api.create_convolver_preset_with_ir(
                preset_name="Correction IR", file=FakeUploadFile(),
                source_measurement_id="measure-2", **BINDING_FIELDS)

        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("crossover area", caught.exception.detail["message"])
        self.assertEqual(self.manager.calls, [])
        self.assertEqual(self.assignments, [])
        self.assertEqual(self.verified[0][0], "measure-2")

    async def test_create_convolver_commit_is_gated(self):
        self.reject = "Measurement target no longer matches live processing: output device 'a' != 'b'"
        with self.assertRaises(HTTPException) as caught:
            await dsp_api.create_convolver_preset(
                preset_name="Correction IR", ir_filename="sweep.irs",
                source_measurement_id="measure-3", **BINDING_FIELDS)

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.verified[0][0], "measure-3")
        self.assertEqual(self.manager.calls, [])
        self.assertEqual(self.assignments, [])

    async def test_dual_filter_commit_is_gated(self):
        self.reject = "Measurement target no longer matches live processing: sample rate 44100 != 48000"
        with self.assertRaises(HTTPException) as caught:
            await dsp_api.import_dual_filter_preset(
                preset_name="Correction IR", left_file=FakeUploadFile("left.wav"),
                right_file=FakeUploadFile("right.wav"),
                source_measurement_id="measure-4", **BINDING_FIELDS)

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.verified[0][0], "measure-4")
        self.assertEqual(self.manager.calls, [])

    async def test_missing_source_measurement_is_reported_as_a_conflict(self):
        # A deleted measurement is a conflict too: the correction can no longer
        # be tied to the processing it was derived from.
        self.reject = "Source measurement measure-404 is no longer available"
        with self.assertRaises(HTTPException) as caught:
            await dsp_api.create_peq_preset(self.peq_request(source_measurement_id="measure-404"))

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail["code"], "measurement-target-mismatch")
        self.assertEqual(self.manager.calls, [])


def frozen_target(*, bank_id="main_l", fingerprint="fp-live", mode="stereo",
                  device_key="device-a", sample_rate_hz=48_000,
                  measured_roles=("main_l",)):
    return {
        "schema": "fxroute.measurement-target",
        "version": 1,
        "mode": mode,
        "device_key": device_key,
        "bank_id": bank_id,
        "preset": "Neutral",
        "revision": 2,
        "processing_fingerprint": fingerprint,
        "sample_rate_hz": sample_rate_hz,
        "channels": 2,
        "roles": ["main_l", "main_r"],
        "measured_roles": list(measured_roles),
        "reference_tap": "fxroute_dsp_sink.monitor",
    }


class FakeMeasurementStore:
    def __init__(self, measurements):
        self._measurements = measurements

    def get_measurement(self, measurement_id):
        if measurement_id not in self._measurements:
            raise KeyError(measurement_id)
        return self._measurements[measurement_id]

    def _resolve_measurement_sample_rate(self):
        return 48_000


class MainMeasurementCommitVerifierTests(unittest.TestCase):
    """main.py's hook compares the stored target with a fresh live freeze."""

    def setUp(self):
        self.stored = frozen_target()
        self.live = frozen_target()
        self.frozen_banks = []
        self.store = FakeMeasurementStore({"measure-1": {"measurement_target": self.stored}})

        def freeze(bank_id, sample_rate_hz):
            self.frozen_banks.append((bank_id, sample_rate_hz))
            return dict(self.live)

        patchers = [
            mock.patch.object(main, "measurement_store", self.store),
            mock.patch.object(main, "_freeze_measurement_target", side_effect=freeze),
            mock.patch.object(main, "_live_measurement_sample_rate", return_value=48_000),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def verify(self, **binding):
        main._verify_measurement_commit(
            "measure-1", {"mode": "stereo", "bank_id": "main_l", "expected_revision": 0, **binding})

    def test_matching_target_passes_and_freezes_the_measurement_area(self):
        self.verify()
        self.assertEqual(self.frozen_banks, [("main_l", 48_000)])

    def test_processing_change_is_rejected(self):
        self.live["processing_fingerprint"] = "fp-changed"
        with self.assertRaisesRegex(ValueError, "no longer matches live processing"):
            self.verify()

    def test_other_area_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "captured for stereo area 'main_l'"):
            self.verify(bank_id="main_r")
        # The live freeze stays anchored on the measured area, never on the
        # area the caller asked to commit into.
        self.assertEqual(self.frozen_banks, [("main_l", 48_000)])

    def test_device_or_rate_change_is_rejected(self):
        self.live["device_key"] = "device-b"
        self.live["sample_rate_hz"] = 44_100
        with self.assertRaisesRegex(ValueError, "output device.*sample rate"):
            self.verify()

    def test_missing_measurement_is_reported(self):
        with self.assertRaisesRegex(ValueError, "measure-404 is no longer available"):
            main._verify_measurement_commit(
                "measure-404", {"mode": "stereo", "bank_id": "main_l", "expected_revision": 0})

    def test_legacy_measurement_is_accepted_without_a_freeze(self):
        self.store._measurements["measure-1"] = {"id": "measure-1"}
        self.verify()
        self.assertEqual(self.frozen_banks, [])

    def test_store_unavailable_is_reported(self):
        with mock.patch.object(main, "measurement_store", None):
            with self.assertRaisesRegex(ValueError, "Measurement store is not available"):
                self.verify()


if __name__ == "__main__":
    unittest.main()
