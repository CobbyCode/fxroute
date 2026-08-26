#!/usr/bin/env python3
"""AutoSub pre-sweep peak safety must use a fresh unclamped master read.

Regression for the review finding that the safety decision before each
sweep used the non-blocking last-known status cache.  A stale low value
would under-estimate the cubic sink gain and could admit a sweep that
clips the float→integer conversion; an externally raised >100% master was
additionally clamped to 100% by the wpctl parser.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main
import measurement.session as measurement_session
import measurement.autosub as autosub
from dsp.runtime import BassManagementConfig


def runtime_config(sub_level_db: float = 12.0) -> BassManagementConfig:
    return BassManagementConfig(
        output_mode="subwoofer-2.1", output_key="mock", output_label="Mock",
        output_channels=4, sample_rate=48_000, crossover_frequency_hz=80,
        main_highpass_enabled=True, sub_level_db=sub_level_db, sub_alignment_ms=2.0,
        sub_polarity="normal",
    )


class AutoSubFreshMasterReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_live_unclamped_read_not_the_status_cache(self):
        with patch.object(
            autosub.measurement, "get_output_volume_unclamped", return_value=62,
        ) as live:
            self.assertEqual(await autosub.measurement._auto_sub_fresh_master_percent(), 62)
        live.assert_called_once_with()

    async def test_failed_live_read_conservatively_assumes_100(self):
        with patch.object(
            autosub.measurement, "get_output_volume_unclamped",
            side_effect=RuntimeError("wpctl wedged"),
        ):
            self.assertEqual(await autosub.measurement._auto_sub_fresh_master_percent(), 100)

    async def test_above_100_percent_master_is_passed_through(self):
        with patch.object(
            autosub.measurement, "get_output_volume_unclamped", return_value=125,
        ):
            self.assertEqual(await autosub.measurement._auto_sub_fresh_master_percent(), 125)

    def test_sink_gain_unclamped_above_100(self):
        self.assertEqual(
            autosub.auto_sub_sink_gain_from_master_percent(125, clamp_upper=False),
            1.25 ** 3,
        )
        self.assertEqual(
            autosub.auto_sub_sink_gain_from_master_percent(125),
            1.0,
        )


class AutoSubCandidateSafetyVolumeTests(unittest.IsolatedAsyncioTestCase):
    """The candidate path blocks unsafe sweeps from the fresh read value."""

    def setUp(self):
        self.last_job = None

    def _harness(self, job, runtime, store, sweep_profile):
        return [
            patch.object(main.runtime, "dsp_runtime", runtime),
            patch.object(main, "measurement_store", store),
            patch.object(autosub.measurement, "set_audio_output_mode"),
            patch.object(autosub.measurement, "get_audio_output_overview", return_value={}),
            patch.object(BassManagementConfig, "from_overview", return_value=runtime_config()),
            patch.object(
                measurement_session, "_sync_dsp_runtime_for_measurement_sweep",
                new_callable=AsyncMock, return_value=None,
            ),
            patch.object(main.asyncio, "sleep", side_effect=lambda _seconds: None),
            patch("audio.samplerate._load_audio_output_mode", return_value={
                "subwoofer": {"sub_alignment_ms": 2.0},
            }),
        ]

    def _completed_job_result(self):
        return {
            "status": "completed",
            "result": {"measurement": {
                "channel": "left",
                "traces": [{"kind": "sweep-response", "points": [[20, -1], [80, 1]]}],
                "analysis": {"normalized_by_db": -20, "sample_rate": 48_000},
            }},
        }

    async def _run_candidate(
        self, master_percent=None, *, master_error=None, sweep_profile=None, measured_peaks=None,
    ):
        job = {"cancel_requested": False, "_sweep_timings": [], "auto_gain": {"available": False, "reason": "pending"}}

        class FakeRuntime:
            def __init__(self):
                self.muted = False

            async def sync(self, _config):
                return None

            async def set_exact_sub_mute(self, enabled):
                previous = self.muted
                self.muted = bool(enabled)
                return previous

            async def reset_output_peaks(self):
                return None

            async def read_output_peaks(self):
                return dict(measured_peaks)

            def snapshot(self):
                return {"exact_sub_mute": self.muted}

        class FakeStore:
            async def start_measurement(self, **_kwargs):
                return {"id": "safety-sweep"}

            def cancel_job(self, _sweep_id):
                return None

            def get_job(self, _sweep_id):
                return self._completed

        runtime = FakeRuntime()
        store = FakeStore()
        store._completed = self._completed_job_result()
        volume_patch = (
            patch.object(
                autosub.measurement, "get_output_volume_unclamped", side_effect=master_error,
            )
            if master_error is not None
            else patch.object(
                autosub.measurement, "get_output_volume_unclamped", return_value=master_percent,
            )
        )
        with contextlib.ExitStack() as stack:
            stack.enter_context(volume_patch)
            stack.enter_context(patch(
                "measurement.autosub.measurement._auto_sub_cancel_requested", return_value=False,
            ))
            for patcher in self._harness(job, runtime, store, sweep_profile):
                stack.enter_context(patcher)
            self.last_job = job
            return job, await autosub._measure_auto_sub_candidate(
                        delay_ms=2.0, job=job, candidate_index=1, total=2,
                        stage="safety", fc=80, input_id="mic", channel="left",
                        mic_input_channel="1", reference_input_channel="", calibration_ref="",
                        calibration_filename=None, calibration_bytes=None,
                        auto_sub_sweep_profile=sweep_profile,
                        auto_sub_rate=48_000, original_level=0.0, original_polarity="normal",
                        original_highpass=True,
                    )

    def _engine_prediction(self, sweep_profile, sink_gain=1.0):
        """Engine-output level prediction (before the sink gain is folded in)."""
        return autosub._auto_sub_stage_peak_prediction(
            sweep_profile=sweep_profile, sample_rate=48_000, channel="left",
            config=runtime_config(), sink_gain=sink_gain,
        )

    async def test_above_100_master_blocks_the_unsafe_sweep(self):
        sweep_profile = {"sweep_seconds": 0.1, "sweep_start_hz": 20.0, "sweep_end_hz": 200.0}
        # At the cubic gain of a 125% master the +12 dB candidate clips the
        # float→integer conversion; at 100% the same candidate is also unsafe.
        self.assertFalse(self._engine_prediction(sweep_profile, sink_gain=1.25 ** 3)["safe"])
        measured = self._engine_prediction(sweep_profile)["linear"]
        with self.assertRaises(autosub.AutoSubPeakSafetyError):
            await self._run_candidate(125, sweep_profile=sweep_profile, measured_peaks=measured)
        self.assertIn(
            "headroom_blocked",
            self.last_job["auto_gain"]["stage_output_peaks"]["status"],
        )
        self.assertIn("master volume 125%", self.last_job["message"])

    async def test_read_failure_blocks_the_unsafe_sweep_conservatively(self):
        sweep_profile = {"sweep_seconds": 0.1, "sweep_start_hz": 20.0, "sweep_end_hz": 200.0}
        # At full master (the conservative fallback) the +12 dB candidate is
        # unsafe; a stale low read must never be able to admit it.
        self.assertFalse(self._engine_prediction(sweep_profile)["safe"])
        measured = self._engine_prediction(sweep_profile)["linear"]
        with self.assertRaises(autosub.AutoSubPeakSafetyError):
            await self._run_candidate(
                master_error=RuntimeError("wpctl wedged"),
                sweep_profile=sweep_profile, measured_peaks=measured,
            )
        self.assertIn(
            "headroom_blocked",
            self.last_job["auto_gain"]["stage_output_peaks"]["status"],
        )
        self.assertIn("master volume 100%", self.last_job["message"])

    async def test_low_fresh_master_allows_the_same_sweep(self):
        sweep_profile = {"sweep_seconds": 0.1, "sweep_start_hz": 20.0, "sweep_end_hz": 200.0}
        # 10% cubic sink gain (0.001) leaves the +12 dB candidate far below
        # full-scale; the swept candidate must complete.
        self.assertTrue(self._engine_prediction(sweep_profile, sink_gain=0.001)["safe"])
        measured = self._engine_prediction(sweep_profile)["linear"]
        _job, result = await self._run_candidate(
            10, sweep_profile=sweep_profile, measured_peaks=measured,
        )
        self.assertEqual(result["status"], "completed")
        self.assertIn("sink_gain", result["stage_output_peaks"]["predicted"])
        self.assertAlmostEqual(
            result["stage_output_peaks"]["predicted"]["sink_gain"], 0.001, places=12,
        )

    async def test_fresh_read_failure_assumes_full_master(self):
        job = {"cancel_requested": False, "_sweep_timings": [], "auto_gain": {"available": False, "reason": "pending"}}
        with patch.object(
            autosub.measurement, "get_output_volume_unclamped",
            side_effect=RuntimeError("wpctl wedged"),
        ):
            self.assertEqual(
                await autosub.measurement._auto_sub_fresh_master_percent(),
                100,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)