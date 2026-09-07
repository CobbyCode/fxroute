#!/usr/bin/env python3
"""Regression: preset load must map validation errors to JSON, not a 500 page.

On the 104 host, selecting the stale "+6" preset (left=gain / right=bell,
written before the shared-stereo-trim validation existed) in A/B compare
returned plain-text "Internal Server Error" (HTTP 500). The frontend's
response.json() then failed with "Unexpected token 'I' ... is not valid
JSON", hiding the real message. Root cause: the load endpoint caught only
(FileNotFoundError, RuntimeError), letting ValueError escape, while the
create endpoint already mapped it to HTTP 400. Valid +6 dB presets
(gain/gain) load fine; only the error mapping was missing.
"""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException

import dsp.api as dsp_api


def _deps(load_effect):
    return dsp_api.DspApiDeps(
        require_dsp_manager=mock.MagicMock(),
        get_dsp_manager=mock.MagicMock(),
        get_dsp_runtime=lambda: None,
        get_dsp_preset_load_lock=lambda: asyncio.Lock(),
        dsp_mutation_lock=lambda: asyncio.Lock(),
        canonical_volume_write_lock=lambda: asyncio.Lock(),
        drain_worker=mock.AsyncMock(),
        run_locked_worker=mock.AsyncMock(),
        broadcast=mock.AsyncMock(),
        load_dsp_preset=load_effect,
        restore_volume_state=mock.AsyncMock(),
        volume_state_for_manager=mock.MagicMock(),
        schedule_peak_monitor_refresh=mock.MagicMock(),
    )


class FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class LoadPresetErrorMappingTests(unittest.TestCase):
    def test_value_error_becomes_400_json(self):
        async def failing_load(preset_name):
            raise ValueError("Gain filter supports only shared stereo trim in dual mode")

        dsp_api.configure_dsp_api(_deps(failing_load))
        try:
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(dsp_api.load_dsp_preset(FakeRequest({"preset_name": "+6"})))
        finally:
            dsp_api.configure_dsp_api(
                dsp_api.DspApiDeps(
                    require_dsp_manager=mock.MagicMock(),
                    get_dsp_manager=mock.MagicMock(),
                    get_dsp_runtime=lambda: None,
                    get_dsp_preset_load_lock=lambda: asyncio.Lock(),
                    dsp_mutation_lock=lambda: asyncio.Lock(),
                    canonical_volume_write_lock=lambda: asyncio.Lock(),
                    drain_worker=mock.AsyncMock(),
                    run_locked_worker=mock.AsyncMock(),
                    broadcast=mock.AsyncMock(),
                    load_dsp_preset=mock.AsyncMock(),
                    restore_volume_state=mock.AsyncMock(),
                    volume_state_for_manager=mock.MagicMock(),
                    schedule_peak_monitor_refresh=mock.MagicMock(),
                )
            )
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("shared stereo trim", str(ctx.exception.detail))


if __name__ == "__main__":
    unittest.main()
