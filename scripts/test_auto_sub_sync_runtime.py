#!/usr/bin/env python3
"""AutoSub DSP runtime sync must receive the exact persisted candidate state.

Regression for the native-DSP migration break where AutoSub passed a
BassManagementConfig dataclass to DSPRuntime.sync (which expects the audio
overview dict), failing every candidate with "'BassManagementConfig' object
has no attribute 'get'" before the short sweep could start.

The sync helper must additionally verify that the live overview still
carries the candidate/winner values AutoSub just persisted (mode, sub
alignments, levels, polarities, crossover, main high-pass) and raise instead
of syncing a stale or incumbent topology.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main
import autosub


def overview_21(alignment, mode="subwoofer-2.1", fc=80, level=-3.0,
                polarity="normal", highpass=True):
    return {
        "selected_output": {"key": "mock", "channels": 4,
                            "active_rate": 48000, "label": "Mock"},
        "output_mode": {
            "mode": mode,
            "crossover_frequency_hz": fc,
            "main_highpass_enabled": highpass,
            "subwoofer": {
                "crossover_frequency_hz": fc,
                "main_highpass_enabled": highpass,
                "sub_alignment_ms": alignment,
                "sub_level_db": level,
                "sub_polarity": polarity,
            },
        },
    }


def overview_22(alignment1, alignment2, mode="subwoofer-2.2", fc=80,
                level1=0.0, level2=-2.0, polarity1="normal",
                polarity2="normal", highpass=True):
    return {
        "selected_output": {"key": "mock", "channels": 4,
                            "active_rate": 48000, "label": "Mock"},
        "output_mode": {
            "mode": mode,
            "crossover_frequency_hz": fc,
            "main_highpass_enabled": highpass,
            "subwoofers": {
                "sub1": {"level_db": level1, "alignment_ms": alignment1,
                         "polarity": polarity1},
                "sub2": {"level_db": level2, "alignment_ms": alignment2,
                         "polarity": polarity2},
            },
        },
    }


class FakeDSPRuntime:
    def __init__(self):
        self.sync = AsyncMock()
        self.sync_calls = []

    async def sync(self, overview, **kwargs):
        await self.sync(overview, **kwargs)
        self.sync_calls.append(overview)


class AutoSubSyncRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_runtime = main.runtime.dsp_runtime
        self.runtime = FakeDSPRuntime()
        main.runtime.dsp_runtime = self.runtime

    def tearDown(self):
        main.runtime.dsp_runtime = self.original_runtime

    async def test_21_sync_receives_live_overview_dict_with_candidate(self):
        persisted = overview_21(2.34, level=-3.0)
        with patch.object(autosub, "get_audio_output_overview", return_value=overview_21(2.34, level=-3.0)):
            await autosub._auto_sub_sync_dsp_runtime(
                output_mode="subwoofer-2.1", persisted_overview=persisted)
        self.assertEqual(self.runtime.sync.await_count, 1)
        received = self.runtime.sync.await_args.args[0]
        self.assertIsInstance(received, dict)
        self.assertEqual(received["output_mode"]["mode"], "subwoofer-2.1")
        self.assertEqual(
            received["output_mode"]["subwoofer"]["sub_alignment_ms"], 2.34)

    async def test_21_stale_incumbent_alignment_is_rejected_before_sync(self):
        persisted = overview_21(2.34)
        live = overview_21(0.0)
        with patch.object(autosub, "get_audio_output_overview", return_value=live):
            with self.assertRaises(RuntimeError) as raised:
                await autosub._auto_sub_sync_dsp_runtime(
                    output_mode="subwoofer-2.1", persisted_overview=persisted)
        self.assertIn("sub1 alignment", str(raised.exception))
        self.runtime.sync.assert_not_awaited()

    async def test_21_level_mismatch_is_rejected_before_sync(self):
        persisted = overview_21(2.34, level=-3.0)
        live = overview_21(2.34, level=-1.0)
        with patch.object(autosub, "get_audio_output_overview", return_value=live):
            with self.assertRaises(RuntimeError) as raised:
                await autosub._auto_sub_sync_dsp_runtime(
                    output_mode="subwoofer-2.1", persisted_overview=persisted)
        self.assertIn("sub1 level", str(raised.exception))
        self.runtime.sync.assert_not_awaited()

    async def test_21_crossover_mismatch_is_rejected_before_sync(self):
        persisted = overview_21(2.34, fc=80)
        live = overview_21(2.34, fc=100)
        with patch.object(autosub, "get_audio_output_overview", return_value=live):
            with self.assertRaises(RuntimeError) as raised:
                await autosub._auto_sub_sync_dsp_runtime(
                    output_mode="subwoofer-2.1", persisted_overview=persisted)
        self.assertIn("crossover", str(raised.exception))
        self.runtime.sync.assert_not_awaited()

    async def test_21_polarity_mismatch_is_rejected_before_sync(self):
        persisted = overview_21(2.34, polarity="invert")
        live = overview_21(2.34, polarity="normal")
        with patch.object(autosub, "get_audio_output_overview", return_value=live):
            with self.assertRaises(RuntimeError) as raised:
                await autosub._auto_sub_sync_dsp_runtime(
                    output_mode="subwoofer-2.1", persisted_overview=persisted)
        self.assertIn("polarity", str(raised.exception))
        self.runtime.sync.assert_not_awaited()

    async def test_22_sub2_alignment_mismatch_is_rejected_before_sync(self):
        persisted = overview_22(3.12, -1.56)
        live = overview_22(3.12, 0.0)
        with patch.object(autosub, "get_audio_output_overview", return_value=live):
            with self.assertRaises(RuntimeError) as raised:
                await autosub._auto_sub_sync_dsp_runtime(
                    output_mode="subwoofer-2.2", persisted_overview=persisted)
        self.assertIn("sub2 alignment", str(raised.exception))
        self.runtime.sync.assert_not_awaited()

    async def test_22_stereo_mode_mismatch_is_rejected_before_sync(self):
        persisted = overview_22(3.12, -1.56, mode="subwoofer-2.2-stereo")
        live = overview_22(3.12, -1.56, mode="subwoofer-2.2")
        with patch.object(autosub, "get_audio_output_overview", return_value=live):
            with self.assertRaises(RuntimeError) as raised:
                await autosub._auto_sub_sync_dsp_runtime(
                    output_mode="subwoofer-2.2-stereo",
                    persisted_overview=persisted)
        self.assertIn("mode", str(raised.exception))
        self.runtime.sync.assert_not_awaited()

    async def test_22_sub2_invert_polarity_is_accepted_and_synced(self):
        persisted = overview_22(3.12, -1.56, polarity2="invert")
        live = overview_22(3.12, -1.56, polarity2="invert")
        with patch.object(autosub, "get_audio_output_overview", return_value=live):
            await autosub._auto_sub_sync_dsp_runtime(
                output_mode="subwoofer-2.2", persisted_overview=persisted)
        self.runtime.sync.assert_awaited_once()

    async def test_runtime_none_is_a_noop(self):
        main.runtime.dsp_runtime = None
        with patch.object(autosub, "get_audio_output_overview", return_value=overview_21(2.34)):
            await autosub._auto_sub_sync_dsp_runtime(
                output_mode="subwoofer-2.1", persisted_overview=overview_21(2.34))
        self.runtime.sync.assert_not_awaited()

    async def test_sync_arg_is_never_the_dataclass_instance(self):
        persisted = overview_21(1.56)
        with patch.object(autosub, "get_audio_output_overview", return_value=overview_21(1.56)):
            await autosub._auto_sub_sync_dsp_runtime(
                output_mode="subwoofer-2.1", persisted_overview=persisted)
        received = self.runtime.sync.await_args.args[0]
        from dsp_runtime import BassManagementConfig
        self.assertFalse(isinstance(received, BassManagementConfig))


if __name__ == "__main__":
    unittest.main()
