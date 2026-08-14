#!/usr/bin/env python3
"""Regression: Stereo measurement release re-syncs the native DSP runtime.

The native DSP runtime now owns Stereo as well as the subwoofer modes.  After
a measurement session rebuilds it at the 48 kHz measurement rate, the release
path (_sync_dsp_runtime_at_rate) must re-sync it at the restored rate for
every output mode, including Stereo.  This guards the removed legacy gate that
skipped the re-sync whenever the active mode was not a subwoofer mode.

Scenario under test:
  native DSP at the measurement rate (48 kHz)
  -> measurement ends with the restore rate established (44.1 kHz)
  -> Stereo is the active output mode
  -> _sync_dsp_runtime_at_rate() re-syncs the DSP
  -> final runtime rate matches the restored rate.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


class _FakeRuntime:
    """Minimal native DSP runtime facade with a mutable helper rate."""

    def __init__(self, rate: int) -> None:
        self.rate = rate

    def snapshot(self) -> dict:
        return {
            "active": True,
            "helper_pid": 4242,
            "config": {"sample_rate": self.rate},
        }


class StereoMeasurementReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._saved = {
            "dsp_runtime": main.dsp_runtime,
            "_sync_dsp_runtime": main._sync_dsp_runtime,
            "_wait_for_selected_output_effective_rate": main._wait_for_selected_output_effective_rate,
            "_wait_for_samplerate_alignment": main._wait_for_samplerate_alignment,
            "get_audio_output_overview": main.get_audio_output_overview,
            "get_samplerate_status": main.get_samplerate_status,
        }

    async def asyncTearDown(self) -> None:
        for name, value in self._saved.items():
            setattr(main, name, value)

    async def test_stereo_release_resyncs_runtime_at_restore_rate(self) -> None:
        """Stereo release re-syncs the DSP from 48 kHz back to 44.1 kHz."""
        runtime = _FakeRuntime(48_000)  # native DSP at the measurement rate
        main.dsp_runtime = runtime

        # Stereo is the active output mode.
        main.get_audio_output_overview = lambda: {
            "output_mode": {"mode": main.OUTPUT_MODE_STEREO},
        }
        main.get_samplerate_status = lambda: {
            "force_rate": 44_100,
            "active_rate": 44_100,
        }
        main._wait_for_selected_output_effective_rate = AsyncMock(
            return_value=(True, {"output_mode": {"mode": main.OUTPUT_MODE_STEREO}})
        )
        main._wait_for_samplerate_alignment = AsyncMock(return_value=True)

        sync_reasons = []

        async def fake_sync(*, reason: str, _rate_lock_held: bool = False, **kwargs) -> dict:
            sync_reasons.append(reason)
            # Simulate the central sync rebuilding the DSP at the authoritative
            # live rate before returning.
            runtime.rate = 44_100
            return {"output_mode": {"mode": main.OUTPUT_MODE_STEREO}}

        main._sync_dsp_runtime = AsyncMock(side_effect=fake_sync)

        await main._sync_dsp_runtime_at_rate(44_100, _rate_lock_held=True)

        # The restore rate was established before the re-sync.
        main._wait_for_selected_output_effective_rate.assert_awaited_once_with(
            44_100, timeout_ms=3500
        )
        main._wait_for_samplerate_alignment.assert_awaited_once_with(
            44_100, timeout_ms=3500
        )

        # Stereo must not be skipped: two re-syncs (release + settle).
        self.assertEqual(
            sync_reasons,
            ["measurement-release", "measurement-release-settle"],
        )
        for call in main._sync_dsp_runtime.call_args_list:
            self.assertTrue(call.kwargs["_rate_lock_held"])

        # Final runtime rate matches the restored rate.
        self.assertEqual(runtime.rate, 44_100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
