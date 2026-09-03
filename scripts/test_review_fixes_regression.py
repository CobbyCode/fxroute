#!/usr/bin/env python3
"""Regression for the code-review findings: DSP sync fallback and limiter bounds.

- sync_runtime() without a caller overview must fall back to the overview it
  already read when the post-gate live re-read fails, never to an empty
  dict that would sync the helper with a sparse config.
- The limiter time params are engine-port bounded (at/rt 0.25..20 ms,
  lk 0.1..20 ms, release default 20 ms); the frontend fallback must match
  the canonical backend defaults.
"""

import asyncio
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dsp.orchestration import DspOrchestrator


class _FakeDspRuntime:
    def __init__(self) -> None:
        self.sync_calls = []

    async def sync(self, overview: dict) -> None:
        self.sync_calls.append(dict(overview))


class _FakeMeasurementSrSession:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()


class _FailSecondOverviewDeps:
    """First overview read succeeds, every later one raises."""

    def __init__(self) -> None:
        self.runtime = _FakeDspRuntime()
        self.calls = 0
        self.overview = {
            "selected_output": {"key": "alsa_output.live", "active_rate": 48000},
            "active_rate": 48000,
            "output_mode": {"mode": "stereo"},
        }
        self.samplerate_status = {"force_rate": 48000, "active_rate": 48000}

    def get_dsp_runtime(self):
        return self.runtime

    def get_dsp_manager(self):
        return None

    def get_audio_output_overview(self):
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("transient PipeWire read failure")
        return dict(self.overview)

    def get_samplerate_status(self):
        return dict(self.samplerate_status)

    def get_measurement_sr_session(self):
        return _FakeMeasurementSrSession()

    def get_player_instance(self):
        return None

    def get_current_track_info(self):
        return None

    def get_peak_monitor(self):
        return None

    def peak_monitor_playback_armed(self):
        return False

    def set_peak_monitor_context_signature(self, _value):
        return None

    async def _unused(self, *_args, **_kwargs):
        raise AssertionError("not used by sync_runtime")

    get_spotify_ui_state = _unused
    sync_peak_monitor_for_playback_state = _unused
    sync_peak_monitor_for_spotify_state = _unused
    load_dsp_preset = _unused
    broadcast = _unused
    wait_for_samplerate_alignment = _unused
    wait_for_selected_output_effective_rate = _unused

    def measurement_audio_graph_owned(self):
        return False

    def observe_playback_samplerate_drift(self):
        raise AssertionError("not used by sync_runtime")

    def playback_transition_is_active(self):
        return False

    def coordinator_target_rate(self, *_args, **_kwargs):
        raise AssertionError("not used by sync_runtime")

    def playback_graph_diagnosis(self, *_args, **_kwargs):
        raise AssertionError("not used by sync_runtime")

    def request_coordinated_recovery(self, *_args, **_kwargs):
        raise AssertionError("not used by sync_runtime")

    def create_lifecycle_background_task(self, coro, *, name):
        coro.close()
        return None

    def peak_monitor_restart_settle_ms(self):
        return 0.0

    async def sleep(self, _seconds):
        return None


class DspSyncFallbackOverviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_arg_sync_reuses_already_read_overview_on_reread_failure(self) -> None:
        deps = _FailSecondOverviewDeps()
        orchestrator = DspOrchestrator(deps)
        result = await orchestrator.sync_runtime(reason="unit-fallback")
        self.assertEqual(len(deps.runtime.sync_calls), 1)
        synced = deps.runtime.sync_calls[0]
        # The already-read live overview survives: selected output key and
        # mode are intact instead of a sparse {} fallback.
        self.assertEqual(synced["selected_output"]["key"], "alsa_output.live")
        self.assertEqual(synced["output_mode"]["mode"], "stereo")
        self.assertEqual(synced["output_mode"]["effective_output_rate"], 48000)
        self.assertEqual(result["selected_output"]["key"], "alsa_output.live")


class LimiterCanonicalBoundsTests(unittest.TestCase):
    def test_backend_limiter_bounds_match_engine_ports(self) -> None:
        from dsp.manager import DSPManager

        self.assertEqual(DSPManager.LIMITER_ATTACK_MIN_MS, 0.25)
        self.assertEqual(DSPManager.LIMITER_ATTACK_MAX_MS, 20.0)
        self.assertEqual(DSPManager.LIMITER_RELEASE_MIN_MS, 0.25)
        self.assertEqual(DSPManager.LIMITER_RELEASE_MAX_MS, 20.0)
        self.assertEqual(DSPManager.LIMITER_LOOKAHEAD_MIN_MS, 0.1)
        self.assertEqual(DSPManager.LIMITER_LOOKAHEAD_MAX_MS, 20.0)
        self.assertEqual(DSPManager.LIMITER_DEFAULTS["params"]["releaseMs"], 20.0)

    def test_out_of_range_limiter_times_clamp_instead_of_reject(self) -> None:
        from dsp.manager import DSPManager

        mgr = DSPManager.__new__(DSPManager)
        normalized = mgr.normalize_effects_extras({
            "limiter": {"enabled": True, "params": {
                "thresholdDb": -1.0, "attackMs": 0.05,
                "releaseMs": 50.0, "lookaheadMs": 25.0,
                "stereoLinkPercent": 100.0,
            }},
        })
        params = normalized["limiter"]["params"]
        self.assertEqual(params["attackMs"], 0.25)
        self.assertEqual(params["releaseMs"], 20.0)
        self.assertEqual(params["lookaheadMs"], 20.0)

    def test_frontend_fallback_matches_canonical_release_default(self) -> None:
        from dsp.manager import DSPManager

        app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        matches = re.findall(r"releaseMs:\s*([0-9.]+)", app_js)
        self.assertTrue(matches, "no releaseMs fallback found in static/app.js")
        canonical = DSPManager.LIMITER_DEFAULTS["params"]["releaseMs"]
        for value in matches:
            self.assertEqual(
                float(value), float(canonical),
                f"frontend releaseMs fallback {value} != backend default {canonical}",
            )

    def test_app_js_version_is_bumped_pattern(self) -> None:
        index_html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertRegex(index_html, r"/app\.js\?v=\d+\.\d+\.\d+")


if __name__ == "__main__":
    unittest.main()
