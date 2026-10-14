#!/usr/bin/env python3
"""Mode-independent output authority and rate monitoring."""

import asyncio
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dsp.orchestration import DspOrchestrationDeps, DspOrchestrator


def _watcher_deps(overrides) -> DspOrchestrationDeps:
    """Minimal dependencies for the real runtime monitor."""
    stub = lambda *a, **k: None
    return DspOrchestrationDeps(
        get_dsp_runtime=overrides.get_dsp_runtime,
        get_audio_output_overview=overrides.get_audio_output_overview,
        get_samplerate_status=lambda: {},
        get_measurement_sr_session=lambda: None,
        get_player_instance=lambda: None,
        get_current_track_info=getattr(overrides, "get_current_track_info", lambda: {}),
        get_peak_monitor=lambda: None,
        peak_monitor_playback_armed=lambda: False,
        set_peak_monitor_context_signature=stub,
        get_spotify_ui_state=mock.AsyncMock(),
        get_qobuz_ui_state=mock.AsyncMock(),
        sync_peak_monitor_for_playback_state=mock.AsyncMock(),
        sync_peak_monitor_for_spotify_state=mock.AsyncMock(),
        sync_peak_monitor_for_qobuz_state=mock.AsyncMock(),
        wait_for_samplerate_alignment=mock.AsyncMock(),
        wait_for_selected_output_effective_rate=mock.AsyncMock(),
        measurement_audio_graph_owned=lambda: False,
        observe_playback_samplerate_drift=overrides.observe_playback_samplerate_drift,
        playback_transition_is_active=getattr(overrides, "playback_transition_is_active", lambda: False),
        playback_graph_diagnosis=mock.AsyncMock(return_value={"links_complete": False}),
        create_lifecycle_background_task=stub,
        peak_monitor_restart_settle_ms=320.0,
        sleep=overrides.sleep,
    )


class RuntimeWatchTests(unittest.IsolatedAsyncioTestCase):
    async def _tick(self, *, mode="stereo", track=None, owned=False, busy=False,
                    authority_failure=None, drift_failure=None):
        ticks = 0

        async def sleep(_delay):
            nonlocal ticks
            ticks += 1
            if ticks > 1:
                raise asyncio.CancelledError

        overview = mock.Mock(return_value={"output_mode": {"mode": mode}})
        drift = mock.AsyncMock(side_effect=drift_failure)
        runtime = SimpleNamespace(sync_in_progress=busy)
        deps = _watcher_deps(SimpleNamespace(
            get_dsp_runtime=lambda: runtime, get_audio_output_overview=overview,
            get_current_track_info=lambda: track or {}, get_output_mode=lambda: mode,
            playback_transition_is_active=lambda: busy,
            sleep=sleep, observe_playback_samplerate_drift=drift))
        deps = replace(deps, measurement_audio_graph_owned=lambda: owned)
        orchestrator = DspOrchestrator(deps)
        authority = mock.AsyncMock(side_effect=authority_failure)
        repair = mock.AsyncMock()
        with mock.patch.object(orchestrator, "reconcile_output_authority", authority), \
                mock.patch.object(orchestrator, "_repair_idle_stale_pinned_rate", repair):
            with self.assertRaises(asyncio.CancelledError):
                await orchestrator.runtime_link_watch_loop()
        return authority, drift, repair, overview, deps

    async def test_playing_tick_uses_only_mode_independent_authority_and_drift(self):
        for mode in ("stereo", "subwoofer-2.2", "surround"):
            with self.subTest(mode=mode):
                authority, drift, repair, overview, deps = await self._tick(
                    mode=mode, track={"source": "radio", "sample_rate_hz": 88200})
                authority.assert_awaited_once()
                drift.assert_awaited_once()
                repair.assert_not_awaited()
                overview.assert_not_called()

    async def test_idle_rate_repair_runs_in_every_mode_without_overview(self):
        for mode in ("stereo", "subwoofer-2.2", "surround"):
            with self.subTest(mode=mode):
                authority, drift, repair, overview, _ = await self._tick(mode=mode)
                authority.assert_awaited_once()
                drift.assert_awaited_once()
                repair.assert_awaited_once()
                overview.assert_not_called()

    async def test_measurement_ownership_skips_every_background_mutation(self):
        authority, drift, repair, overview, _ = await self._tick(owned=True)
        authority.assert_not_awaited()
        drift.assert_not_awaited()
        repair.assert_not_awaited()
        overview.assert_not_called()

    async def test_busy_runtime_does_not_attempt_idle_rate_repair(self):
        _, _, repair, _, _ = await self._tick(busy=True)
        repair.assert_not_awaited()

    async def test_authority_failure_does_not_suppress_rate_monitoring(self):
        _, drift, repair, _, _ = await self._tick(authority_failure=RuntimeError("default unavailable"))
        drift.assert_awaited_once()
        repair.assert_awaited_once()

    async def test_drift_failure_does_not_suppress_idle_rate_repair(self):
        _, _, repair, _, _ = await self._tick(drift_failure=RuntimeError("MPV unavailable"))
        repair.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
