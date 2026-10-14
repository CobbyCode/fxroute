#!/usr/bin/env python3
"""Regressions for extras merging and corrupt preset isolation at the API boundary."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException
from dsp import api
from dsp.manager import DSPManager


class DspAuditApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.manager = DSPManager(home=Path(temporary.name), active_preset_reader=lambda: "Direct")
        previous_deps = api._runtime.deps
        self.addCleanup(setattr, api._runtime, "deps", previous_deps)
        canonical, mutation, preset = asyncio.Lock(), asyncio.Lock(), asyncio.Lock()

        async def worker(function, *args, **kwargs):
            return await asyncio.to_thread(function, *args, **kwargs)

        api.configure_dsp_api(api.DspApiDeps(
            require_dsp_manager=lambda: self.manager, get_dsp_manager=lambda: self.manager,
            get_dsp_runtime=lambda: None,
            dsp_mutation_lock=lambda: mutation, canonical_volume_write_lock=lambda: canonical,
            drain_worker=worker, run_locked_worker=AsyncMock(), broadcast=AsyncMock(),
            restore_volume_state=AsyncMock(),
            volume_state_for_manager=AsyncMock(return_value={}), schedule_peak_monitor_refresh=lambda _: None,
            sync_v2_extras_runtime=AsyncMock()))

    async def test_loudness_volume_is_persisted_even_with_a_corrupt_preset(self):
        (self.manager.output_dir / "Broken.json").write_text("{")
        result = await api.save_dsp_extras(SimpleNamespace(json=AsyncMock(return_value={"loudnessVolumeDb": -24.5})))
        self.assertEqual(result["extras"]["loudness"]["params"]["volumeDb"], -24.5)
        self.assertEqual(self.manager.load_global_extras()["loudness"]["params"]["volumeDb"], -24.5)

    async def test_invalid_loudness_volume_is_a_bad_request_without_mutation(self):
        previous = self.manager.load_global_extras()
        for invalid in ("invalid", None, {}, float("nan"), -(10**400)):
            with self.subTest(value=invalid):
                with self.assertRaises(HTTPException) as caught:
                    await api.save_dsp_extras(SimpleNamespace(json=AsyncMock(return_value={"loudnessVolumeDb": invalid})))
                self.assertEqual(caught.exception.status_code, 400)
                self.assertEqual(self.manager.load_global_extras(), previous)


if __name__ == "__main__":
    unittest.main()
