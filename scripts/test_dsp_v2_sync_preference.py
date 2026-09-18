#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Regression: helper sync serves the committed v2 plan, legacy is fallback.

Every playback helper (re)build -- startup, play/pause transitions,
link-watch repairs -- goes through ``DspOrchestrator.sync_runtime``.  When
the committed output state activates for the current device and rate, the
sync must render that plan (crossover ways, bank chains, trims) instead of
the legacy overview graph; otherwise the next play after a bank/crossover
edit silently wipes the running DSP back to the legacy 80 Hz topology.

Covered here, without importing the application composition root:
  v2 renderer returns a target -> ``sync_rendered`` wins, legacy ``sync``
      is never called (normal and stale-repair branches);
  v2 renderer returns None (head cannot activate) -> legacy ``sync`` runs;
  v2 renderer raises -> fail-open to legacy ``sync``.
"""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dsp.orchestration import DspOrchestrator


class _FakeDspRuntime:
    def __init__(self) -> None:
        self.sync_calls = []
        self.rendered_calls = []
        self.snapshot_value = {"active": False, "config": {}}

    async def sync(self, overview: dict) -> None:
        self.sync_calls.append(dict(overview))

    async def sync_rendered(self, target) -> None:
        self.rendered_calls.append(target)

    def snapshot(self):
        return dict(self.snapshot_value)


class _FakeSession:
    def __init__(self) -> None:
        import asyncio as _asyncio

        self.lock = _asyncio.Lock()


class _FakeDeps:
    def __init__(self, v2_target=None, v2_raises=False) -> None:
        self.runtime = _FakeDspRuntime()
        self._v2_target = v2_target
        self._v2_raises = v2_raises
        self.overview = {
            "selected_output": {"key": "A", "channels": 4, "active_rate": 48_000},
            "active_rate": 48_000,
            "output_mode": {
                "mode": "stereo",
                "effective_output_key": "A",
                "effective_output_channels": 4,
                "hardware_playback_ports": [f"playback_AUX{i}" for i in range(4)],
            },
        }
        self.status = {"active_rate": 48_000, "force_rate": 48_000}

    def get_dsp_runtime(self):
        return self.runtime

    def get_dsp_manager(self):
        return None

    def get_audio_output_overview(self):
        return dict(self.overview)

    def get_samplerate_status(self):
        return dict(self.status)

    def get_measurement_sr_session(self):
        return _FakeSession()

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

    async def get_spotify_ui_state(self, *_a, **_k):
        raise AssertionError("unused")

    async def get_qobuz_ui_state(self, *_a, **_k):
        raise AssertionError("unused")

    async def sync_peak_monitor_for_playback_state(self, *_a, **_k):
        raise AssertionError("unused")

    async def sync_peak_monitor_for_spotify_state(self, *_a, **_k):
        raise AssertionError("unused")

    async def sync_peak_monitor_for_qobuz_state(self, *_a, **_k):
        raise AssertionError("unused")

    async def load_dsp_preset(self, *_a, **_k):
        raise AssertionError("unused")

    async def broadcast(self, *_a, **_k):
        raise AssertionError("unused")

    async def wait_for_samplerate_alignment(self, *_a, **_k):
        return True

    async def wait_for_selected_output_effective_rate(self, *_a, **_k):
        return (True, None)

    def measurement_audio_graph_owned(self):
        return False

    async def observe_playback_samplerate_drift(self):
        raise AssertionError("unused")

    def playback_transition_is_active(self):
        return False

    def coordinator_target_rate(self, *_a, **_k):
        return 48_000

    async def playback_graph_diagnosis(self, *_a, **_k):
        raise AssertionError("unused")

    async def request_coordinated_recovery(self, *_a, **_k):
        raise AssertionError("unused")

    def create_lifecycle_background_task(self, _coro, *, name):
        raise AssertionError("unused")

    async def sleep(self, _delay):
        return None

    async def try_render_v2_target(self, rate, overview):
        if self._v2_raises:
            raise RuntimeError("plan cannot activate")
        assert rate == 48_000
        assert overview["output_mode"]["effective_output_key"] == "A"
        return self._v2_target


def run_sync(deps):
    return asyncio.run(DspOrchestrator(deps).sync_runtime(reason="unit"))


class V2SyncPreferenceTests(unittest.TestCase):
    def test_v2_target_wins_over_legacy_sync(self):
        deps = _FakeDeps(v2_target=object())
        run_sync(deps)
        self.assertEqual(len(deps.runtime.rendered_calls), 1)
        self.assertIs(deps.runtime.rendered_calls[0], deps._v2_target)
        self.assertEqual(deps.runtime.sync_calls, [])

    def test_v2_none_falls_back_to_legacy_sync(self):
        deps = _FakeDeps(v2_target=None)
        run_sync(deps)
        self.assertEqual(deps.runtime.rendered_calls, [])
        self.assertEqual(len(deps.runtime.sync_calls), 1)

    def test_v2_failure_falls_back_to_legacy_sync(self):
        deps = _FakeDeps(v2_target=object(), v2_raises=True)
        run_sync(deps)
        self.assertEqual(deps.runtime.rendered_calls, [])
        self.assertEqual(len(deps.runtime.sync_calls), 1)

    def test_stale_repair_branch_prefers_v2(self):
        async def run():
            deps = _FakeDeps(v2_target=object())
            orchestrator = DspOrchestrator(deps)
            token = dict(deps.overview)
            await orchestrator.sync_runtime(
                token, reason="unit-repair", _rate_lock_held=True,
                allow_unsettled_rate=True)
            self.assertEqual(len(deps.runtime.rendered_calls), 1)
            self.assertEqual(deps.runtime.sync_calls, [])

        asyncio.run(run())

    def test_stale_repair_branch_falls_back_to_legacy(self):
        async def run():
            deps = _FakeDeps(v2_target=None)
            orchestrator = DspOrchestrator(deps)
            token = dict(deps.overview)
            await orchestrator.sync_runtime(
                token, reason="unit-repair", _rate_lock_held=True,
                allow_unsettled_rate=True)
            self.assertEqual(deps.runtime.rendered_calls, [])
            self.assertEqual(len(deps.runtime.sync_calls), 1)

        asyncio.run(run())

    def test_unrenderable_head_keeps_running_v2_graph(self):
        # A committed-but-unrenderable head (reachable only past the API,
        # e.g. a hand-edited state file) must not wipe a healthy running v2
        # graph back to the legacy topology from a background sync.
        deps = _FakeDeps(v2_target=None)
        deps.runtime.snapshot_value = {
            "active": True,
            "config": {"plan_fingerprint": "abc123", "output_key": "A"},
        }
        run_sync(deps)
        self.assertEqual(deps.runtime.rendered_calls, [])
        self.assertEqual(deps.runtime.sync_calls, [])

    def test_unrenderable_head_without_running_v2_uses_legacy(self):
        deps = _FakeDeps(v2_target=None)
        deps.runtime.snapshot_value = {"active": True, "config": {}}
        run_sync(deps)
        self.assertEqual(deps.runtime.rendered_calls, [])
        self.assertEqual(len(deps.runtime.sync_calls), 1)

    def test_unrenderable_head_with_idle_engine_uses_legacy(self):
        deps = _FakeDeps(v2_target=None)
        deps.runtime.snapshot_value = {
            "active": False,
            "config": {"plan_fingerprint": "abc123"},
        }
        run_sync(deps)
        self.assertEqual(deps.runtime.rendered_calls, [])
        self.assertEqual(len(deps.runtime.sync_calls), 1)

    def test_dead_engine_bypasses_sink_misalignment_deferral(self):
        # A dead helper pins nothing: the sync must rebuild at the
        # authoritative rate instead of deferring forever behind a rate pin.
        deps = _FakeDeps(v2_target=None)
        deps.status = {"active_rate": 44_100, "force_rate": 48_000}
        deps.runtime.snapshot_value = {"active": False, "config": {}}
        run_sync(deps)
        self.assertEqual(deps.runtime.rendered_calls, [])
        self.assertEqual(len(deps.runtime.sync_calls), 1)

    def test_live_engine_keeps_sink_misalignment_deferral(self):
        deps = _FakeDeps(v2_target=object())
        deps.status = {"active_rate": 44_100, "force_rate": 48_000}
        deps.runtime.snapshot_value = {"active": True, "config": {}}
        run_sync(deps)
        self.assertEqual(deps.runtime.rendered_calls, [])
        self.assertEqual(deps.runtime.sync_calls, [])

    def test_dead_engine_prefers_v2_over_legacy(self):
        deps = _FakeDeps(v2_target=object())
        deps.status = {"active_rate": 44_100, "force_rate": 48_000}
        deps.runtime.snapshot_value = {"active": False, "config": {}}
        run_sync(deps)
        self.assertEqual(len(deps.runtime.rendered_calls), 1)
        self.assertEqual(deps.runtime.sync_calls, [])


if __name__ == "__main__":
    unittest.main()
