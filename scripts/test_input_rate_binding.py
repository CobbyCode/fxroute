#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Active input sessions retain pins; source end releases auto policy."""

import asyncio
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import audio.samplerate as samplerate
import playback.orchestration as orchestration
from playback.transition import TransitionRequest
from playback_transition_test_support import make_transition_runtime


class InputRateBindingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.mode = "stdin-input"
        self.stdin = {"available": True, "session_id": "writer", "rate": 48000, "state": "streaming",
                      "selected": True, "routed": True}
        self.rate = {"active_rate": 48000, "force_rate": 48000, "default_rate": 44100}
        self.writes = []
        self.policy = {"mode": "auto", "rate": None}
        self.stack.enter_context(patch.object(samplerate, "_load_audio_source_selection",
            side_effect=lambda: {"mode": self.mode, "selected_input_key": None}))
        # Real source overview with only the hardware probes replaced.
        self.stack.enter_context(patch.object(samplerate.overview, "_load_audio_source_selection",
            side_effect=lambda: {"mode": self.mode, "selected_input_key": "input"}))
        self.stack.enter_context(patch.object(samplerate.overview, "_run_command", return_value=""))
        self.stack.enter_context(patch.object(samplerate.overview, "get_bluetooth_audio_overview",
            return_value={"available": True, "roles": {"bluetooth_input": {"available": True, "selectable": True}},
                          "receiver_session": {"source_name": "bluez_input.test"}}))
        previous_provider = samplerate.overview._stdin_status_reader
        samplerate.configure_stdin_input_status(lambda: dict(self.stdin))
        self.addCleanup(lambda: samplerate.configure_stdin_input_status(previous_provider))
        self.stack.enter_context(patch.object(samplerate.alignment, "load_sample_rate_policy",
                                             side_effect=lambda: dict(self.policy)))
        self.stack.enter_context(patch.object(samplerate.alignment, "get_samplerate_status",
                                             side_effect=lambda: dict(self.rate)))
        self.stack.enter_context(patch.object(samplerate.alignment, "set_pipewire_force_rate",
                                             side_effect=self.write_rate))

    def write_rate(self, rate):
        self.writes.append(rate)
        self.rate["force_rate"] = rate

    async def test_policy_commit_does_not_treat_active_stdin_as_idle(self):
        runtime = make_transition_runtime()
        request = TransitionRequest(operation="sample-rate-policy", source="local",
            target_rate=48000, should_play=False, target_url=None, sample_rate_policy=self.policy)
        with patch.object(samplerate, "persist_sample_rate_policy", return_value=self.policy):
            await runtime.commit_sample_rate_policy(request)
        self.assertEqual(self.rate["force_rate"], 48000)

    async def test_ready_or_draining_session_retains_rate_without_recent_frames(self):
        for state in ("connected", "ready", "draining"):
            with self.subTest(state=state):
                self.stdin.update(state=state, routed=False)
                samplerate.clear_auto_policy_force_rate(48000, status=self.rate, idle=True)
                self.assertEqual(self.rate["force_rate"], 48000)

    async def test_active_app_at_default_keeps_pin(self):
        self.mode = "app-playback"
        self.rate.update(active_rate=44100, force_rate=44100)
        self.assertFalse(samplerate.clear_auto_policy_force_rate(44100, status=self.rate))
        self.assertEqual(self.rate["force_rate"], 44100)

    async def test_bluetooth_and_external_selection_survive_app_stop_clear(self):
        for mode in ("bluetooth-input", "external-input"):
            with self.subTest(mode=mode):
                self.mode = mode
                self.rate["force_rate"] = 48000
                samplerate.clear_auto_policy_force_rate(48000, status=self.rate, idle=True)
                self.assertEqual(self.rate["force_rate"], 48000)

    async def test_ended_stdin_releases_pin(self):
        self.stdin = {"available": True, "session_id": None, "rate": None,
                      "state": "waiting", "selected": True}
        with patch.object(main.source_transitions, "publish_stdin_state", new=AsyncMock()), \
             patch.object(main.runtime, "stdin_input", SimpleNamespace(snapshot=lambda: self.stdin)), \
             patch.object(main, "_measurement_session_owns_live_rate", return_value=False), \
             patch.object(main, "_playback_transition_is_active", return_value=False), \
             patch.object(main, "_surround_carrier_owned", False), \
             patch.object(samplerate, "load_sample_rate_policy", return_value=self.policy), \
             patch.object(orchestration.configured(), "sample_rate_policy_request", new=AsyncMock(
                 return_value=SimpleNamespace(rate_change=False, target_rate=48000,
                                              target_url=None, should_play=False))):
            await main._on_stdin_state_changed()
        self.assertEqual(self.rate["force_rate"], 0)

    async def test_idle_source_handoff_releases_pin_even_without_rate_change(self):
        self.mode = "app-playback"
        with patch.object(main, "_measurement_session_owns_live_rate", return_value=False), \
             patch.object(main, "_playback_transition_is_active", return_value=False), \
             patch.object(main, "_surround_carrier_owned", False), \
             patch.object(samplerate, "load_sample_rate_policy", return_value=self.policy), \
             patch.object(orchestration.configured(), "sample_rate_policy_request", new=AsyncMock(
                 return_value=SimpleNamespace(rate_change=False, target_rate=48000,
                                              target_url=None, should_play=False))):
            await main._follow_active_source_native_rate({"mode": self.mode}, reason="test-handoff")
        self.assertEqual(self.rate["force_rate"], 0)

    async def test_fixed_policy_keeps_ended_input_pin(self):
        self.policy = {"mode": "fixed", "rate": 48000}
        self.stdin = {"session_id": None}
        self.assertFalse(samplerate.clear_auto_policy_force_rate(48000, status=self.rate, idle=True))
        self.assertEqual(self.rate["force_rate"], 48000)

    async def test_unreadable_input_activity_never_releases_pin(self):
        with patch.object(samplerate.alignment, "get_audio_source_overview",
                          side_effect=RuntimeError("input read failed")):
            self.assertFalse(samplerate.clear_auto_policy_force_rate(48000, idle=True))
        self.assertEqual(self.rate["force_rate"], 48000)
        self.stdin = {"available": False, "session_id": None}
        self.assertFalse(samplerate.clear_auto_policy_force_rate(48000, idle=True))
        self.assertEqual(self.rate["force_rate"], 48000)

    async def test_input_eof_does_not_clear_measurement_transition_or_carrier_pin(self):
        self.stdin = {"available": True, "session_id": None, "state": "waiting"}
        for owner in ("measurement", "transition", "carrier"):
            with self.subTest(owner=owner), \
                 patch.object(main.source_transitions, "publish_stdin_state", new=AsyncMock()), \
                 patch.object(main.runtime, "stdin_input", SimpleNamespace(snapshot=lambda: self.stdin)), \
                 patch.object(main, "_measurement_session_owns_live_rate", return_value=owner == "measurement"), \
                 patch.object(main, "_playback_transition_is_active", return_value=owner == "transition"), \
                 patch.object(main, "_surround_carrier_owned", owner == "carrier"), \
                 patch.object(samplerate, "load_sample_rate_policy", return_value=self.policy):
                await main._on_stdin_state_changed()
                self.assertEqual(self.rate["force_rate"], 48000)

    async def test_active_app_sources_keep_default_pin_after_policy_commit(self):
        self.mode = "app-playback"
        self.rate.update(active_rate=44100, force_rate=44100)
        runtime = make_transition_runtime()
        for source in ("local", "radio", "spotify", "qobuz"):
            with self.subTest(source=source), \
                 patch.object(samplerate, "persist_sample_rate_policy", return_value=self.policy):
                await runtime.commit_sample_rate_policy(TransitionRequest(
                    operation="sample-rate-policy", source=source, target_rate=44100,
                    target_url="track", should_play=True, sample_rate_policy=self.policy))
                self.assertEqual(self.rate["force_rate"], 44100)

    async def test_idle_release_rechecks_measurement_ownership_after_request_build(self):
        self.mode = "app-playback"
        owned = False

        async def build(*args, **kwargs):
            nonlocal owned
            owned = True
            return SimpleNamespace(rate_change=False, target_rate=48000,
                                   target_url=None, should_play=False)

        with patch.object(main, "_measurement_session_owns_live_rate", side_effect=lambda: owned), \
             patch.object(main, "_playback_transition_is_active", return_value=False), \
             patch.object(main, "_surround_carrier_owned", False), \
             patch.object(samplerate, "load_sample_rate_policy", return_value=self.policy), \
             patch.object(orchestration.configured(), "sample_rate_policy_request", side_effect=build):
            await main._follow_active_source_native_rate({"mode": self.mode}, reason="test-owner-race")
        self.assertEqual(self.rate["force_rate"], 48000)

    async def test_idle_release_holds_existing_coordinator_and_rate_locks(self):
        self.mode = "app-playback"
        coordinator = SimpleNamespace(lock=asyncio.Lock())
        session = SimpleNamespace(lock=asyncio.Lock(), owns_audio_graph=False)
        locks_at_write = []

        def write(rate):
            locks_at_write.append((coordinator.lock.locked(), session.lock.locked()))
            self.write_rate(rate)

        with patch.object(main, "playback_transition_coordinator", coordinator), \
             patch.object(main, "measurement_sr_session", session), \
             patch.object(main, "_playback_transition_is_active", return_value=False), \
             patch.object(main, "_surround_carrier_owned", False), \
             patch.object(samplerate, "load_sample_rate_policy", return_value=self.policy), \
             patch.object(samplerate.alignment, "set_pipewire_force_rate", side_effect=write), \
             patch.object(orchestration.configured(), "sample_rate_policy_request", new=AsyncMock(
                 return_value=SimpleNamespace(rate_change=False, target_rate=48000,
                                              target_url=None, should_play=False))):
            await main._follow_active_source_native_rate({"mode": self.mode}, reason="test-locks")
        self.assertEqual(self.rate["force_rate"], 0)
        self.assertEqual(locks_at_write, [(True, True)])

    async def test_idle_release_does_not_use_a_superseded_auto_policy(self):
        self.mode = "app-playback"

        async def build(*args, **kwargs):
            self.policy = {"mode": "fixed", "rate": 48000}
            return SimpleNamespace(rate_change=False, target_rate=48000,
                                   target_url=None, should_play=False)

        with patch.object(main, "_measurement_session_owns_live_rate", return_value=False), \
             patch.object(main, "_playback_transition_is_active", return_value=False), \
             patch.object(main, "_surround_carrier_owned", False), \
             patch.object(samplerate, "load_sample_rate_policy", side_effect=lambda: dict(self.policy)), \
             patch.object(orchestration.configured(), "sample_rate_policy_request", side_effect=build):
            await main._follow_active_source_native_rate({"mode": self.mode}, reason="test-policy-race")
        self.assertEqual(self.rate["force_rate"], 48000)

    async def test_bluetooth_inventory_failure_is_not_an_ended_session(self):
        self.mode = "bluetooth-input"
        with patch.object(samplerate.overview, "get_bluetooth_audio_overview", return_value={
            "available": True, "roles": {"bluetooth_input": {"selectable": True, "state": "idle"}},
            "receiver_session": None, "notes": ["Bluetooth source inventory unavailable: probe failed"],
        }):
            self.assertFalse(samplerate.clear_auto_policy_force_rate(48000, idle=True))
        self.assertEqual(self.rate["force_rate"], 48000)

    async def test_output_mode_completion_releases_eof_skipped_while_busy(self):
        self.stdin = {"available": True, "session_id": None, "rate": None,
                      "state": "waiting", "selected": True}
        busy = True
        service = SimpleNamespace(snapshot=lambda: self.stdin, resync_routing=AsyncMock())
        with patch.object(main.source_transitions, "publish_stdin_state", new=AsyncMock()), \
             patch.object(main.runtime, "stdin_input", service), \
             patch.object(main, "_measurement_session_owns_live_rate", return_value=False), \
             patch.object(main, "_playback_transition_is_active", side_effect=lambda: busy), \
             patch.object(main, "_surround_carrier_owned", False), \
             patch.object(samplerate, "load_sample_rate_policy", return_value=self.policy), \
             patch.object(orchestration.configured(), "sample_rate_policy_request", new=AsyncMock(
                 return_value=SimpleNamespace(rate_change=False, target_rate=48000,
                                              target_url=None, should_play=False))):
            await main._on_stdin_state_changed()
            self.assertEqual(self.rate["force_rate"], 48000)
            busy = False
            await main._resync_stdin_routing()
        self.assertEqual(self.rate["force_rate"], 0)

    async def test_rate_change_follow_discards_a_superseded_epoch(self):
        async def build(*args, **kwargs):
            main.playback_state.playback_transition_epoch += 1
            return SimpleNamespace(rate_change=True, target_rate=48000)

        with patch.object(main.playback_state, "playback_transition_epoch", 0), \
             patch.object(main, "_measurement_session_owns_live_rate", return_value=False), \
             patch.object(main, "_playback_transition_is_active", return_value=False), \
             patch.object(main, "_surround_carrier_owned", False), \
             patch.object(samplerate, "load_sample_rate_policy", return_value=self.policy), \
             patch.object(orchestration.configured(), "sample_rate_policy_request", side_effect=build), \
             patch.object(main, "_transition_sample_rate_policy", new=AsyncMock()) as transition:
            await main._follow_active_source_native_rate({"mode": self.mode}, reason="test-stale-rate")
        transition.assert_not_awaited()

    async def test_rate_follow_cannot_replace_fixed_policy_committed_before_admission(self):
        orchestrator = orchestration.configured()
        request = TransitionRequest(operation="sample-rate-policy", source="local", target_rate=48000,
                                    rate_change=True, sample_rate_policy=dict(self.policy))

        async def admit(request):
            self.policy = {"mode": "fixed", "rate": 44100}
            guard = request.skip_if_committed_owner
            if guard is None or not await guard():
                self.policy = dict(request.sample_rate_policy)
            return SimpleNamespace(committed=False)

        deps = replace(orchestrator._deps, run_transition=admit)
        with patch.object(main, "_measurement_session_owns_live_rate", return_value=False), \
             patch.object(main, "_playback_transition_is_active", return_value=False), \
             patch.object(main, "_surround_carrier_owned", False), \
             patch.object(samplerate, "load_sample_rate_policy", side_effect=lambda: dict(self.policy)), \
             patch.object(orchestrator, "_deps", deps), \
             patch.object(orchestrator, "sample_rate_policy_request", new=AsyncMock(return_value=request)):
            await main._follow_active_source_native_rate({"mode": self.mode}, reason="test-admission")
        self.assertEqual(self.policy, {"mode": "fixed", "rate": 44100})

    async def test_line_request_never_reloads_retained_app_track(self):
        orchestrator = orchestration.configured()
        deps = replace(orchestrator._deps,
            get_audio_output_overview=lambda: {"selected_output": {"supported_rates": [44100, 48000]}},
            get_samplerate_status=lambda: dict(self.rate), get_source_mode=lambda: self.mode,
            get_stdin_snapshot=lambda: dict(self.stdin))
        old_app = {"source": "local", "target_url": "/old.flac",
                   "target_track": {"sample_rate_hz": 44100}, "should_play": False}
        self.rate.update(active_rate=44100, force_rate=44100)
        with patch.object(orchestrator, "_deps", deps), \
             patch.object(orchestrator, "current_playback_context", new=AsyncMock(return_value=old_app)), \
             patch.object(samplerate, "playback_target_rate", side_effect=lambda rate, policy: rate):
            request = await orchestrator.sample_rate_policy_request(self.policy, detail="test-input")
        self.assertEqual(request.target_rate, 48000)
        self.assertIsNone(request.target_url)
        self.assertFalse(request.reload_source)

    async def test_native_input_repins_an_already_aligned_unpinned_graph(self):
        orchestrator = orchestration.configured()
        self.rate["force_rate"] = 0
        deps = replace(orchestrator._deps,
            get_audio_output_overview=lambda: {"selected_output": {"supported_rates": [44100, 48000]}},
            get_samplerate_status=lambda: dict(self.rate), get_source_mode=lambda: self.mode,
            get_stdin_snapshot=lambda: dict(self.stdin))
        with patch.object(orchestrator, "_deps", deps), \
             patch.object(orchestrator, "current_playback_context", new=AsyncMock(return_value={})), \
             patch.object(samplerate, "playback_target_rate", side_effect=lambda rate, policy: rate):
            request = await orchestrator.sample_rate_policy_request(self.policy, detail="test-repin")
        self.assertTrue(request.rate_change)


if __name__ == "__main__":
    unittest.main(verbosity=2)
