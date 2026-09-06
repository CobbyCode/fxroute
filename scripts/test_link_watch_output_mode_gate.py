#!/usr/bin/env python3

"""Stereo-mode link watcher skips the overview build; subwoofer modes keep it."""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dsp.orchestration import DspOrchestrationDeps, DspOrchestrator  # noqa: E402


def _watcher_deps(overrides) -> DspOrchestrationDeps:
    """Minimal DspOrchestrationDeps; unused members point at harmless stubs."""
    stub = lambda *a, **k: None  # noqa: E731

    async def _none():
        return None

    return DspOrchestrationDeps(
        get_dsp_runtime=overrides.get_dsp_runtime,
        get_dsp_manager=lambda: None,
        get_audio_output_overview=overrides.get_audio_output_overview,
        get_samplerate_status=lambda: {},
        get_measurement_sr_session=lambda: None,
        get_player_instance=lambda: None,
        get_current_track_info=lambda: {},
        get_peak_monitor=lambda: None,
        peak_monitor_playback_armed=lambda: False,
        set_peak_monitor_context_signature=stub,
        get_spotify_ui_state=_none,
        get_qobuz_ui_state=_none,
        sync_peak_monitor_for_playback_state=stub,
        sync_peak_monitor_for_spotify_state=stub,
        sync_peak_monitor_for_qobuz_state=stub,
        load_dsp_preset=stub,
        broadcast=stub,
        wait_for_samplerate_alignment=stub,
        wait_for_selected_output_effective_rate=stub,
        measurement_audio_graph_owned=lambda: False,
        observe_playback_samplerate_drift=overrides.observe_playback_samplerate_drift,
        playback_transition_is_active=lambda: True,
        coordinator_target_rate=lambda *_a, **_k: 44100,
        playback_graph_diagnosis=stub,
        request_coordinated_recovery=stub,
        create_lifecycle_background_task=stub,
        peak_monitor_restart_settle_ms=320.0,
        sleep=overrides.sleep,
        get_output_mode=overrides.get_output_mode,
    )


class LinkWatchOutputModeGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_stereo_tick_never_builds_the_overview(self):
        overview_calls = []

        def overview_builder():
            overview_calls.append(True)
            return {"output_mode": {"mode": "stereo"}}

        ticks = 0

        async def cancel_on_second_sleep(_delay):
            nonlocal ticks
            ticks += 1
            if ticks < 2:
                return
            raise asyncio.CancelledError

        deps = SimpleNamespace(
            sleep=cancel_on_second_sleep,
            get_dsp_runtime=lambda: object(),
            get_audio_output_overview=overview_builder,
            observe_playback_samplerate_drift=mock.AsyncMock(),
            get_output_mode=lambda: "stereo",
        )
        orchestrator = DspOrchestrator(_watcher_deps(deps))
        task = asyncio.create_task(orchestrator.runtime_link_watch_loop())
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(overview_calls, [], "stereo ticks must not build the overview")
        self.assertEqual(deps.observe_playback_samplerate_drift.await_count, 1)

    async def test_subwoofer_mode_still_builds_the_overview(self):
        overview_calls = []

        def overview_builder():
            overview_calls.append(True)
            return {"output_mode": {"mode": "subwoofer-2.2"}}

        ticks = 0

        async def cancel_on_second_sleep(_delay):
            nonlocal ticks
            ticks += 1
            if ticks < 2:
                return
            raise asyncio.CancelledError

        deps = SimpleNamespace(
            sleep=cancel_on_second_sleep,
            get_dsp_runtime=lambda: object(),
            get_audio_output_overview=overview_builder,
            observe_playback_samplerate_drift=mock.AsyncMock(),
            get_output_mode=lambda: "subwoofer-2.2",
        )
        orchestrator = DspOrchestrator(_watcher_deps(deps))
        task = asyncio.create_task(orchestrator.runtime_link_watch_loop())
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(overview_calls, [True], "subwoofer ticks must build the overview")

    async def test_missing_provider_keeps_legacy_overview_path(self):
        # Without the injected output-mode getter the loop must keep its
        # historical behavior (build overview, then decide on the mode).
        overview_calls = []

        def overview_builder():
            overview_calls.append(True)
            return {"output_mode": {"mode": "stereo"}}

        ticks = 0

        async def cancel_on_second_sleep(_delay):
            nonlocal ticks
            ticks += 1
            if ticks < 2:
                return
            raise asyncio.CancelledError

        deps = SimpleNamespace(
            sleep=cancel_on_second_sleep,
            get_dsp_runtime=lambda: object(),
            get_audio_output_overview=overview_builder,
            observe_playback_samplerate_drift=mock.AsyncMock(),
        )
        overrides = {k: getattr(deps, k) for k in ("sleep", "get_dsp_runtime", "get_audio_output_overview", "observe_playback_samplerate_drift")}
        orchestrator = DspOrchestrator(_watcher_deps(SimpleNamespace(**overrides, get_output_mode=None)))
        task = asyncio.create_task(orchestrator.runtime_link_watch_loop())
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(overview_calls, [True], "legacy path must keep building the overview")


if __name__ == "__main__":
    unittest.main()
