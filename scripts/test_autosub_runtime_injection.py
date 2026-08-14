#!/usr/bin/env python3
"""AutoSub resolves its DSP runtime through the injected accessor.

Proves the lifecycle-owned runtime back-import from main.py is gone: the
AutoSub DSP helpers read the native DSP runtime from the injected
``AutoSubRuntimeDependencies.get_dsp_runtime`` accessor and never from
``main.runtime``.  This module imports only ``autosub``; it does not import
``main`` at all.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import autosub


def overview_21(alignment=2.34, mode="subwoofer-2.1", fc=80, level=-3.0,
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


class FakeDSPRuntime:
    def __init__(self):
        self.sync = AsyncMock()

    def snapshot(self):
        return {"active": True}


class AutoSubRuntimeInjectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.first = FakeDSPRuntime()
        autosub.configure_runtime_dependencies(
            autosub.AutoSubRuntimeDependencies(get_dsp_runtime=lambda: self.first)
        )

    async def test_sync_uses_injected_dsp_runtime(self):
        persisted = overview_21()
        with patch.object(autosub, "get_audio_output_overview", return_value=overview_21()):
            await autosub._auto_sub_sync_dsp_runtime(
                output_mode="subwoofer-2.1", persisted_overview=persisted)
        self.first.sync.assert_awaited_once()

    async def test_late_bound_accessor_observes_reconfiguration(self):
        replacement = FakeDSPRuntime()
        autosub.configure_runtime_dependencies(
            autosub.AutoSubRuntimeDependencies(get_dsp_runtime=lambda: replacement)
        )
        persisted = overview_21()
        with patch.object(autosub, "get_audio_output_overview", return_value=overview_21()):
            await autosub._auto_sub_sync_dsp_runtime(
                output_mode="subwoofer-2.1", persisted_overview=persisted)
        replacement.sync.assert_awaited_once()
        self.first.sync.assert_not_awaited()

    async def test_none_runtime_is_a_noop(self):
        autosub.configure_runtime_dependencies(
            autosub.AutoSubRuntimeDependencies(get_dsp_runtime=lambda: None)
        )
        with patch.object(autosub, "get_audio_output_overview", return_value=overview_21()):
            await autosub._auto_sub_sync_dsp_runtime(
                output_mode="subwoofer-2.1", persisted_overview=overview_21())
        self.first.sync.assert_not_awaited()


if __name__ == "__main__":
    unittest.main(verbosity=2)
