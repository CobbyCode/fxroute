#!/usr/bin/env python3
"""Radio fixed -> auto restore must not flap the hardware rate.

Regression for the .104 observation: auto -> fixed 88200 -> auto with radio
playing switched the sink four times instead of once. The committed radio
track still carried the fixed 88200 rate after the auto restore, so the
subwoofer link watcher derived a stale 88200 target, saw the correct 44100
helper as a mismatch, and ping-ponged recovery/replay transitions.

Covers both layers:
1. ``transition_sample_rate_policy`` refreshes the committed MPV track rate
   on an auto commit (mirrors the recovery/play paths).
2. The subwoofer link watcher defers a pure helper-rate mismatch with an
   otherwise intact topology to the drift observer (live-MPV authority)
   instead of requesting a rate-changing recovery.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dsp.orchestration import DspOrchestrator
from playback.orchestration import PlaybackOrchestrator

RADIO_URL = "https://ice4.somafm.com/suburbsofgoa-128-aac"


def _overview() -> dict:
    return {
        "selected_output": {"supported_rates": [44100, 48000, 88200, 96000]},
        "current_output": {"supported_rates": [44100, 48000, 88200, 96000]},
        "output_mode": {"mode": "subwoofer-2.2"},
    }


def _rate_only_mismatch_diagnosis() -> dict:
    links = {
        "fxroute_dsp_sink:monitor_FL -> fxroute_dsp:input_1": True,
        "fxroute_dsp_sink:monitor_FR -> fxroute_dsp:input_2": True,
        "fxroute_dsp:output_1 -> hw:playback_FL": True,
        "fxroute_dsp:output_2 -> hw:playback_FR": True,
        "fxroute_dsp:output_3 -> hw:playback_RL": True,
        "fxroute_dsp:output_4 -> hw:playback_RR": True,
    }
    return {
        "mode": "subwoofer-2.2",
        "output_key": "hw",
        "dsp_ports": True,
        "helper_ports": True,
        "helper_active": True,
        "helper_rate": 44100,
        "helper_rate_matches": False,
        "links": links,
        "source_links": {"mpv:output_FL -> fxroute_dsp_sink:playback_FL": True},
        "source_links_complete": True,
        "direct_source_to_hw_present": False,
        "links_complete": False,
        "bypass_only": False,
        "signature": "rate-only-mismatch",
    }


class PolicyTransitionTrackRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_commit_refreshes_stale_radio_track_rate(self):
        live_track = {"source": "radio", "url": RADIO_URL, "sample_rate_hz": 88200}
        playback_state = SimpleNamespace(current_track_info=live_track)
        run = AsyncMock(return_value=SimpleNamespace(committed=True, target_rate=44100))
        deps = SimpleNamespace(
            get_audio_output_overview=_overview,
            get_player_audio_samplerate=lambda: 44100,
            get_samplerate_status=lambda: {"active_rate": 88200, "force_rate": 88200},
            run_transition=run,
            get_playback_state=lambda: playback_state,
            sample_rate_policy_is_auto=lambda: True,
            get_player_queue_fields=lambda: {},
        )
        orchestrator = PlaybackOrchestrator(deps)
        context = {
            "source": "radio",
            "target_url": RADIO_URL,
            "target_track": dict(live_track),
            "should_play": True,
        }
        with patch.object(
            PlaybackOrchestrator, "current_playback_context", new=AsyncMock(return_value=context)
        ):
            await orchestrator.transition_sample_rate_policy(
                {"mode": "auto", "rate": None}, detail="test-auto-restore"
            )
        run.assert_awaited_once()
        self.assertEqual(live_track["sample_rate_hz"], 44100)

    async def test_fixed_commit_leaves_track_untouched(self):
        live_track = {"source": "radio", "url": RADIO_URL, "sample_rate_hz": 44100}
        playback_state = SimpleNamespace(current_track_info=live_track)
        run = AsyncMock(return_value=SimpleNamespace(committed=True, target_rate=88200))
        deps = SimpleNamespace(
            get_audio_output_overview=_overview,
            get_player_audio_samplerate=lambda: 44100,
            get_samplerate_status=lambda: {"active_rate": 44100, "force_rate": 44100},
            run_transition=run,
            get_playback_state=lambda: playback_state,
            sample_rate_policy_is_auto=lambda: False,
            get_player_queue_fields=lambda: {},
        )
        orchestrator = PlaybackOrchestrator(deps)
        context = {
            "source": "radio",
            "target_url": RADIO_URL,
            "target_track": dict(live_track),
            "should_play": True,
        }
        with patch.object(
            PlaybackOrchestrator, "current_playback_context", new=AsyncMock(return_value=context)
        ):
            await orchestrator.transition_sample_rate_policy(
                {"mode": "fixed", "rate": 88200}, detail="test-fixed-pin"
            )
        run.assert_awaited_once()
        self.assertEqual(live_track["sample_rate_hz"], 44100)


class LinkWatcherRateDeferralTests(unittest.IsolatedAsyncioTestCase):
    async def _run_one_tick(self, diagnosis: dict):
        recovery = AsyncMock()
        sleep_calls = 0

        async def one_tick_then_cancel(_delay):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls == 1:
                return
            raise asyncio.CancelledError

        deps = SimpleNamespace(
            measurement_audio_graph_owned=lambda: False,
            observe_playback_samplerate_drift=AsyncMock(),
            get_dsp_runtime=lambda: SimpleNamespace(sync_in_progress=False),
            get_audio_output_overview=_overview,
            playback_transition_is_active=lambda: False,
            get_current_track_info=lambda: {
                "source": "radio",
                "url": RADIO_URL,
                "sample_rate_hz": 88200,
            },
            coordinator_target_rate=lambda *args, **kwargs: 88200,
            playback_graph_diagnosis=AsyncMock(return_value=diagnosis),
            request_coordinated_recovery=recovery,
            sleep=one_tick_then_cancel,
        )
        orchestrator = DspOrchestrator(deps)
        task = asyncio.create_task(orchestrator.runtime_link_watch_loop())
        with self.assertRaises(asyncio.CancelledError):
            await task
        return recovery

    async def test_pure_rate_mismatch_defers_to_drift_observer(self):
        recovery = await self._run_one_tick(_rate_only_mismatch_diagnosis())
        recovery.assert_not_awaited()

    async def test_real_link_loss_still_requests_recovery(self):
        diagnosis = _rate_only_mismatch_diagnosis()
        diagnosis["links"] = dict(diagnosis["links"])
        diagnosis["links"]["fxroute_dsp:output_4 -> hw:playback_RR"] = False
        diagnosis["links_complete"] = False
        recovery = await self._run_one_tick(diagnosis)
        recovery.assert_awaited_once()

    def test_rate_only_predicate(self):
        self.assertTrue(
            DspOrchestrator._is_topology_complete_rate_only_mismatch(
                _rate_only_mismatch_diagnosis()
            )
        )
        missing = _rate_only_mismatch_diagnosis()
        missing["links"] = dict(missing["links"])
        missing["links"]["fxroute_dsp:output_4 -> hw:playback_RR"] = False
        self.assertFalse(DspOrchestrator._is_topology_complete_rate_only_mismatch(missing))
        external = _rate_only_mismatch_diagnosis()
        external["helper_rate_matches"] = True
        external["links_complete"] = True
        self.assertFalse(DspOrchestrator._is_topology_complete_rate_only_mismatch(external))


if __name__ == "__main__":
    unittest.main()
