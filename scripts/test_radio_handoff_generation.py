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
            "playback_transition_generation", "current_track_info",
            "_resolve_expected_playback_samplerate", "_wait_for_samplerate_alignment",
            "_sync_easyeffects_preset_for_playback_samplerate",
            "_ensure_stereo_easyeffects_output_graph",
            "easyeffects_manager", "player_instance", "subwoofer_runtime",
            "_wait_for_player_current_file",
        )
        self.originals = {name: getattr(main, name) for name in names}
        self.monitor = FakePeakMonitor()
        main.peak_monitor = self.monitor
        main.manager = FakeManager()
        main.peak_monitor_transition_lock = asyncio.Lock()
        main.playback_transition_generation = 4
        main.current_track_info = {
            "id": "local-track", "source": "local", "url": "/music/local.flac"
        }
        self.resolved_sources = []

        async def resolve_rate(source, prefer_live_radio_rate=False):
            self.resolved_sources.append(source)
            return 44_100 if source == "radio" else 48_000

        main._resolve_expected_playback_samplerate = resolve_rate
        main._wait_for_samplerate_alignment = lambda _rate: async_value(True)
        main._sync_easyeffects_preset_for_playback_samplerate = lambda **_kwargs: async_value(None)
        main._ensure_stereo_easyeffects_output_graph = lambda: async_value(None)
        main.easyeffects_manager = object()
        main.player_instance = type("Player", (), {"_running": True})()

    async def asyncTearDown(self):
        for name, value in self.originals.items():
            setattr(main, name, value)

    async def test_stale_radio_callback_cannot_apply_after_local_handoff(self):
        main.peak_monitor_playback_armed = False
        main.peak_monitor_context_signature = "player:radio:https://radio.example/stream"

        await main.sync_peak_monitor_for_playback_state(
            {"current_file": "https://radio.example/stream", "paused": False, "ended": False},
            transition_generation=2,
        )

        self.assertEqual(self.monitor.restarts, 0)
        self.assertEqual(self.monitor.relinks, 0)
        self.assertEqual(main.peak_monitor_context_signature, "player:radio:https://radio.example/stream")

    async def test_genuine_context_change_keeps_full_restart(self):
        main.peak_monitor_playback_armed = True
        main.peak_monitor_context_signature = "player:radio:https://radio.example/stream"

        await main.sync_peak_monitor_for_playback_state(
            {"current_file": "/music/local.flac", "paused": False, "ended": False},
            transition_generation=4,
        )

        self.assertEqual(self.monitor.restarts, 1)
        self.assertEqual(self.monitor.relinks, 0)
        self.assertEqual(main.peak_monitor_context_signature, "player:local:/music/local.flac")
        self.assertEqual(self.resolved_sources, [], "peak monitor must not resolve or mutate rate")

    async def test_local_to_radio_uses_committed_radio_context(self):
        main.current_track_info = {
            "id": "radio-station", "source": "radio", "url": "https://radio.example/stream"
        }
        main.peak_monitor_playback_armed = True
        main.peak_monitor_context_signature = "player:local:/music/local.flac"

        await main.sync_peak_monitor_for_playback_state(
            {"current_file": "https://radio.example/stream", "paused": False, "ended": False},
            transition_generation=4,
        )

        self.assertEqual(self.monitor.restarts, 1)
        self.assertEqual(main.peak_monitor_context_signature, "player:radio:https://radio.example/stream")
        self.assertEqual(self.resolved_sources, [], "peak monitor must not resolve or mutate rate")

    async def test_same_source_resume_keeps_relink_optimization(self):
        main.peak_monitor_playback_armed = False
        main.peak_monitor_context_signature = "player:local:/music/local.flac"

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
            hasattr(main, "_sync_subwoofer_runtime_after_playback_transition"),
            "direct deferred helper sync must be removed in favor of Coordinator recovery",
        )

    def test_legacy_delayed_samplerate_recovery_path_is_removed(self):
        self.assertFalse(hasattr(main, "_maybe_recover_samplerate_mismatch"))


async def async_value(value):
    return value


if __name__ == "__main__":
    unittest.main()
