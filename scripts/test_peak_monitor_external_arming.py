# SPDX-License-Identifier: AGPL-3.0-only
"""Focused tests for provider-neutral external-renderer peak/VU arming.

Verifies the PeakMonitorCoordinator arms/stops the capture through the shared
external-renderer activity abstraction for Spotify and Qobuz, and that TIDAL
(an MPV source) keeps arming through the native playback-state path.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dsp.peak_monitor import PeakMonitorCoordinator, PeakMonitorCoordinatorDeps


class FakePeakMonitor:
    def __init__(self):
        self.restarts = 0
        self.stops = 0
        self.relinks = 0

    async def restart(self):
        self.restarts += 1

    async def stop(self):
        self.stops += 1

    async def relink(self):
        self.relinks += 1
        return True

    def snapshot(self):
        return {"available": True}


async def _state(state):
    return state


def make_coordinator(*, player_state, external_states, track_info=None):
    monitor = FakePeakMonitor()

    async def broadcast(_message):
        return None

    deps = PeakMonitorCoordinatorDeps(
        get_peak_monitor=lambda: monitor,
        get_player_state=lambda: player_state,
        get_current_track_info=lambda: track_info or {},
        broadcast=broadcast,
        get_spotify_ui_state=lambda: _state(external_states.get("spotify", {})),
        get_qobuz_ui_state=lambda: _state(external_states.get("qobuz", {})),
        get_audio_source_overview=lambda: {},
        capture_transition_epoch=lambda: None,
        transition_context_is_current=lambda _generation: True,
        transition_is_active=lambda: False,
        sleep=lambda _delay: asyncio.sleep(0),
    )
    return PeakMonitorCoordinator(deps), monitor


class ExternalRendererArmingTests(unittest.IsolatedAsyncioTestCase):
    async def test_qobuz_playing_arms(self):
        coordinator, monitor = make_coordinator(
            player_state={}, external_states={"qobuz": {"available": True, "status": "Playing"}},
        )
        await coordinator.sync_qobuz_state({"available": True, "status": "Playing"})
        self.assertTrue(coordinator.armed)
        self.assertEqual(coordinator.signature, "qobuz:playing")
        self.assertEqual(monitor.restarts, 1)

    async def test_qobuz_paused_never_arms_from_scratch(self):
        coordinator, monitor = make_coordinator(
            player_state={}, external_states={"qobuz": {"available": True, "status": "Paused"}},
        )
        await coordinator.sync_qobuz_state({"available": True, "status": "Paused"})
        self.assertFalse(coordinator.armed)
        self.assertEqual(monitor.restarts, 0)

    async def test_qobuz_paused_stops_after_playing(self):
        coordinator, monitor = make_coordinator(
            player_state={}, external_states={"qobuz": {"available": True, "status": "Paused"}},
        )
        await coordinator.sync_qobuz_state({"available": True, "status": "Playing"})
        self.assertTrue(coordinator.armed)
        await coordinator.sync_qobuz_state({"available": True, "status": "Paused"})
        self.assertFalse(coordinator.armed)
        self.assertEqual(monitor.stops, 1)

    async def test_spotify_playing_arms(self):
        coordinator, monitor = make_coordinator(
            player_state={}, external_states={"spotify": {"available": True, "status": "Playing"}},
        )
        await coordinator.sync_spotify_state({"available": True, "status": "Playing"})
        self.assertTrue(coordinator.armed)
        self.assertEqual(coordinator.signature, "spotify:playing")
        self.assertEqual(monitor.restarts, 1)

    async def test_tidal_playing_arms_via_playback_state(self):
        # TIDAL rides the MPV engine; it must arm through the native
        # playback-state path just like local/radio.
        coordinator, monitor = make_coordinator(
            player_state={},
            external_states={},
            track_info={"source": "tidal", "url": "https://stream.example/tidal"},
        )
        await coordinator.sync_playback_state(
            {"current_file": "https://stream.example/tidal", "paused": False, "ended": False},
        )
        self.assertTrue(coordinator.armed)
        self.assertEqual(coordinator.signature, "player:tidal:https://stream.example/tidal")
        self.assertEqual(monitor.restarts, 1)


if __name__ == "__main__":
    unittest.main()
