# SPDX-License-Identifier: AGPL-3.0-only
"""Peak/VU arming for Bluetooth and external line sources.

The capture taps the post-DSP output, so it must run whenever a line
source is actively routed even though no app playback is playing: the
footer meter and peak badge read this snapshot in source modes.
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

    async def restart(self):
        self.restarts += 1

    async def stop(self):
        self.stops += 1

    def snapshot(self):
        return {"available": True}


async def _state(state):
    return state


def make_coordinator():
    monitor = FakePeakMonitor()

    async def broadcast(_message):
        return None

    deps = PeakMonitorCoordinatorDeps(
        get_peak_monitor=lambda: monitor,
        get_player_state=lambda: {},
        get_current_track_info=lambda: {},
        broadcast=broadcast,
        get_spotify_ui_state=lambda: _state({}),
        get_qobuz_ui_state=lambda: _state({}),
        get_audio_source_overview=lambda: {},
        capture_transition_epoch=lambda: None,
        transition_context_is_current=lambda _generation: True,
        transition_is_active=lambda: False,
        sleep=lambda _delay: asyncio.sleep(0),
    )
    return PeakMonitorCoordinator(deps), monitor


def external_overview(key="scarlett::pair:1-2"):
    return {
        "mode": "external-input",
        "selected_input": {"key": key, "label": "Scarlett — Input 1–2"},
        "current_input": {"key": key, "label": "Scarlett — Input 1–2"},
        "inputs": [{"key": key}],
        "bluetooth": {},
    }


class LineSourceArmingTests(unittest.IsolatedAsyncioTestCase):
    async def test_external_input_arms(self):
        coordinator, monitor = make_coordinator()
        await coordinator.sync_source_mode_state(external_overview())
        self.assertTrue(coordinator.armed)
        self.assertEqual(coordinator.signature, "external:scarlett::pair:1-2")
        self.assertEqual(monitor.restarts, 1)

    async def test_external_input_resync_is_idempotent(self):
        coordinator, monitor = make_coordinator()
        await coordinator.sync_source_mode_state(external_overview())
        await coordinator.sync_source_mode_state(external_overview())
        self.assertEqual(monitor.restarts, 1)

    async def test_external_input_change_rearms(self):
        coordinator, monitor = make_coordinator()
        await coordinator.sync_source_mode_state(external_overview("scarlett::pair:1-2"))
        await coordinator.sync_source_mode_state(external_overview("scarlett::pair:3-4"))
        self.assertEqual(coordinator.signature, "external:scarlett::pair:3-4")
        self.assertEqual(monitor.restarts, 2)

    async def test_external_input_without_selection_never_arms(self):
        coordinator, monitor = make_coordinator()
        await coordinator.sync_source_mode_state({
            "mode": "external-input",
            "selected_input": None,
            "current_input": None,
            "inputs": [],
            "bluetooth": {},
        })
        self.assertFalse(coordinator.armed)
        self.assertEqual(monitor.restarts, 0)

    async def test_leaving_external_input_stops(self):
        coordinator, monitor = make_coordinator()
        await coordinator.sync_source_mode_state(external_overview())
        self.assertTrue(coordinator.armed)
        await coordinator.sync_source_mode_state({
            "mode": "app-playback",
            "selected_input": None,
            "current_input": None,
            "inputs": [],
            "bluetooth": {},
        })
        self.assertFalse(coordinator.armed)
        self.assertIsNone(coordinator.signature)
        self.assertEqual(monitor.stops, 1)

    async def test_bluetooth_streaming_still_arms(self):
        coordinator, monitor = make_coordinator()
        await coordinator.sync_source_mode_state({
            "mode": "bluetooth-input",
            "selected_input": None,
            "current_input": None,
            "inputs": [],
            "bluetooth": {"state": "streaming", "connected_device": "ZENBOOK", "active_codec": "aac"},
        })
        self.assertTrue(coordinator.armed)
        self.assertEqual(coordinator.signature, "bluetooth:ZENBOOK:aac")
        self.assertEqual(monitor.restarts, 1)


if __name__ == "__main__":
    unittest.main()
