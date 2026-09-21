#!/usr/bin/env python3
"""Focused shared Measurement job setup checks."""

import tempfile
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.output_state import default_output_state, set_mode_routing, switch_mode, set_crossover
from measurement.store import MeasurementStore
from measurement.target import freeze_measurement_target, narrow_measured_target, target_output_mask


def crossover_state():
    state = default_output_state()
    routes = [f"{side}_{way}" for side in ("right", "left") for way in ("high", "low")]
    routes += ["sub1", "left_low"]
    state = set_mode_routing(set_crossover(state, "stereo-sub", True), "stereo-sub", "dev", routes)
    state = switch_mode(state, "stereo-sub")
    state["revision"] = 7
    processing = state["modes"]["stereo-sub"]["processing"]
    for side in ("left", "right"):
        processing[f"{side}_low"]["lowpass"] = {
            "family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 2000}
        processing[f"{side}_high"]["highpass"] = {
            "family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 2000}
    return state, len(routes)


class MeasurementJobSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_setup_normalizes_reference_channel_and_common_fields(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = MeasurementStore(home=Path(tempdir))
            store._discover_capture_inputs = lambda: [{
                "id": "mic",
                "label": "Mic",
                "node_serial": "serial-1",
                "node_name": "capture_1",
                "channels": 2,
                "sample_rate": 48_000,
                "available": True,
            }]
            store._measurement_inputs_with_sample_rate = lambda inputs: inputs

            setup = await store._prepare_measurement_job_setup(
                input_id="mic",
                input_key="",
                mic_input_channel="1",
                reference_input_channel="1",
                calibration_filename=None,
                calibration_bytes=None,
                calibration_ref=None,
                measurement_scope="active-chain",
                job_prefix="measurement-job-",
            )

            self.assertEqual(setup["job"]["id"].split("-")[:2], ["measurement", "job"])
            self.assertEqual(setup["job"]["status"], "queued")
            self.assertEqual(setup["job"]["input"]["id"], "mic")
            self.assertEqual(setup["job"]["input_channels"]["mic"], 1)
            self.assertIsNone(setup["job"]["input_channels"]["electrical_reference"])
            self.assertIn("same channel", setup["job"]["input_channels"]["reference_disabled_reason"])
            self.assertEqual(setup["job"]["measurement_scope"], "active_chain")

    async def test_split_reference_channels_stored_for_multi_channel_input(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = MeasurementStore(home=Path(tempdir))
            store._discover_capture_inputs = lambda: [{
                "id": "mic", "label": "Mic", "node_serial": "serial-1", "node_name": "capture_1",
                "channels": 4, "sample_rate": 48_000, "available": True,
            }]
            store._measurement_inputs_with_sample_rate = lambda inputs: inputs

            setup = await store._prepare_measurement_job_setup(
                input_id="mic", input_key="", mic_input_channel="1", reference_input_channel="",
                reference_input_channel_left="2", reference_input_channel_right="3",
                calibration_filename=None, calibration_bytes=None, calibration_ref=None,
                measurement_scope="active-chain", job_prefix="measurement-job-",
            )

            channels = setup["job"]["input_channels"]
            self.assertEqual(channels["electrical_reference_left"], 2)
            self.assertEqual(channels["electrical_reference_right"], 3)
            self.assertEqual(channels["electrical_reference"], 2)
            self.assertEqual(channels["reference_disabled_reason"], "")

    async def test_shared_reference_applies_to_both_sides(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = MeasurementStore(home=Path(tempdir))
            store._discover_capture_inputs = lambda: [{
                "id": "mic", "label": "Mic", "node_serial": "serial-1", "node_name": "capture_1",
                "channels": 2, "sample_rate": 48_000, "available": True,
            }]
            store._measurement_inputs_with_sample_rate = lambda inputs: inputs

            setup = await store._prepare_measurement_job_setup(
                input_id="mic", input_key="", mic_input_channel="1", reference_input_channel="2",
                calibration_filename=None, calibration_bytes=None, calibration_ref=None,
                measurement_scope="active-chain", job_prefix="measurement-job-",
            )

            channels = setup["job"]["input_channels"]
            self.assertEqual(channels["electrical_reference"], 2)
            self.assertEqual(channels["electrical_reference_left"], 2)
            self.assertEqual(channels["electrical_reference_right"], 2)

    async def test_only_affected_side_is_disabled_when_reference_matches_mic(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = MeasurementStore(home=Path(tempdir))
            store._discover_capture_inputs = lambda: [{
                "id": "mic", "label": "Mic", "node_serial": "serial-1", "node_name": "capture_1",
                "channels": 4, "sample_rate": 48_000, "available": True,
            }]
            store._measurement_inputs_with_sample_rate = lambda inputs: inputs

            setup = await store._prepare_measurement_job_setup(
                input_id="mic", input_key="", mic_input_channel="2", reference_input_channel="",
                reference_input_channel_left="2", reference_input_channel_right="3",
                calibration_filename=None, calibration_bytes=None, calibration_ref=None,
                measurement_scope="active-chain", job_prefix="measurement-job-",
            )

            channels = setup["job"]["input_channels"]
            self.assertIsNone(channels["electrical_reference_left"])
            self.assertEqual(channels["electrical_reference_right"], 3)
            self.assertIn("same channel", channels["reference_disabled_reason_left"])
            self.assertIn("L", channels["reference_disabled_reason_left"])
            self.assertEqual(channels["reference_disabled_reason_right"], "")

    def test_resolver_picks_reference_for_capture_side(self):
        resolve = MeasurementStore._resolve_electrical_reference_input_channel
        split = {"mic": 1, "electrical_reference": 2, "electrical_reference_left": 2, "electrical_reference_right": 3}
        self.assertEqual(resolve(split, "left"), 2)
        self.assertEqual(resolve(split, "right"), 3)
        self.assertEqual(resolve(split, "stereo"), 2)
        self.assertIsNone(resolve({**split, "electrical_reference_left": None}, "left"))
        legacy = {"mic": 1, "electrical_reference": 4}
        self.assertEqual(resolve(legacy, "left"), 4)
        self.assertEqual(resolve(legacy, "right"), 4)
        self.assertIsNone(resolve({"mic": 1, "electrical_reference": None}, "left"))

    def test_candidate_resolver_keeps_every_configured_loopback_channel(self):
        resolve = MeasurementStore._resolve_electrical_reference_input_channels
        # An explicit candidate list is never filtered by side: which channel
        # carries the sweep is decided from the capture evidence.
        self.assertEqual(resolve({"electrical_reference_candidates": [7, 8, 7]}, "left"), [7, 8])
        self.assertEqual(resolve({"electrical_reference_candidates": [8]}, "right"), [8])
        self.assertEqual(resolve({"electrical_reference_candidates": ("8", 7)}, "right"), [8, 7])
        self.assertEqual(resolve({"electrical_reference_candidates": ["x", 0, 7]}, "left"), [7])
        # Without candidates the legacy single per-side reference is unchanged.
        split = {"mic": 1, "electrical_reference": 2, "electrical_reference_left": 2, "electrical_reference_right": 3}
        self.assertEqual(resolve(split, "right"), [3])
        self.assertEqual(resolve({"mic": 1, "electrical_reference": 4}, "left"), [4])
        self.assertEqual(resolve({"electrical_reference_candidates": []}, "left"), [])
        self.assertEqual(resolve({"mic": 1, "electrical_reference": None}, "left"), [])

    async def test_candidate_channels_are_stored_for_one_simultaneous_take(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = MeasurementStore(home=Path(tempdir))
            store._discover_capture_inputs = lambda: [{
                "id": "mic", "label": "Mic", "node_serial": "serial-1", "node_name": "capture_1",
                "channels": 18, "sample_rate": 48_000, "available": True,
            }]
            store._measurement_inputs_with_sample_rate = lambda inputs: inputs

            setup = await store._prepare_measurement_job_setup(
                input_id="mic", input_key="", mic_input_channel="1", reference_input_channel="7",
                reference_input_channel_left="7", reference_input_channel_right="8",
                reference_candidate_channels=["7", "8"],
                calibration_filename=None, calibration_bytes=None, calibration_ref=None,
                measurement_scope="active-chain", job_prefix="measurement-job-",
            )

            channels = setup["job"]["input_channels"]
            self.assertEqual(channels["electrical_reference_candidates"], [7, 8])
            self.assertEqual(channels["electrical_reference"], 7)
            self.assertEqual(channels["electrical_reference_left"], 7)
            self.assertEqual(channels["electrical_reference_right"], 8)

    async def test_candidate_channel_matching_the_microphone_is_dropped(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = MeasurementStore(home=Path(tempdir))
            store._discover_capture_inputs = lambda: [{
                "id": "mic", "label": "Mic", "node_serial": "serial-1", "node_name": "capture_1",
                "channels": 18, "sample_rate": 48_000, "available": True,
            }]
            store._measurement_inputs_with_sample_rate = lambda inputs: inputs

            setup = await store._prepare_measurement_job_setup(
                input_id="mic", input_key="", mic_input_channel="7", reference_input_channel="",
                reference_candidate_channels=["7", "8"],
                calibration_filename=None, calibration_bytes=None, calibration_ref=None,
                measurement_scope="active-chain", job_prefix="measurement-job-",
            )

            channels = setup["job"]["input_channels"]
            self.assertEqual(channels["electrical_reference_candidates"], [8])
            self.assertEqual(channels["mic"], 7)
            self.assertEqual(channels["electrical_reference"], 8)

    async def test_out_of_range_candidate_channel_is_rejected(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = MeasurementStore(home=Path(tempdir))
            store._discover_capture_inputs = lambda: [{
                "id": "mic", "label": "Mic", "node_serial": "serial-1", "node_name": "capture_1",
                "channels": 2, "sample_rate": 48_000, "available": True,
            }]
            store._measurement_inputs_with_sample_rate = lambda inputs: inputs

            with self.assertRaisesRegex(ValueError, "reference_candidate_channels"):
                await store._prepare_measurement_job_setup(
                    input_id="mic", input_key="", mic_input_channel="1", reference_input_channel="",
                    reference_candidate_channels=["7"],
                    calibration_filename=None, calibration_bytes=None, calibration_ref=None,
                    measurement_scope="active-chain", job_prefix="measurement-job-",
                )

    def test_narrowing_a_frozen_target_keeps_its_identity(self):
        state, channels = crossover_state()
        target = freeze_measurement_target(
            state, bank_id="global", output_key="dev", channels=channels,
            sample_rate_hz=48_000, fingerprint="frozen-plan")
        self.assertEqual(target["measured_roles"], target["roles"])
        narrowed = narrow_measured_target(target, roles=["left_low", "left_high"])
        self.assertEqual(narrowed["measured_roles"], ["left_low", "left_high"])
        self.assertEqual(narrowed["roles"], target["roles"])
        self.assertEqual(narrowed["revision"], target["revision"])
        self.assertEqual(narrowed["processing_fingerprint"], "frozen-plan")
        # The narrowed target mutes every role of the other side, and the global
        # target is left untouched.
        ordered = target["roles"]
        mask = target_output_mask(narrowed, roles=ordered)
        self.assertNotEqual(mask, 0)
        for index, role in enumerate(ordered):
            self.assertEqual(bool(mask & (1 << index)), not role.startswith("left_"))
        self.assertEqual(target_output_mask(target, roles=ordered), 0)
        self.assertEqual(target["measured_roles"], target["roles"])

    def test_narrowing_rejects_unknown_or_legacy_targets(self):
        state, channels = crossover_state()
        target = freeze_measurement_target(
            state, bank_id="global", output_key="dev", channels=channels,
            sample_rate_hz=48_000, fingerprint="frozen-plan")
        with self.assertRaisesRegex(ValueError, "no longer routed"):
            narrow_measured_target(target, roles=["left_mid"])
        with self.assertRaisesRegex(ValueError, "distinct role names"):
            narrow_measured_target(target, roles=["left_low", "left_low"])
        with self.assertRaisesRegex(ValueError, "frozen target document"):
            narrow_measured_target({"legacy": True}, roles=["left_low"])
        with self.assertRaises(ValueError):
            narrow_measured_target(["left_low"], roles=["left_low"])

    def test_frozen_target_replaces_the_provider_and_is_rate_checked(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = MeasurementStore(home=Path(tempdir))
            state, channels = crossover_state()
            target = freeze_measurement_target(
                state, bank_id="global", output_key="dev", channels=channels,
                sample_rate_hz=48_000, fingerprint="frozen-plan")
            narrowed = narrow_measured_target(target, roles=["left_low", "left_high"])
            calls = []
            store.measurement_target_provider = lambda bank, rate: calls.append((bank, rate)) or target
            job = {"measurement_scope": "active-chain",
                   "input": {"measurement_sample_rate": 48_000}}

            frozen = store._freeze_measurement_job_target(job, "", narrowed)
            self.assertEqual(calls, [])
            self.assertEqual(frozen["measurement_target"], narrowed)
            self.assertEqual(frozen["measurement_bank"], "global")
            self.assertEqual(frozen["output_mask"],
                             target_output_mask(narrowed, roles=target["roles"]))
            # Without a pre-frozen target the provider keeps resolving the area.
            self.assertEqual(store._freeze_measurement_job_target(job, "left_low")["measurement_target"],
                             target)
            self.assertEqual(calls, [("left_low", 48_000)])
            with self.assertRaisesRegex(ValueError, "input's rate"):
                store._freeze_measurement_job_target(job, "", {**narrowed, "sample_rate_hz": 96_000})
            with self.assertRaisesRegex(ValueError, "bank id"):
                store._freeze_measurement_job_target(job, "", {**narrowed, "bank_id": ""})

    async def test_selected_measurement_rate_is_stored_on_job(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = MeasurementStore(home=Path(tempdir))
            store._discover_capture_inputs = lambda: [{
                "id": "mic", "label": "Mic", "node_serial": "serial-1", "node_name": "capture_1",
                "channels": 2, "sample_rate": 192_000, "supported_rates": [48_000, 96_000, 192_000], "available": True,
            }]
            store._write_settings({"measure": {"selectedInputId": "mic", "measurementSampleRate": 48_000}})
            setup = await store._prepare_measurement_job_setup(
                input_id="mic", input_key="", mic_input_channel="1", reference_input_channel=None,
                calibration_filename=None, calibration_bytes=None, calibration_ref=None,
                measurement_scope="active-chain", job_prefix="measurement-job-",
            )
            self.assertEqual(setup["job"]["input"]["measurement_sample_rate"], 48_000)


if __name__ == "__main__":
    unittest.main()
