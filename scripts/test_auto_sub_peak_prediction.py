#!/usr/bin/env python3
"""AutoSub peak prediction runs off the event loop and caches by state.

The pure-Python cascaded biquad prediction previously ran synchronously on the
event loop (seconds per sweep, minutes per run, freezing every HTTP handler).
Regression coverage:

* the async wrapper actually executes the prediction in a worker thread,
* identical state returns an equal, isolated copy (the exact-sub-mute caller
  mutates its own result without poisoning the cache),
* the cache key covers configuration, sample rate, sweep profile, channel and
  sink gain, so any relevant state change recomputes,
* a repeat with unchanged state skips the filter computation entirely.
"""

import asyncio
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dsp.runtime import BassManagementConfig  # noqa: E402

from measurement.autosub.jobs import (  # noqa: E402
    _auto_sub_peak_prediction_cache_key,
    _predict_auto_sub_stage_peaks,
)

import measurement.autosub.jobs as autosub_jobs  # noqa: E402

# Short sweep: exercising the cache/offload semantics must not pay the
# production 3-11 s sweep filter cost on every assertion.
PROFILE = {"sweep_start_hz": 20.0, "sweep_end_hz": 600.0, "sweep_seconds": 0.25}


def config_21(*, alignment_ms: float = 2.34, level_db: float = -3.0) -> BassManagementConfig:
    overview = {
        "selected_output": {"key": "mock", "channels": 4, "active_rate": 48000, "label": "Mock"},
        "output_mode": {
            "mode": "subwoofer-2.1",
            "crossover_frequency_hz": 80,
            "main_highpass_enabled": True,
            "subwoofer": {
                "crossover_frequency_hz": 80,
                "main_highpass_enabled": True,
                "sub_alignment_ms": alignment_ms,
                "sub_level_db": level_db,
                "sub_polarity": "normal",
            },
        },
    }
    return BassManagementConfig.from_overview(overview)


def _args(**overrides):
    args = dict(
        sweep_profile=PROFILE, sample_rate=48000, channel="left",
        config=config_21(), playback_gain=1.0, sink_gain=0.5,
    )
    args.update(overrides)
    return args


class PeakPredictionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # The cache globals live in the jobs module and are rebound there on
        # every prediction, so reset them through the module before each test.
        autosub_jobs._AUTO_SUB_PEAK_PREDICTION_CACHE_KEY = None
        autosub_jobs._AUTO_SUB_PEAK_PREDICTION_CACHE_RESULT = None

    async def test_prediction_runs_in_a_worker_thread(self) -> None:
        main_thread = threading.get_ident()
        executed_in: list[int] = []

        def fake_prediction(**kwargs):
            executed_in.append(threading.get_ident())
            return {"linear": {"output_1": 0.5}, "safe": True}

        with patch("measurement.autosub.jobs._auto_sub_stage_peak_prediction", side_effect=fake_prediction):
            result = await _predict_auto_sub_stage_peaks(**_args())
        self.assertEqual(result, {"linear": {"output_1": 0.5}, "safe": True})
        self.assertEqual(len(executed_in), 1)
        self.assertNotEqual(executed_in[0], main_thread)

    async def test_identical_state_is_cached_and_isolated(self) -> None:
        first = await _predict_auto_sub_stage_peaks(**_args())
        second = await _predict_auto_sub_stage_peaks(**_args())
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        # The caller (exact-sub-mute handling in _measure_auto_sub_candidate)
        # mutates the returned dict; the cached entry must stay untouched.
        first["linear"]["output_1"] = 999.0
        third = await _predict_auto_sub_stage_peaks(**_args())
        self.assertEqual(third["linear"]["output_1"], second["linear"]["output_1"])
        self.assertNotEqual(third["linear"]["output_1"], 999.0)

    async def test_cached_repeat_skips_the_filter_computation(self) -> None:
        import numpy as np
        from measurement.autosub import jobs

        calls = 0
        original_arange = np.arange

        def counting_arange(*nargs, **nkwargs):
            nonlocal calls
            calls += 1
            return original_arange(*nargs, **nkwargs)

        with patch.object(jobs.np, "arange", side_effect=counting_arange):
            first = await _predict_auto_sub_stage_peaks(**_args())
            self.assertEqual(calls, 1)
            second = await _predict_auto_sub_stage_peaks(**_args())
            # The second call must be served from the cache: no sweep PCM
            # array is built, i.e. np.arange never runs again.
            self.assertEqual(calls, 1)
        self.assertEqual(first, second)

    async def test_sink_gain_change_recomputes_and_scales_output(self) -> None:
        low_volume = await _predict_auto_sub_stage_peaks(**_args())
        high_volume = await _predict_auto_sub_stage_peaks(**_args(sink_gain=1.0))
        # The sink gain is a linear amplitude scale on every output.
        self.assertAlmostEqual(
            high_volume["linear"]["output_1"] / low_volume["linear"]["output_1"],
            2.0, places=4,
        )
        self.assertNotEqual(autosub_jobs._AUTO_SUB_PEAK_PREDICTION_CACHE_KEY, _auto_sub_peak_prediction_cache_key(**_args()))
        self.assertEqual(autosub_jobs._AUTO_SUB_PEAK_PREDICTION_CACHE_KEY, _auto_sub_peak_prediction_cache_key(**_args(sink_gain=1.0)))

    async def test_configuration_change_recomputes(self) -> None:
        base = await _predict_auto_sub_stage_peaks(**_args())
        moved = await _predict_auto_sub_stage_peaks(
            **_args(config=config_21(alignment_ms=-3.12, level_db=0.0))
        )
        self.assertNotEqual(moved["linear"], base["linear"])
        self.assertEqual(
            autosub_jobs._AUTO_SUB_PEAK_PREDICTION_CACHE_KEY,
            _auto_sub_peak_prediction_cache_key(
                **_args(config=config_21(alignment_ms=-3.12, level_db=0.0))
            ),
        )


if __name__ == "__main__":
    unittest.main()
