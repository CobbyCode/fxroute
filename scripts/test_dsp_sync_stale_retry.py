#!/usr/bin/env python3
"""Regression: output-switch DSP sync retried after the rate settles.

When an output switch (or same-mode output-mode change) is suppressed by the
native-DSP stale guard -- the caller's overview still carries the old card's
rate while the graph is pinned on another card's rate -- the graph would stay
linked to the previous card forever.  ``sync_runtime(retry_on_stale=True)``
schedules a bounded background retry that re-attempts the DSP sync once the
sink settles on the authoritative rate.

Scenario under test:
  sink pinned at 96 kHz, user switches to the 44.1 kHz card
  -> first sync suppressed (stale)
  -> rate settles back on 96 kHz
  -> retry re-syncs the DSP with fresh live state
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dsp.orchestration import DspOrchestrator


class _FakeDspRuntime:
    """Minimal native DSP runtime facade; records sync calls."""

    def __init__(self) -> None:
        self.sync_calls = []

    async def sync(self, overview: dict) -> None:
        self.sync_calls.append(dict(overview))


class _FakeMeasurementSrSession:
    """Measurement sample-rate session with a usable asyncio lock."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()


class _FakeDeps:
    """Dependency bag for DspOrchestrator with mutable live state.

    ``samplerate_status`` drives the authoritative/sink rate; overview reads
    only come from ``audio_overview`` and are static for the first call.
    """

    def __init__(self, samplerate_status: dict) -> None:
        self.samplerate_status = samplerate_status
        self.runtime = _FakeDspRuntime()
        self.scheduled = []
        self.overview = {
            "selected_output": {"active_rate": 44_100},
            "active_rate": 44_100,
            "output_mode": {"mode": "stereo"},
        }

    # -- injected services -------------------------------------------------

    def get_dsp_runtime(self):
        return self.runtime

    def get_dsp_manager(self):
        return None

    def get_audio_output_overview(self):
        return dict(self.overview)

    def get_samplerate_status(self):
        return dict(self.samplerate_status)

    def get_measurement_sr_session(self):
        return _FakeMeasurementSrSession()

    def get_player_instance(self):
        return None

    def get_current_track_info(self):
        return None

    def get_peak_monitor(self):
        return None

    def peak_monitor_playback_armed(self):
        return False

    def set_peak_monitor_context_signature(self, _value):
        return None

    def get_spotify_ui_state(self):
        raise AssertionError("not used by sync_runtime")

    def sync_peak_monitor_for_playback_state(self):
        raise AssertionError("not used by sync_runtime")

    def sync_peak_monitor_for_spotify_state(self):
        raise AssertionError("not used by sync_runtime")

    def load_dsp_preset(self):
        raise AssertionError("not used by sync_runtime")

    def broadcast(self):
        raise AssertionError("not used by sync_runtime")

    def wait_for_samplerate_alignment(self, *_args, **_kwargs):
        raise AssertionError("not used by sync_runtime")

    def wait_for_selected_output_effective_rate(self, *_args, **_kwargs):
        raise AssertionError("not used by sync_runtime")

    def measurement_audio_graph_owned(self):
        return False

    def observe_playback_samplerate_drift(self):
        raise AssertionError("not used by sync_runtime")

    def playback_transition_is_active(self):
        return False

    def coordinator_target_rate(self, *_args, **_kwargs):
        raise AssertionError("not used by sync_runtime")

    def playback_graph_diagnosis(self, *_args, **_kwargs):
        raise AssertionError("not used by sync_runtime")

    def request_coordinated_recovery(self, *_args, **_kwargs):
        raise AssertionError("not used by sync_runtime")

    def create_lifecycle_background_task(self, coro, *, name):
        self.scheduled.append((coro, name))
        return None

    def peak_monitor_restart_settle_ms(self):
        return 0.0

    async def sleep(self, _seconds):
        return None


def _make_orchestrator(samplerate_status: dict, *, stale_retry_deadline_s: float | None = None) -> tuple[DspOrchestrator, _FakeDeps]:
    deps = _FakeDeps(samplerate_status)
    kwargs = {} if stale_retry_deadline_s is None else {"stale_retry_deadline_s": stale_retry_deadline_s}
    return DspOrchestrator(deps, **kwargs), deps


class DspSyncStaleRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_suppression_schedules_retry_and_resyncs_after_settle(self) -> None:
        """Output switch during pinned playback: suppressed, then re-synced."""
        # Graph pinned on the old card (96 kHz) while the switched overview
        # still reports the 44.1 kHz target.
        orchestrator, deps = _make_orchestrator(
            {"force_rate": 96_000, "active_rate": 96_000},
        )
        stale_overview = dict(deps.overview)

        result = await orchestrator.sync_runtime(stale_overview, reason="output-selection", retry_on_stale=True)

        # First attempt suppressed; nothing was synced yet.
        self.assertIs(result, stale_overview)
        self.assertEqual(deps.runtime.sync_calls, [])
        self.assertEqual(len(deps.scheduled), 1)
        retry_coro, name = deps.scheduled[0]
        self.assertEqual(name, "dsp-sync-retry:output-selection")
        self.assertTrue(asyncio.iscoroutine(retry_coro))

        # The rate settles back on the authoritative value.
        deps.samplerate_status = {"force_rate": 96_000, "active_rate": 96_000}
        await retry_coro

        # The retry re-synced the DSP from fresh live state.
        self.assertEqual(len(deps.runtime.sync_calls), 1)
        synced_overview = deps.runtime.sync_calls[0]
        self.assertEqual(synced_overview["output_mode"]["effective_output_rate"], 96_000)

    async def test_no_retry_scheduled_without_opt_in(self) -> None:
        """Callers that do not opt in keep the old suppress-only behavior."""
        orchestrator, deps = _make_orchestrator(
            {"force_rate": 96_000, "active_rate": 96_000},
        )
        stale_overview = dict(deps.overview)

        await orchestrator.sync_runtime(stale_overview, reason="output-selection")

        self.assertEqual(deps.runtime.sync_calls, [])
        self.assertEqual(deps.scheduled, [])

    async def test_retry_gives_up_when_rate_never_settles(self) -> None:
        """The retry is bounded: it gives up instead of running forever."""
        orchestrator, deps = _make_orchestrator(
            {"force_rate": 96_000, "active_rate": 96_000},
            stale_retry_deadline_s=0.3,
        )
        stale_overview = dict(deps.overview)

        await orchestrator.sync_runtime(stale_overview, reason="output-selection", retry_on_stale=True)
        retry_coro, _name = deps.scheduled[0]

        # The sink stays stuck on the wrong rate for the whole deadline.
        deps.samplerate_status = {"force_rate": 96_000, "active_rate": 44_100}
        with self.assertLogs("dsp.orchestration", level="WARNING") as captured:
            await asyncio.wait_for(retry_coro, timeout=5)

        self.assertEqual(deps.runtime.sync_calls, [])
        self.assertTrue(
            any("gave up" in message for message in captured.output),
            captured.output,
        )


if __name__ == "__main__":
    unittest.main()
