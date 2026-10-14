#!/usr/bin/env python3
"""Radio fixed -> auto restore must not flap the hardware rate.

Regression for the .104 observation: auto -> fixed 88200 -> auto with radio
playing switched the sink four times instead of once. The committed radio
track still carried the fixed 88200 rate after the auto restore, so the
subwoofer link watcher derived a stale 88200 target, saw the correct 44100
helper as a mismatch, and ping-ponged recovery/replay transitions.

Covers the policy transition:
1. ``transition_sample_rate_policy`` refreshes the committed MPV track rate
   on an auto commit (mirrors the recovery/play paths).
The subwoofer-only watcher is removed. Mode-independent output authority
and drift observation are covered by test_runtime_watch.py.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playback.orchestration import PlaybackOrchestrator
from playback.transition import TransitionResult

RADIO_URL = "https://ice4.somafm.com/suburbsofgoa-128-aac"


def _overview() -> dict:
    return {
        "selected_output": {"supported_rates": [44100, 48000, 88200, 96000]},
        "current_output": {"supported_rates": [44100, 48000, 88200, 96000]},
        "output_mode": {"mode": "subwoofer-2.2"},
    }


class PolicyTransitionTrackRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_commit_refreshes_stale_radio_track_rate(self):
        live_track = {"source": "radio", "url": RADIO_URL, "sample_rate_hz": 88200}
        playback_state = SimpleNamespace(current_track_info=live_track)
        run = AsyncMock(return_value=TransitionResult(transition_id="auto", committed=True,
                                                     source="radio", target_rate=44100, state={}))
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
        run = AsyncMock(return_value=TransitionResult(transition_id="fixed", committed=True,
                                                     source="radio", target_rate=88200, state={}))
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


if __name__ == "__main__":
    unittest.main()
