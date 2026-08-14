#!/usr/bin/env python3
"""Focused regressions for SR-002 playback-context invalidation."""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


class FakePeakMonitor:
    def __init__(self):
        self.restarts = 0
        self.relinks = 0

    async def restart(self):
        self.restarts += 1

    async def relink(self):
        self.relinks += 1
        return True

    def snapshot(self):
        return {"available": True}


class FakeManager:
    async def broadcast(self, _message):
        return None


class PlaybackTransitionGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        names = (
            "peak_monitor", "manager", "peak_monitor_transition_lock",
            "peak_monitor_playback_armed", "peak_monitor_context_signature",
            "playback_transition_epoch", "current_track_info",
            "_wait_for_samplerate_alignment",
            "dsp_manager", "player_instance", "dsp_runtime",
            "_wait_for_player_current_file",
        )
        self.originals = {name: (getattr(main.runtime, name) if hasattr(main.runtime, name) else getattr(main, name)) for name in names}
        self._sync_preset_original = main.dsp_orchestrator.sync_preset_for_playback_samplerate
        self.monitor = FakePeakMonitor()
        main.runtime.peak_monitor = self.monitor
        main.manager = FakeManager()
        main.runtime.peak_monitor_transition_lock = asyncio.Lock()
        main.playback_transition_epoch = 4
        main.current_track_info = {
            "id": "local-track", "source": "local", "url": "/music/local.flac"
        }
        main._wait_for_samplerate_alignment = lambda _rate: async_value(True)
        main.dsp_orchestrator.sync_preset_for_playback_samplerate = lambda **_kwargs: async_value(None)
        main.dsp_manager = object()
        main.runtime.player_instance = type("Player", (), {"_running": True})()

    async def asyncTearDown(self):
        for name, value in self.originals.items():
            setattr(main.runtime if hasattr(main.runtime, name) else main, name, value)
        main.dsp_orchestrator.sync_preset_for_playback_samplerate = self._sync_preset_original

    async def test_stale_radio_callback_cannot_apply_after_local_handoff(self):
        main.runtime.peak_monitor_playback_armed = False
        main.runtime.peak_monitor_context_signature = "player:radio:https://radio.example/stream"

        await main.sync_peak_monitor_for_playback_state(
            {"current_file": "https://radio.example/stream", "paused": False, "ended": False},
            transition_generation=2,
        )

        self.assertEqual(self.monitor.restarts, 0)
        self.assertEqual(self.monitor.relinks, 0)
        self.assertEqual(main.runtime.peak_monitor_context_signature, "player:radio:https://radio.example/stream")

    async def test_genuine_context_change_keeps_full_restart(self):
        main.runtime.peak_monitor_playback_armed = True
        main.runtime.peak_monitor_context_signature = "player:radio:https://radio.example/stream"

        await main.sync_peak_monitor_for_playback_state(
            {"current_file": "/music/local.flac", "paused": False, "ended": False},
            transition_generation=4,
        )

        self.assertEqual(self.monitor.restarts, 1)
        self.assertEqual(self.monitor.relinks, 0)
        self.assertEqual(main.runtime.peak_monitor_context_signature, "player:local:/music/local.flac")

    async def test_local_to_radio_uses_committed_radio_context(self):
        main.current_track_info = {
            "id": "radio-station", "source": "radio", "url": "https://radio.example/stream"
        }
        main.runtime.peak_monitor_playback_armed = True
        main.runtime.peak_monitor_context_signature = "player:local:/music/local.flac"

        await main.sync_peak_monitor_for_playback_state(
            {"current_file": "https://radio.example/stream", "paused": False, "ended": False},
            transition_generation=4,
        )

        self.assertEqual(self.monitor.restarts, 1)
        self.assertEqual(main.runtime.peak_monitor_context_signature, "player:radio:https://radio.example/stream")

    async def test_same_source_resume_keeps_relink_optimization(self):
        main.runtime.peak_monitor_playback_armed = False
        main.runtime.peak_monitor_context_signature = "player:local:/music/local.flac"

        await main.sync_peak_monitor_for_playback_state(
            {"current_file": "/music/local.flac", "paused": False, "ended": False},
            transition_generation=4,
        )

        self.assertEqual(self.monitor.relinks, 1)
        self.assertEqual(self.monitor.restarts, 0)

    async def test_deferred_subwoofer_repair_is_coordinator_owned(self):
        # The watcher no longer has a direct helper-sync callback to invalidate;
        # its only allowed action is a Coordinator recovery request.
        self.assertFalse(
            hasattr(main, "_sync_dsp_runtime_after_playback_transition"),
            "direct deferred helper sync must be removed in favor of Coordinator recovery",
        )

    def test_legacy_delayed_samplerate_recovery_path_is_removed(self):
        self.assertFalse(hasattr(main, "_maybe_recover_samplerate_mismatch"))


async def async_value(value):
    return value


if __name__ == "__main__":
    unittest.main()
