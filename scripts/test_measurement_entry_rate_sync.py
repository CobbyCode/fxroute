#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only

"""Regression: measurement-entry must rebuild the DSP helper at the target rate.

Live bug (.104, sweep start): from a 96 kHz playback state, a measurement-entry
to 48 kHz failed at the ``effects-helper-links`` transition stage with
"Coordinator effects/helper graph did not reach the canonical topology".

Root cause: ``establish_effects_and_helper`` handed the native-DSP sync the live
audio overview, whose effective rate still reflected the pre-rebuild hardware
rate (96 kHz).  The sync's stale-check then compared requested(96000) against
the already-established authoritative rate (48000) and suppressed the helper
rebuild that is precisely what moves the device to 48 kHz.

Fix: for ``measurement-entry``/``measurement-restore`` the sync token carries the
transition's own target rate (``audio_output_overview_with_effective_rate``), so
the stale-check sees requested(48000) == authoritative(48000) and the helper is
rebuilt at 48 kHz.  This test drives the real native-DSP sync through the real
orchestrator stage to prove the helper is rebuilt exactly once at the target.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)

import audio.samplerate as samplerate
import dsp.orchestration as dsp_orchestration
import playback.orchestration as playback_orchestration

OUTPUT_KEY = "alsa_output.usb-BEHRINGER_UMC204HD_192k-00.analog-surround-40"


class _RecordingDspRuntime:
    """Minimal native DSP runtime facade recording every helper rebuild."""

    def __init__(self) -> None:
        self.sync_calls: list[dict] = []

    async def sync(self, overview: dict) -> None:
        self.sync_calls.append(dict(overview))


class _FakeMeasurementSrSession:
    def __init__(self) -> None:
        self.lock = __import__("asyncio").Lock()


class _DspDeps:
    """Dependency bag for the real DspOrchestrator under test."""

    def __init__(self, samplerate_status: dict, live_overview: dict, runtime: _RecordingDspRuntime) -> None:
        self.samplerate_status = samplerate_status
        self.live_overview = live_overview
        self.runtime = runtime

    def get_dsp_runtime(self):
        return self.runtime

    def get_dsp_manager(self):
        return None

    def get_audio_output_overview(self):
        return dict(self.live_overview)

    def get_samplerate_status(self):
        return dict(self.samplerate_status)

    def get_measurement_sr_session(self):
        return _FakeMeasurementSrSession()

    def create_lifecycle_background_task(self, coro, *, name):
        raise AssertionError("not used by the measurement-entry sync path")


class MeasurementEntryRateSyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.samplerate_status = {"force_rate": 48000, "active_rate": 48000}
        self.live_overview = {
            "output_mode": {
                "mode": "stereo",
                "effective_output_key": OUTPUT_KEY,
                "effective_output_rate": 96000,
            },
            "selected_output": {"key": OUTPUT_KEY, "active_rate": 96000},
            "current_output": {"key": OUTPUT_KEY, "active_rate": 96000},
        }
        self.dsp = _RecordingDspRuntime()
        dsp_deps = _DspDeps(self.samplerate_status, self.live_overview, self.dsp)
        self.real_sync = dsp_orchestration.DspOrchestrator(dsp_deps).sync_runtime

        self.diagnosis = {
            "links_complete": True,
            "bypass_only": False,
            "dsp_ports": True,
            "helper_ports": True,
            "links": {"main_left": True, "main_right": True},
        }

    def _make_orchestrator(self):
        deps = SimpleNamespace(
            dsp_port_timeout_ms=1000,
            get_audio_output_overview=lambda: dict(self.live_overview),
            get_dsp_manager=lambda: None,
            sync_preset_for_samplerate=AsyncMock(return_value=None),
            wait_for_dsp_output_ports=AsyncMock(return_value=True),
            reconcile_sink_rate=AsyncMock(return_value=True),
            get_samplerate_status=lambda: dict(self.samplerate_status),
            sync_runtime=self.real_sync,
            get_dsp_snapshot=lambda: {"active": True, "config": {"sample_rate": 96000}},
            helper_argument_sample_rate=lambda snapshot: (snapshot or {}).get("config", {}).get("sample_rate"),
            output_mode_subwoofer_modes=frozenset(),
            sleep=AsyncMock(return_value=None),
            pipewire_poll_interval_ms=20,
        )
        orchestrator = playback_orchestration.PlaybackOrchestrator(deps)
        orchestrator.playback_graph_diagnosis = AsyncMock(return_value=dict(self.diagnosis))
        orchestrator.wait_for_dsp_output_ports = AsyncMock(return_value=True)
        orchestrator.reconcile_subwoofer_links_only = AsyncMock(return_value=None)
        orchestrator.repair_stereo_output_links_once = AsyncMock(return_value=None)
        orchestrator.relink_missing_production_links = AsyncMock(return_value=True)
        return orchestrator

    def _entry_request(self, **overrides):
        from playback.transition import TransitionRequest

        kwargs = dict(
            operation="measurement-entry",
            source="spotify",
            target_rate=48000,
            target_url=None,
            target_track={"id": "/com/spotify/track/6DSOpFdwej9u10qg4L3NzO"},
            should_play=False,
            rate_change=True,
            reload_source=False,
            detail="measurement-entry",
        )
        kwargs.update(overrides)
        return TransitionRequest(**kwargs)

    async def test_raw_live_overview_would_suppress_the_helper_rebuild(self):
        # The mechanism, directly on the real sync: an overview still carrying
        # the pre-rebuild hardware rate (96 kHz) next to an authoritative 48 kHz
        # is suppressed by the stale-check and rebuilds nothing.
        await self.real_sync(
            dict(self.live_overview),
            reason="coordinator-measurement-entry",
            _rate_lock_held=True,
        )
        self.assertEqual(self.dsp.sync_calls, [])

    async def test_measurement_entry_rebuilds_helper_at_target_rate(self):
        # The orchestrator stage hands the sync a token carrying the transition's
        # own target rate (48 kHz), so the stale-check passes and the helper is
        # rebuilt exactly once at 48 kHz -- the graph reaches canonical topology.
        orchestrator = self._make_orchestrator()
        result = await orchestrator.establish_effects_and_helper(self._entry_request())

        self.assertEqual(len(self.dsp.sync_calls), 1, "the helper must be rebuilt once at the target rate")
        synced = self.dsp.sync_calls[0]
        self.assertEqual(samplerate.overview_sample_rate(synced), 48000)
        self.assertEqual(synced["output_mode"]["effective_output_rate"], 48000)
        self.assertTrue(result["graph_complete"])

    async def test_existing_44k_playback_entry_to_48k_rebuilds_helper(self):
        # Same regression from the common 44.1 kHz playback state: the staged
        # token must carry the 48 kHz target so the entry commits.
        self.samplerate_status = {"force_rate": 48000, "active_rate": 48000}
        self.live_overview = {
            "output_mode": {"mode": "stereo", "effective_output_key": OUTPUT_KEY, "effective_output_rate": 44100},
            "selected_output": {"key": OUTPUT_KEY, "active_rate": 44100},
            "current_output": {"key": OUTPUT_KEY, "active_rate": 44100},
        }
        orchestrator = self._make_orchestrator()
        result = await orchestrator.establish_effects_and_helper(self._entry_request())
        self.assertTrue(result["graph_complete"])
        self.assertEqual(len(self.dsp.sync_calls), 1)
        self.assertEqual(samplerate.overview_sample_rate(self.dsp.sync_calls[0]), 48000)


if __name__ == "__main__":
    unittest.main()
