#!/usr/bin/env python3
"""SR-001 contracts after samplerate recovery became coordinator-owned."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


class _CoordinatorDouble:
    def __init__(self, *, active: bool = False, target_rate: int | None = None):
        self.transition_active = active
        self.target_rate = target_rate
        self.requests = []


class CoordinatorRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_radio_recovery_submits_one_coordinator_request(self):
        coordinator = _CoordinatorDouble(target_rate=48000)

        async def run(request):
            coordinator.requests.append(request)
            return SimpleNamespace(target_rate=48000)

        track = {"source": "radio", "url": "https://radio.example/live", "sample_rate_hz": 44100}
        with patch.object(main, "playback_transition_coordinator", coordinator), patch.object(
            main, "_run_coordinated_transition", run
        ):
            await main._request_coordinated_recovery(track, "status-drift-repair")

        self.assertEqual(len(coordinator.requests), 1)
        request = coordinator.requests[0]
        self.assertEqual(request.operation, "recovery")
        self.assertEqual(request.source, "radio")
        self.assertTrue(request.rate_change)
        self.assertEqual(track["sample_rate_hz"], 48000)

    async def test_recovery_does_not_mutate_while_transition_is_active(self):
        coordinator = _CoordinatorDouble(active=True)
        with patch.object(main, "playback_transition_coordinator", coordinator), patch.object(
            main, "_run_coordinated_transition", AsyncMock()
        ) as run:
            await main._request_coordinated_recovery(
                {"source": "local", "url": "/music/a.flac", "sample_rate_hz": 44100},
                "status-drift-repair",
            )
        run.assert_not_awaited()

    async def test_old_delayed_recovery_path_is_removed(self):
        self.assertFalse(hasattr(main, "_maybe_recover_samplerate_mismatch"))

    async def test_spotify_recovery_uses_same_coordinator_submission(self):
        coordinator = _CoordinatorDouble()
        recovery = AsyncMock()
        with patch.object(main, "_request_coordinated_recovery", recovery), patch.object(
            main, "get_spotify_ui_state",
            AsyncMock(return_value={"status": "Playing", "title": "Track", "artist": "Artist"}),
        ), patch.object(main.asyncio, "sleep", new=AsyncMock()):
            await main._maybe_recover_spotify_samplerate_mismatch(delay_ms=0, reason="watcher")
        recovery.assert_awaited_once()
        request_track = recovery.await_args.args[0]
        self.assertEqual(request_track["source"], "spotify")

    async def test_recovery_failure_is_latched_by_coordinator(self):
        from playback_transition import PlaybackTransitionCoordinator, PlaybackTransitionFailure, TransitionRequest

        class Runtime:
            def __init__(self):
                self.muted = False
                self.fail = True
                self.events = []

            async def read_hardware_mute(self):
                return self.muted

            async def set_hardware_mute(self, muted, transition_id):
                self.muted = muted

            async def read_transition_snapshot(self, request):
                return {"active_rate": 44100, "force_rate": 44100}

            async def quiet_old_source(self, request):
                self.events.append("quiet")

            async def resolve_target_rate(self, request):
                return request.target_rate

            async def establish_target_rate(self, request):
                self.events.append("rate")
                if self.fail:
                    raise RuntimeError("rate mismatch")

            async def establish_effects_and_helper(self, request):
                self.events.append("graph")

            async def prepare_target_source(self, request):
                self.events.append("prepare")

            async def start_target_source(self, request):
                self.events.append("start")

            async def set_source_volume(self, volume, transition_id):
                self.events.append(f"volume:{volume}")

            async def verify_committed_transition(self, request):
                return {"committed": True}

            async def pause_source_after_failure(self, request):
                self.events.append("pause")

        runtime = Runtime()
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
        request = TransitionRequest(
            operation="recovery", source="local", target_rate=48000,
            target_url="/music/a.flac", should_play=True,
            rate_change=True, reload_source=True,
        )
        with self.assertRaises(PlaybackTransitionFailure):
            await coordinator.execute(request)
        self.assertTrue(coordinator.gate.failure_latched)
        self.assertTrue(runtime.muted)
        self.assertIn("pause", runtime.events)

    async def test_recovery_requests_have_no_parallel_direct_mutation(self):
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text()
        start = source.index("async def _request_coordinated_recovery")
        end = source.index("def _transition_error_http", start)
        body = source[start:end]
        self.assertNotIn("_set_pipewire_force_rate", body)
        self.assertNotIn("_sync_subwoofer_runtime", body)
        self.assertIn("_run_coordinated_transition", body)


if __name__ == "__main__":
    unittest.main()
