#!/usr/bin/env python3
"""Saved output metadata describes the frozen v2 take, not the live device."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import measurement.session as session
from fastapi import HTTPException
from measurement.target import SCHEMA, VERSION


class SaveContextTests(unittest.IsolatedAsyncioTestCase):
    async def save(self, body):
        async def json():
            return body
        store = SimpleNamespace(save_measurement=lambda payload: payload,
                                save_measurements=lambda payload: payload)
        services = SimpleNamespace(get_store=lambda: store,
                                   get_audio_output_overview=lambda: {"output_mode": {"mode": "stereo"}})
        with patch.object(session, "_measurement_services", return_value=services):
            return await session.save_measurement(SimpleNamespace(json=json))

    async def test_batch_preserves_each_frozen_mode_device_and_rate(self):
        takes = [{"measurement_target": {"schema": SCHEMA, "version": VERSION,
                  "mode": mode, "device_key": "recorded-device",
                  "channels": channels, "sample_rate_hz": rate, "revision": 12,
                  "processing_fingerprint": "recorded-plan"}}
                 for mode, channels, rate in (("stereo-sub", 6, 48000), ("surround", 8, 96000))]
        saved = (await self.save({"measurements": takes}))["measurements"]
        for take, (mode, channels, rate) in zip(saved, (("stereo-sub", 6, 48000), ("surround", 8, 96000))):
            context = take["audio_output_context"]
            self.assertEqual(context["output_mode"], mode)
            self.assertEqual(context["output_key"], "recorded-device")
            self.assertEqual(context["output_channels"], channels)
            self.assertEqual(context["sample_rate_hz"], rate)

    async def test_legacy_take_does_not_claim_the_live_stereo_mode(self):
        saved = (await self.save({"name": "old take"}))["measurement"]
        self.assertEqual(saved["audio_output_context"]["output_mode"], "unknown")

    async def test_nested_frozen_context_overrides_an_old_stereo_label(self):
        saved = (await self.save({"audio_output_context": {"output_mode": "stereo",
            "measurement_target": {"schema": SCHEMA, "version": VERSION, "mode": "surround",
                "device_key": "recorded", "channels": 8, "sample_rate_hz": 96000}}}))["measurement"]
        self.assertEqual(saved["audio_output_context"]["output_mode"], "surround")
        self.assertEqual(saved["audio_output_context"]["sample_rate_hz"], 96000)

    async def test_incomplete_target_is_bad_input_not_an_internal_error(self):
        with self.assertRaises(HTTPException) as caught:
            await self.save({"measurement_target": {"schema": SCHEMA, "version": VERSION}})
        self.assertEqual(caught.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
