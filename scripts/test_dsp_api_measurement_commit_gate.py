#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Generated PEQ/FIR commits are gated on their source measurements' targets.

A correction derived from a measurement may only be committed into the bank,
speaker group and channel shape that measurement was taken for.  The gate
lives in the injected ``verify_measurement_commit`` hook; the routes must
consult it before anything is created or assigned, and must leave a commit
that names no source measurement or no bank unchecked.
"""

import dataclasses
import json
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

import main
import dsp.api as dsp_api

OUTPUT_STATE = {
    "active_mode": "stereo", "revision": 4,
    "modes": {"stereo": {"selected_bank": "main_l", "banks": {"global": {}, "main_l": {}}}},
}
SURROUND_STATE = {
    "active_mode": "surround", "revision": 4,
    "modes": {"surround": {"selected_bank": "global",
                           "banks": {"global": {"preset": "Front set"}, "main_l": {}, "main_r": {},
                                     "rear_l": {}, "rear_r": {}}}},
}
BINDING_FIELDS = {"bank_mode": "stereo", "bank_id": "main_l", "expected_revision": 4}
EXTRAS = {"limiter": {"enabled": False}, "headroom": {"enabled": False}}
PEQ = {"enabled": True, "params": {"channelMode": "dual", "leftBands": [], "rightBands": []}}
# Opaque to the routes: only main.py's hook reads the targets.
SOURCES = [{"schema": "fxroute.measurement-target", "bank_id": "main_l"}, None]
FORM_SOURCES = json.dumps(SOURCES)


class FakeOutputService:
    """The service contract the routes' binding pre-flight reads."""

    def load(self):
        return OUTPUT_STATE

    def require_writable(self):
        return None


async def routed_roles(state, mode):
    """The fixture states route every role they store."""
    return [role for role in state["modes"][mode]["banks"] if role != "global"]


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
        self.groups = []
        self.modes = []
        self.bases = []

    def load_global_extras(self):
        return {}

    def normalize_effects_extras(self, extras):
        return extras

    def get_status(self):
        return {"status": "ok", "active_preset": None}

    def record(self, kind, name, bank, group, mode, base_preset):
        self.calls.append((kind, name, bank))
        self.groups.append(group)
        self.modes.append(mode)
        self.bases.append(base_preset)

    def create_peq_preset(self, name, peq, extras=None, bank=None, group=None, mode=None, base_preset=None):
        self.record("create-peq", name, bank, group, mode, base_preset)
        return {"name": name, "chain": []}

    def create_convolver_preset(self, name, ir_filename, extras=None, bank=None, group=None, mode=None,
                                base_preset=None):
        self.record("create-convolver", name, bank, group, mode, base_preset)
        return {"name": name, "chain": []}

    def create_convolver_preset_with_upload(self, name, path, filename, extras=None, bank=None, group=None,
                                            mode=None, base_preset=None):
        self.record("create-with-ir", name, bank, group, mode, base_preset)
        return {"ir": {"name": filename}, "preset": {"name": name, "chain": []}}

    def create_convolver_preset_with_dual_uploads(self, name, *args, bank=None, group=None, mode=None,
                                                  base_preset=None, **kwargs):
        self.record("import-filter-dual", name, bank, group, mode, base_preset)
        return {"preset": {"name": name, "chain": []}}


async def _raise_conflict(_message):
    raise HTTPException(status_code=409, detail="conflict")


class MeasurementCommitGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = FakeDspManager()
        self.verified = []
        self.assignments = []
        self.reject = ""

        def verify(targets, binding):
            self.verified.append((targets, dict(binding)))
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
        deps = dataclasses.replace(main._make_dsp_api_deps(), verify_measurement_commit=verify,
                                   routed_roles=routed_roles)
        self.addCleanup(dsp_api.configure_dsp_api, main._make_dsp_api_deps())
        dsp_api.configure_dsp_api(deps)

    def peq_request(self, **overrides):
        payload = {"presetName": "Room correction", "peq": PEQ, **BINDING_FIELDS}
        payload.update(overrides)
        return FakeRequest(payload)

    async def test_peq_commit_is_rejected_when_the_measurement_does_not_fit(self):
        self.reject = "Measurement was captured for stereo area 'main_r', but this commit targets stereo area 'main_l'"
        with self.assertRaises(HTTPException) as caught:
            await dsp_api.create_peq_preset(self.peq_request(source_measurement_targets=SOURCES))

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail, {
            "code": "measurement-target-mismatch", "message": self.reject, "assigned": False})
        # Nothing may be created or assigned before the gate passes.
        self.assertEqual(self.manager.calls, [])
        self.assertEqual(self.assignments, [])
        self.assertEqual(self.verified, [(SOURCES, {"mode": "stereo", "bank_id": "main_l",
                                                    "expected_revision": 4})])

    async def test_peq_commit_is_created_and_assigned_when_the_target_matches(self):
        response = await dsp_api.create_peq_preset(self.peq_request(source_measurement_targets=SOURCES))

        self.assertEqual(self.manager.calls, [("create-peq", "Room correction", "main_l")])
        self.assertEqual(self.assignments, [("Room correction", {"mode": "stereo",
                                                                "bank_id": "main_l",
                                                                "expected_revision": 4})])
        self.assertTrue(response["bank"]["assigned"])
        self.assertEqual(self.verified, [(SOURCES, {"mode": "stereo", "bank_id": "main_l",
                                                    "expected_revision": 4})])

    async def test_commit_without_source_measurements_is_not_gated(self):
        # Imports and manual presets name no source; an empty list is the same.
        for sources in ({}, {"source_measurement_targets": []}, {"source_measurement_targets": None}):
            with self.subTest(sources=sources):
                self.manager.calls.clear()
                response = await dsp_api.create_peq_preset(self.peq_request(**sources))
                self.assertEqual(self.verified, [])
                self.assertEqual(self.manager.calls, [("create-peq", "Room correction", "main_l")])
                self.assertTrue(response["bank"]["assigned"])
        await dsp_api.create_convolver_preset(preset_name="Manual", ir_filename="x.irs",
                                              source_measurement_targets="", **BINDING_FIELDS)
        self.assertEqual(self.verified, [])

    async def test_commit_without_a_bank_binding_is_not_gated(self):
        # A preset that is not committed into an area has no target to match.
        await dsp_api.create_peq_preset(FakeRequest({
            "presetName": "Draft", "peq": PEQ, "source_measurement_targets": SOURCES}))

        self.assertEqual(self.verified, [])
        self.assertEqual(self.manager.calls, [("create-peq", "Draft", None)])
        self.assertEqual(self.assignments, [])

    async def test_malformed_source_targets_are_bad_input(self):
        for sources in ({"schema": "x"}, "not json", "{}", 7):
            with self.subTest(sources=sources), self.assertRaises(HTTPException) as caught:
                await dsp_api.create_peq_preset(self.peq_request(source_measurement_targets=sources))
            self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(self.verified, [])
        self.assertEqual(self.manager.calls, [])

    async def test_create_with_ir_commit_is_gated(self):
        self.reject = "Measurement was captured for crossover area 'left_low', but this commit targets stereo area 'main_l'"
        with self.assertRaises(HTTPException) as caught:
            await dsp_api.create_convolver_preset_with_ir(
                preset_name="Correction IR", file=FakeUploadFile(),
                source_measurement_targets=FORM_SOURCES, **BINDING_FIELDS)

        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("crossover area", caught.exception.detail["message"])
        self.assertEqual(self.manager.calls, [])
        self.assertEqual(self.assignments, [])
        # Form posts carry the list JSON-encoded.
        self.assertEqual(self.verified[0][0], SOURCES)

    async def test_create_convolver_commit_is_gated(self):
        self.reject = "Source measurement carries no frozen target; measure again"
        with self.assertRaises(HTTPException) as caught:
            await dsp_api.create_convolver_preset(
                preset_name="Correction IR", ir_filename="sweep.irs",
                source_measurement_targets=FORM_SOURCES, **BINDING_FIELDS)

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.verified[0][0], SOURCES)
        self.assertEqual(self.manager.calls, [])
        self.assertEqual(self.assignments, [])

    async def test_dual_filter_commit_is_gated(self):
        self.reject = "Measurement covers ['main_l'], but stereo area 'main_l' plays ['main_l', 'main_r']"
        with self.assertRaises(HTTPException) as caught:
            await dsp_api.import_dual_filter_preset(
                preset_name="Correction IR", left_file=FakeUploadFile("left.wav"),
                right_file=FakeUploadFile("right.wav"),
                source_measurement_targets=FORM_SOURCES, **BINDING_FIELDS)

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.verified[0][0], SOURCES)
        self.assertEqual(self.manager.calls, [])

    def surround_peq_request(self, **overrides):
        return self.peq_request(bank_mode="surround", bank_id="global", **overrides)

    async def test_surround_global_commit_carries_its_speaker_group(self):
        with mock.patch.object(FakeOutputService, "load", return_value=SURROUND_STATE):
            response = await dsp_api.create_peq_preset(
                self.surround_peq_request(bank_group="rear", source_measurement_targets=SOURCES))

        self.assertEqual(self.manager.calls, [("create-peq", "Room correction", "global")])
        self.assertEqual(self.manager.groups, ["rear"])
        binding = {"mode": "surround", "bank_id": "global", "expected_revision": 4, "bank_group": "rear"}
        self.assertEqual(self.verified, [(SOURCES, binding)])
        self.assertEqual(self.assignments, [("Room correction", binding)])
        self.assertTrue(response["bank"]["assigned"])

    async def test_every_creation_route_takes_the_group(self):
        fields = {"bank_mode": "surround", "bank_id": "global", "bank_group": "front", "expected_revision": 4}
        with mock.patch.object(FakeOutputService, "load", return_value=SURROUND_STATE):
            await dsp_api.create_convolver_preset(preset_name="A", ir_filename="x.irs", **fields)
            await dsp_api.create_convolver_preset_with_ir(preset_name="B", file=FakeUploadFile(), **fields)
            await dsp_api.import_dual_filter_preset(
                preset_name="C", left_file=FakeUploadFile("l.wav"), right_file=FakeUploadFile("r.wav"), **fields)
        self.assertEqual(self.manager.groups, ["front", "front", "front"])
        # The bank's output mode travels along: it makes a Global preset that mode's own.
        self.assertEqual(self.manager.modes, ["surround", "surround", "surround"])
        # A group filter completes the listened Global set.
        self.assertEqual(self.manager.bases, ["Front set", "Front set", "Front set"])

    async def test_surround_global_needs_a_group_and_other_banks_take_none(self):
        with mock.patch.object(FakeOutputService, "load", return_value=SURROUND_STATE):
            with self.assertRaises(HTTPException) as missing:
                await dsp_api.create_peq_preset(self.surround_peq_request())
            with self.assertRaises(HTTPException) as unknown:
                await dsp_api.create_peq_preset(self.surround_peq_request(bank_group="lfe"))
            with self.assertRaises(HTTPException) as foreign:
                await dsp_api.create_peq_preset(self.peq_request(bank_mode="surround", bank_id="rear",
                                                                  bank_group="rear"))
        self.assertEqual(missing.exception.status_code, 400)
        self.assertIn("need a speaker group", missing.exception.detail)
        self.assertEqual(unknown.exception.status_code, 400)
        self.assertEqual(foreign.exception.status_code, 400)
        with self.assertRaises(HTTPException) as stereo:
            await dsp_api.create_peq_preset(self.peq_request(bank_group="front"))
        self.assertEqual(stereo.exception.status_code, 400)
        self.assertEqual(self.manager.calls, [])
        self.assertEqual(self.assignments, [])


def frozen_target(*, bank_id="main", fingerprint="fp-live", mode="stereo",
                  device_key="device-a", sample_rate_hz=48_000,
                  measured_roles=("main_l", "main_r"), **extra):
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
        **extra,
    }


class MainMeasurementCommitVerifierTests(unittest.TestCase):
    """main.py's hook compares every source target with the roles the commit bank plays."""

    def setUp(self):
        self.roles = ["main_l", "main_r"]
        self.areas = []
        self.resolved = []

        def area(bank_id):
            self.areas.append(bank_id)
            return {"state": True}, bank_id, "device-a", 2

        def roles(state, *, bank_id, output_key, channels, group=None):
            self.resolved.append((bank_id, output_key, channels, group))
            return list(self.roles)

        patchers = [
            mock.patch.object(main, "_measurement_area", side_effect=area),
            mock.patch.object(main, "area_measured_roles", side_effect=roles),
            # Neither the store nor a compiled plan may be needed.
            mock.patch.object(main, "measurement_store", None),
            mock.patch.object(main, "_freeze_measurement_target", side_effect=AssertionError("freeze")),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def verify(self, *targets, **binding):
        main._verify_measurement_commit(
            list(targets), {"mode": "stereo", "bank_id": "main", "expected_revision": 0, **binding})

    def test_saved_and_unsaved_sources_pass_and_the_commit_bank_is_resolved_once(self):
        self.verify(frozen_target(), frozen_target())
        self.assertEqual(self.areas, ["main"])
        self.assertEqual(self.resolved, [("main", "device-a", 2, None)])

    def test_rate_processing_device_and_tap_changes_pass(self):
        self.verify(frozen_target(fingerprint="fp-measured", device_key="device-b", sample_rate_hz=96_000,
                                  reference_tap="elsewhere"))

    def test_other_bank_mode_or_group_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "captured for stereo area 'global'"):
            self.verify(frozen_target(bank_id="global"))
        with self.assertRaisesRegex(ValueError, "captured for surround area 'main'"):
            self.verify(frozen_target(mode="surround"))
        with self.assertRaisesRegex(ValueError, "speaker group 'rear'"):
            self.verify(frozen_target(global_group="rear"))
        # The roles are those of the bank the preset is committed into.
        self.assertEqual(set(self.areas), {"main"})

    def test_every_source_is_checked(self):
        with self.assertRaisesRegex(ValueError, "captured for stereo area 'global'"):
            self.verify(frozen_target(), frozen_target(bank_id="global"))

    def test_another_channel_shape_is_rejected(self):
        with self.assertRaisesRegex(ValueError, r"covers \['main_l'\], but stereo area 'main' plays"):
            self.verify(frozen_target(measured_roles=("main_l",)))
        # The bank now plays other roles, e.g. after the routing changed.
        self.roles = ["main_l", "main_r", "sub1"]
        with self.assertRaisesRegex(ValueError, "plays \\['main_l', 'main_r', 'sub1'\\]"):
            self.verify(frozen_target())

    def test_all_banks_and_results_without_a_target_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "through All Banks"):
            self.verify(frozen_target(bank_id="all"))
        for source in (None, {}, {"schema": "fxroute.measurement-target", "version": 1, "legacy": True}):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "no frozen target; measure again"):
                self.verify(source)

    def test_surround_global_resolves_the_binding_group(self):
        self.roles = ["rear_l", "rear_r", "sub1"]
        target = frozen_target(mode="surround", bank_id="global", global_group="rear",
                               measured_roles=("rear_l", "rear_r", "sub1"))
        self.verify(target, mode="surround", bank_id="global", bank_group="rear")
        self.assertEqual(self.resolved, [("global", "device-a", 2, "rear")])
        with self.assertRaisesRegex(ValueError, "speaker group 'rear'"):
            self.verify(target, mode="surround", bank_id="global", bank_group="front")

    def test_an_unresolvable_commit_bank_is_reported(self):
        with mock.patch.object(main, "area_measured_roles",
                               side_effect=ValueError("Bank sub1 is not an active role on the selected outputs")):
            with self.assertRaisesRegex(ValueError, "not an active role"):
                self.verify(frozen_target(bank_id="sub1"), bank_id="sub1")


if __name__ == "__main__":
    unittest.main()
