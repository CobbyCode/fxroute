#!/usr/bin/env python3
"""Sample-rate fallback: native when supported, else the highest supported rate.

Covers the rate rule and the invariants that keep the DSP state current when
a source rate is not supported by the selected output:

- playable_rate: native when supported, else the highest supported rate below
  it (96 -> 48 kHz on a 48 kHz-only card, 384 -> 192 kHz on a 192 kHz device),
  else the lowest supported rate; an unknown capability never changes a rate
- a force-rate pin the output cannot run (qbzd pins its track rate itself) is
  neither honoured nor authoritative: the live sink rate decides
- the drift observer does not request endless recoveries for a source that
  PipeWire resamples to the fallback rate, and still repairs a real drift
- a DSP sync is never deferred waiting for an unplayable pin
- the idle stale-pin repair never chases an unplayable pin
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audio.samplerate as samplerate
from audio.drift import SamplerateDriftDependencies, SamplerateDriftObserver
from test_dsp_sync_stale_retry import _make_orchestrator

NOTEBOOK_RATES = [44100, 48000]


class _CapabilityCase(unittest.TestCase):
    def setUp(self):
        samplerate.remember_selected_output_rates(NOTEBOOK_RATES)
        self.addCleanup(samplerate.remember_selected_output_rates, None)


class PlayableRateTests(unittest.TestCase):
    def test_supported_rate_stays_native(self):
        for rate in (44100, 48000):
            self.assertEqual(samplerate.playable_rate(rate, NOTEBOOK_RATES), rate)
        self.assertEqual(samplerate.playable_rate(96000, [44100, 48000, 96000]), 96000)

    def test_unsupported_rate_falls_back_to_highest_supported_below(self):
        self.assertEqual(samplerate.playable_rate(96000, NOTEBOOK_RATES), 48000)
        self.assertEqual(samplerate.playable_rate(384000, [44100, 48000, 96000, 192000]), 192000)
        self.assertEqual(samplerate.playable_rate(88200, NOTEBOOK_RATES), 48000)

    def test_rate_below_every_supported_rate_uses_the_lowest(self):
        self.assertEqual(samplerate.playable_rate(22050, NOTEBOOK_RATES), 44100)

    def test_unknown_capability_never_changes_a_rate(self):
        samplerate.remember_selected_output_rates(None)
        self.assertEqual(samplerate.playable_rate(96000), 96000)
        self.assertEqual(samplerate.playable_rate(96000, []), 96000)
        self.assertIsNone(samplerate.playable_rate(None, NOTEBOOK_RATES))

    def test_remembered_capability_is_the_default(self):
        samplerate.remember_selected_output_rates(NOTEBOOK_RATES)
        self.addCleanup(samplerate.remember_selected_output_rates, None)
        self.assertEqual(samplerate.playable_rate(96000), 48000)
        self.assertEqual(samplerate.playback_target_rate(96000), 48000)
        # A fixed policy is fitted the same way.
        self.assertEqual(
            samplerate.playback_target_rate(44100, {"mode": "fixed", "rate": 96000}), 48000
        )


class RateAuthorityTests(_CapabilityCase):
    def test_unplayable_pin_is_not_honoured_or_authoritative(self):
        status = {"force_rate": 96000, "active_rate": 48000}
        self.assertIsNone(samplerate.honoured_force_rate(status))
        self.assertEqual(samplerate.authoritative_sample_rate(status), 48000)

    def test_unplayable_pin_without_sink_rate_falls_back_to_playable_rate(self):
        status = {"force_rate": 96000, "active_rate": None}
        self.assertEqual(samplerate.authoritative_sample_rate(status), 48000)

    def test_playable_pin_stays_authoritative(self):
        status = {"force_rate": 44100, "active_rate": 48000}
        self.assertEqual(samplerate.honoured_force_rate(status), 44100)
        self.assertEqual(samplerate.authoritative_sample_rate(status), 44100)

    def test_sink_running_at_an_unplayable_graph_rate_falls_back(self):
        status = {"force_rate": 96000, "active_rate": 96000}
        self.assertEqual(samplerate.authoritative_sample_rate(status), 48000)
        self.assertEqual(samplerate.authoritative_sample_rate({"force_rate": 0, "active_rate": 96000}), 48000)

    def test_native_sink_rate_wins_over_an_unplayable_pin(self):
        # Radio at 44.1 kHz while qbzd pinned 96 kHz: no deferral, no switch.
        status = {"force_rate": 96000, "active_rate": 44100}
        self.assertEqual(samplerate.authoritative_sample_rate(status), 44100)

    def test_no_pin_uses_the_sink_rate(self):
        self.assertEqual(samplerate.authoritative_sample_rate({"force_rate": 0, "active_rate": 48000}), 48000)
        self.assertIsNone(samplerate.honoured_force_rate({"force_rate": 0}))


def _drift_observer(status: dict, *, mpv_rate: int, track_rate: int):
    recovery = mock.AsyncMock()

    class Player:
        state = {"current_file": "/music/hires.flac", "ended": False}

    deps = SamplerateDriftDependencies(
        get_current_track_info=lambda: {"source": "local", "url": "/music/hires.flac"},
        get_player_instance=lambda: Player(),
        coordinator_source_rate=lambda *_args, **_kwargs: track_rate,
        get_player_audio_samplerate=lambda: mpv_rate,
        get_samplerate_status=lambda: dict(status),
        playback_transition_is_active=lambda: False,
        is_measurement_window_open=lambda: False,
        measurement_session_active=lambda: False,
        measurement_audio_graph_owned=lambda: False,
        request_coordinated_recovery=recovery,
    )
    return SamplerateDriftObserver(deps), recovery


class DriftObserverFallbackTests(_CapabilityCase):
    def _observe(self, observer, passes=3):
        for _ in range(passes):
            asyncio.run(observer.observe())

    def test_resampled_source_at_the_fallback_rate_is_healthy(self):
        # 96 kHz file on a 48 kHz card: the committed graph runs at 48 kHz.
        with mock.patch.object(samplerate, "load_sample_rate_policy", return_value={"mode": "auto"}):
            observer, recovery = _drift_observer(
                {"active_rate": 48000, "force_rate": 48000}, mpv_rate=96000, track_rate=96000,
            )
            self._observe(observer)
        recovery.assert_not_called()

    def test_unplayable_foreign_pin_on_the_right_graph_is_healthy(self):
        with mock.patch.object(samplerate, "load_sample_rate_policy", return_value={"mode": "auto"}):
            observer, recovery = _drift_observer(
                {"active_rate": 48000, "force_rate": 96000}, mpv_rate=96000, track_rate=96000,
            )
            self._observe(observer)
        recovery.assert_not_called()

    def test_graph_off_the_fallback_rate_still_recovers_once(self):
        with mock.patch.object(samplerate, "load_sample_rate_policy", return_value={"mode": "auto"}):
            observer, recovery = _drift_observer(
                {"active_rate": 44100, "force_rate": 44100}, mpv_rate=96000, track_rate=96000,
            )
            self._observe(observer, passes=2)
        recovery.assert_awaited_once()
        self.assertEqual(recovery.await_args.kwargs["diagnosis"]["expected_rate"], 48000)


class _LiveEngine:
    def __init__(self):
        self.sync_calls = []

    def snapshot(self):
        return {"active": True, "config": {"sample_rate": 48000}}

    async def sync(self, overview):
        self.sync_calls.append(dict(overview))


class DspSyncNeverDefersOnUnplayablePinTests(_CapabilityCase):
    def test_live_engine_syncs_at_the_sink_rate_despite_a_96_khz_pin(self):
        # qbzd pinned 96 kHz on a 48 kHz card; the helper clocks the sink at
        # 48 kHz.  A headroom change must reach the running engine.
        orchestrator, deps = _make_orchestrator({"force_rate": 96000, "active_rate": 48000})
        deps.runtime = _LiveEngine()
        deps.overview = {
            "selected_output": {"active_rate": 48000},
            "active_rate": 48000,
            "output_mode": {"mode": "stereo"},
        }
        asyncio.run(orchestrator.sync_runtime(reason="global-extras-update"))
        self.assertEqual(len(deps.runtime.sync_calls), 1)
        self.assertEqual(deps.runtime.sync_calls[0]["output_mode"]["effective_output_rate"], 48000)

    def test_restart_rebuilds_the_helper_at_the_sink_rate(self):
        # FXRoute restart with the 96 kHz pin still set and no helper running.
        orchestrator, deps = _make_orchestrator({"force_rate": 96000, "active_rate": 48000})
        overview = {
            "selected_output": {"active_rate": 48000},
            "active_rate": 48000,
            "output_mode": {"mode": "stereo"},
        }
        deps.overview = dict(overview)
        asyncio.run(orchestrator.sync_runtime(dict(overview), reason="startup"))
        self.assertEqual(len(deps.runtime.sync_calls), 1)
        self.assertEqual(deps.runtime.sync_calls[0]["output_mode"]["effective_output_rate"], 48000)

    def test_restart_while_the_graph_runs_at_the_unplayable_pin(self):
        # FXRoute restarted while qbzd kept playing: no helper clocks the
        # graph, so the sink node itself runs at the pinned 96 kHz (PipeWire
        # resamples to the card).  The helper must come back at 48 kHz.
        orchestrator, deps = _make_orchestrator({"force_rate": 96000, "active_rate": 96000})
        overview = {
            "selected_output": {"active_rate": 96000},
            "active_rate": 96000,
            "output_mode": {"mode": "stereo"},
        }
        deps.overview = dict(overview)
        asyncio.run(orchestrator.sync_runtime(dict(overview), reason="startup"))
        self.assertEqual(len(deps.runtime.sync_calls), 1)
        self.assertEqual(deps.runtime.sync_calls[0]["output_mode"]["effective_output_rate"], 48000)

    def test_idle_repair_ignores_an_unplayable_pin(self):
        orchestrator, deps = _make_orchestrator({"force_rate": 96000, "active_rate": 48000})
        deps.runtime = _LiveEngine()
        deps.get_current_force_rate = lambda: 96000
        with mock.patch.object(orchestrator, "recover_stale_helper_samplerate") as recover:
            asyncio.run(orchestrator._repair_idle_stale_pinned_rate(deps.runtime))
        recover.assert_not_called()


if __name__ == "__main__":
    unittest.main()
