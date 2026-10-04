#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""L/R repeat entry must retain the committed plan, not a legacy overview."""

from copy import deepcopy
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.output_state import default_output_state, set_mode_routing, switch_mode
from dsp.native_config import layout_from_plan
from dsp.processing_plan import compile_processing_plan
from measurement import session
from measurement.store import MeasurementStore
from measurement.target import freeze_measurement_target


class RepeatPlanContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        environment = patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(Path(home.name) / "config"),
            "XDG_STATE_HOME": str(Path(home.name) / "state"),
            "XDG_DATA_HOME": str(Path(home.name) / "data"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.store = MeasurementStore(home=Path(home.name))
        roles = ["main_l", "main_r", "rear_l", "rear_r", "sub1", "sub2"]
        self.state = switch_mode(set_mode_routing(default_output_state(), "surround", "device", roles), "surround")
        self.plan = compile_processing_plan(
            self.state, output_key="device", channels=18, sample_rate_hz=48000,
            preset_loader=lambda _: {"chain": []})
        self.layout = layout_from_plan(self.plan, resolve_ir=lambda _: self.fail("Unexpected IR"))
        self.expected = {
            "expected_native_layout": self.layout,
            "expected_native_output_mode": "surround",
            "expected_plan_fingerprint": "committed-surround-plan",
        }
        self.store.measurement_target_provider = lambda bank_id, sample_rate_hz: freeze_measurement_target(
            self.state, bank_id=bank_id, output_key="device", channels=18,
            sample_rate_hz=sample_rate_hz, fingerprint="committed-surround-plan", plan=self.plan)
        self.setup = patch.object(self.store, "_prepare_measurement_job_setup", new=AsyncMock(return_value={
            "job": {"id": "repeat-test", "created_at": "2026-10-04T20:00:00Z",
                    "measurement_scope": "active_chain", "input": {"sample_rate": 48000}},
        }))
        self.setup_mock = self.setup.start()
        self.addCleanup(self.setup.stop)
        registration = patch.object(self.store, "_register_measurement_job", side_effect=lambda job, executor: deepcopy(job))
        registration.start()
        self.addCleanup(registration.stop)

    async def start_route(self, factory):
        services = SimpleNamespace(
            get_store=lambda: self.store, get_session=lambda: None,
            auto_sub_active=lambda: False, stage_bank_v2_context=factory)
        with patch.object(session, "_services", services), \
             patch.object(session, "_resolve_measurement_start_sample_rate", return_value=48000), \
             patch.object(session, "_measurement_entry_preflight", new=AsyncMock()):
            return await session.start_lr_repeat_measurement(
                input_id="microphone", input_key="stable-microphone", base_name="Repeat test",
                mic_input_channel="1", reference_input_channel="7",
                reference_input_channel_left="7", reference_input_channel_right="8",
                calibration_ref=None, calibration_file=None, measurement_bank="main")

    async def test_repeat_endpoint_stages_and_stores_the_committed_surround_plan(self):
        factory = Mock(return_value=self.expected)
        job = (await self.start_route(factory))["job"]
        factory.assert_called_once_with(measurement_bank="main", measurement_rate_hz=48000, channel="stereo")
        self.assertEqual(job["_expected_native_output_mode"], "surround")
        self.assertEqual(job["_expected_native_layout"], self.layout)
        self.assertEqual(job["_expected_plan_fingerprint"], "committed-surround-plan")
        self.assertEqual(job["measurement_target"]["bank_id"], "main")
        self.assertEqual(job["sweep_output_masks"], {"left": 62, "right": 61})

    async def test_repeat_without_factory_preserves_the_existing_legacy_fallback(self):
        job = (await self.start_route(None))["job"]
        self.assertNotIn("_expected_native_layout", job)
        self.assertEqual(job["measurement_target"]["mode"], "surround")

    async def test_repeat_context_is_detached_before_hardware_setup_awaits(self):
        original_layout = deepcopy(self.layout)
        async def prepare(**kwargs):
            self.layout[0]["routes"][0]["gain"] = 99.0
            return {"job": {"id": "repeat-test", "created_at": "2026-10-04T20:00:00Z",
                            "measurement_scope": "active_chain", "input": {"sample_rate": 48000}}}
        self.setup_mock.side_effect = prepare
        job = await self.store.start_lr_repeat_measurement(
            input_id="microphone", measurement_bank="main", **self.expected)
        self.assertEqual(job["_expected_native_layout"], original_layout)

    async def test_partial_repeat_plan_is_rejected_before_hardware_setup(self):
        with self.assertRaisesRegex(ValueError, "required together"):
            await self.store.start_lr_repeat_measurement(
                input_id="microphone", expected_native_output_mode="surround")
        self.setup_mock.assert_not_awaited()

    async def test_internal_repeat_sweep_inherits_the_frozen_native_expectation(self):
        job = await self.store.start_lr_repeat_measurement(
            input_id="microphone", measurement_bank="main", **self.expected)
        seen = []
        def capture(capture_job):
            seen.append(capture_job)
            raise RuntimeError("Stopped at hardware boundary")
        with patch.object(self.store, "_execute_capture_job", side_effect=capture):
            with self.assertRaisesRegex(RuntimeError, "hardware boundary"):
                self.store._repeat_runner._execute_lr_repeat_job(job)
        self.assertEqual(seen[0]["channel"], "left")
        self.assertEqual(seen[0]["_expected_native_layout"], self.layout)
        self.assertEqual(seen[0]["_expected_native_output_mode"], "surround")
        self.assertEqual(seen[0]["_expected_plan_fingerprint"], "committed-surround-plan")


if __name__ == "__main__":
    unittest.main(verbosity=2)
