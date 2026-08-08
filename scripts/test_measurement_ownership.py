#!/usr/bin/env python3
"""Focused regressions for Measurement ownership of the playback graph."""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import measurement_session


class MeasurementOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._originals = {
            name: getattr(main, name)
            for name in (
                "measurement_sr_session",
                "playback_transition_coordinator",
                "player_instance",
                "current_track_info",
                "subwoofer_runtime",
                "_is_measurement_window_open",
                "_run_coordinated_transition",
                "_recovery_context_is_valid",
                "_coordinator_target_rate",
                "_coordinator_rate_change",
                "_coordinator_commit_context_id",
                "_playback_graph_diagnosis",
                "_observe_playback_samplerate_drift",
                "get_audio_output_overview",
                "get_samplerate_status",
            )
        }
        main._is_measurement_window_open = lambda: False

    async def asyncTearDown(self):
        for name, value in self._originals.items():
            setattr(main, name, value)

    async def test_entry_in_progress_owns_graph_before_session_becomes_active(self):
        session = main.MeasurementSampleRateSession()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def run_transition(request):
            self.assertEqual(request.operation, "measurement-entry")
            self.assertTrue(session.entry_in_progress)
            self.assertTrue(session.owns_audio_graph)
            self.assertFalse(session.active)
            entered.set()
            await release.wait()
            return SimpleNamespace(committed=True, target_rate=request.target_rate)

        context = {
            "source": "local",
            "target_url": "/music/a.flac",
            "target_track": {"source": "local", "url": "/music/a.flac"},
            "should_play": True,
        }
        with patch.object(main, "measurement_sr_session", session), patch.object(
            main, "get_samplerate_status", return_value={"force_rate": 44100}
        ), patch.object(
            main, "_coordinator_current_playback_context", new=AsyncMock(return_value=context)
        ), patch.object(
            measurement_session, "_capture_playback_state_before_measurement"
        ), patch.object(main, "_run_coordinated_transition", new=run_transition):
            task = asyncio.create_task(session.start(48000))
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            self.assertTrue(session.owns_audio_graph)
            self.assertFalse(session.active)
            release.set()
            await task

        self.assertTrue(session.active)
        self.assertFalse(session.entry_in_progress)
        self.assertTrue(session.owns_audio_graph)
        session.active = False

    async def test_direct_recovery_is_blocked_during_entry_and_active_ownership(self):
        track = {"source": "local", "url": "/music/a.flac", "sample_rate_hz": 44100}
        run = AsyncMock()
        coordinator = SimpleNamespace(
            transition_active=False,
            transition_blocked=False,
            gate=SimpleNamespace(closed=False, failure_latched=False),
        )

        for entry_in_progress, active in ((True, False), (False, True)):
            session = main.MeasurementSampleRateSession()
            session.entry_in_progress = entry_in_progress
            session.active = active
            with patch.object(main, "measurement_sr_session", session), patch.object(
                main, "playback_transition_coordinator", coordinator
            ), patch.object(main, "_run_coordinated_transition", run):
                await main._request_coordinated_recovery(track, "measurement-ownership-test")
            self.assertFalse(coordinator.gate.closed)

        run.assert_not_awaited()

    async def test_subwoofer_watcher_skips_tick_while_measurement_owns_graph(self):
        session = main.MeasurementSampleRateSession()
        session.active = True
        session.measurement_rate = 48000
        main.current_track_info = {
            "source": "local",
            "url": "/music/a.flac",
            "sample_rate_hz": 44100,
        }
        main.subwoofer_runtime = object()
        observe_drift = AsyncMock()
        diagnose = AsyncMock()
        recovery = AsyncMock()
        sleep_calls = 0

        async def one_tick_then_cancel(_delay):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls == 1:
                return
            raise asyncio.CancelledError

        with patch.object(main, "measurement_sr_session", session), patch.object(
            main, "_observe_playback_samplerate_drift", observe_drift
        ), patch.object(main, "_playback_graph_diagnosis", diagnose), patch.object(
            main, "_request_coordinated_recovery", recovery
        ), patch.object(main, "asyncio") as asyncio_module:
            asyncio_module.sleep = one_tick_then_cancel
            task = asyncio.create_task(main._subwoofer_runtime_link_watch_loop())
            with self.assertRaises(asyncio.CancelledError):
                await task

        observe_drift.assert_not_awaited()
        diagnose.assert_not_awaited()
        recovery.assert_not_awaited()

    async def test_second_sweep_reconciles_link_loss_without_playback_recovery(self):
        session = main.MeasurementSampleRateSession()
        session.active = True
        session.measurement_rate = 48000
        coordinator = SimpleNamespace(
            transition_active=False,
            transition_blocked=False,
            gate=SimpleNamespace(closed=False, failure_latched=False),
            reconcile_measurement_session=AsyncMock(return_value={
                "committed": True,
                "graph_complete": True,
                "stable_readbacks": 2,
            }),
        )
        complete = {"links_complete": True, "signature": "complete"}
        link_loss = {
            "mode": "subwoofer-2.2",
            "ee_ports": True,
            "helper_ports": True,
            "helper_active": True,
            "helper_rate": 48000,
            "helper_rate_matches": True,
            "direct_ee_to_hw_present": False,
            "links_complete": False,
            "signature": "missing-ee-helper-fl-fr",
        }
        diagnosis = AsyncMock(side_effect=[complete, link_loss])
        with patch.object(main, "measurement_sr_session", session), patch.object(
            main, "playback_transition_coordinator", coordinator
        ), patch.object(
            main, "get_samplerate_status", return_value={"active_rate": 48000, "force_rate": 48000}
        ), patch.object(main, "_playback_graph_diagnosis", diagnosis), patch.object(
            main, "measurement_store", None
        ):
            await measurement_session._measurement_entry_preflight(48000)
            await measurement_session._measurement_entry_preflight(48000)

        coordinator.reconcile_measurement_session.assert_awaited_once_with(
            target_rate=48000,
            initial_graph=link_loss,
        )
        self.assertTrue(session.owns_audio_graph)

    async def test_recovery_is_allowed_after_measurement_release(self):
        session = main.MeasurementSampleRateSession()
        class Coordinator:
            transition_active = False

            async def run_recovery(self, **kwargs):
                if await kwargs["validate"]():
                    return await kwargs["execute"]()
                return None

        coordinator = Coordinator()
        player = SimpleNamespace(state={
            "current_file": "/music/a.flac",
            "playing": True,
            "paused": False,
            "ended": False,
        })
        track = {"source": "local", "url": "/music/a.flac", "sample_rate_hz": 44100}
        run = AsyncMock(return_value=SimpleNamespace(committed=True, target_rate=44100))
        with patch.object(main, "measurement_sr_session", session), patch.object(
            main, "playback_transition_coordinator", coordinator
        ), patch.object(main, "player_instance", player), patch.object(
            main, "current_track_info", dict(track)
        ), patch.object(main, "_coordinator_target_rate", return_value=44100), patch.object(
            main, "_coordinator_rate_change", return_value=False
        ), patch.object(main, "_coordinator_commit_context_id", return_value="tr-after-release"), patch.object(
            main, "_recovery_context_is_valid", new=AsyncMock(return_value=True)
        ), patch.object(main, "_run_coordinated_transition", run):
            await main._request_coordinated_recovery(track, "post-measurement-watcher")

        run.assert_awaited_once()
        self.assertFalse(session.owns_audio_graph)


if __name__ == "__main__":
    unittest.main()
