#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Per-input mode memory and source ownership before digital ingress routing."""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.external_input import ExternalInputRouting, ExternalInputRoutingDependencies
from audio.drift import SamplerateDriftObserver, SamplerateDriftDependencies
from audio.output_state import default_output_state
from audio.source_feed import SourceOverviewFeed, SourceTransitionLock
from audio.source_transitions import SourceTransitions, SourceTransitionDependencies
from playback.transition import TransitionRequest
from playback.runtime.source import _RuntimeSourceMixin
from playback.runtime.output_mode import _RuntimeOutputModeMixin
from types import SimpleNamespace
from dsp.runtime import DSPRuntime, DSPRuntimeConfig, CommandResult
from dsp.runtime import _contains_link
from playback.orchestration import PlaybackOrchestrator


class InputModeTests(unittest.TestCase):
    def test_app_identity_ignores_retained_external_key(self):
        from audio.input_output_mode import restore_mode
        app = {"mode": "app-playback"}
        tv = {"mode": "external-input", "selected_input_key": "tv"}
        state = default_output_state()
        state["active_mode"] = "stereo-sub"
        state = restore_mode(state, app, tv)
        state["active_mode"] = "surround"
        returned = restore_mode(state, tv, {"mode": "app-playback", "selected_input_key": "tv"})
        self.assertEqual(returned["active_mode"], "stereo-sub")

    def test_each_external_pair_remembers_its_own_mode_and_app_is_separate(self):
        from audio.input_output_mode import remember_mode, restore_mode
        app = {"mode": "app-playback"}
        tv = {"mode": "external-input", "selected_input_key": "scarlett::pair:9-10"}
        analog = {"mode": "external-input", "selected_input_key": "scarlett::pair:1-2"}
        state = restore_mode(default_output_state(), app, tv)
        state["active_mode"] = "surround"
        state = remember_mode(state, tv)
        state = restore_mode(state, tv, analog)
        self.assertEqual(state["active_mode"], "stereo")
        state["active_mode"] = "stereo-sub"
        state = restore_mode(state, analog, tv)
        self.assertEqual(state["active_mode"], "surround")
        state = restore_mode(state, tv, app)
        self.assertEqual(state["active_mode"], "stereo")
        state = restore_mode(state, app, analog)
        self.assertEqual(state["active_mode"], "stereo-sub")


class SourceOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_surround_ingress_requires_confirmed_links_not_command_exit_alone(self):
        async def run(args):
            return CommandResult(0, "")
        native = DSPRuntime(None, command_runner=run)
        native._process = SimpleNamespace(returncode=None)
        native._config = DSPRuntimeConfig("surround", "A", 48000, (), ())
        with self.assertRaisesRegex(RuntimeError, "ingress"):
            await native.set_surround_input(True)

    async def test_canonical_graph_verification_checks_all_decoded_positions(self):
        positions = ["FL", "FR", "FC", "LFE", "SL", "SR", "BL", "BR"]
        links = [(f"fxroute_dsp_sink:monitor_{position}", f"fxroute_dsp:input_{i + 1}")
                 for i, position in enumerate(positions[:2])]
        links += [(f"fxroute_surround_input:output_{position}", f"fxroute_dsp:input_{i + 1}")
                  for i, position in enumerate(positions)]
        links += [("fxroute_dsp:output_1", "alsa_output.A:playback_FL"),
                  ("fxroute_dsp:output_2", "alsa_output.A:playback_FR")]
        async def read(*args):
            return "\n".join(f"{a} -> {b}" for a, b in links)
        overview = {"output_mode": {"mode": "stereo", "effective_output_key": "alsa_output.A",
                     "hardware_playback_ports": ["playback_FL", "playback_FR"]}}
        deps = SimpleNamespace(run_pw_link_command=read, output_mode_subwoofer_modes=frozenset(),
            get_dsp_snapshot=lambda: {"active": True, "surround_input": True,
                                     "config": {"sample_rate": 48000, "output_mode": "surround"}},
            contains_link=_contains_link, helper_argument_sample_rate=lambda snapshot: 48000)
        orchestrator = PlaybackOrchestrator(deps)
        self.assertTrue((await orchestrator.playback_graph_diagnosis(overview, target_rate=48000))["links_complete"])
        links.remove(("fxroute_surround_input:output_FC", "fxroute_dsp:input_3"))
        self.assertFalse((await orchestrator.playback_graph_diagnosis(overview, target_rate=48000))["links_complete"])

    async def test_clock_borrow_keeps_previously_playing_app_paused(self):
        adapter = _RuntimeOutputModeMixin()
        player = SimpleNamespace(state={"current_file": "music", "playing": True, "paused": False},
                                 set_pause=lambda paused: None)
        adapter._player = player
        async def drain(fn, paused):
            player.state.update(playing=not paused, paused=paused)
        adapter._deps = SimpleNamespace(player_is_running=lambda: True, drain_worker=drain)
        request = TransitionRequest(operation="sample-rate-policy", source="local", input_rate_hz=48000)
        await adapter.restore_output_mode_transport(request, {"player": dict(player.state)}, "test")
        self.assertTrue(player.state["paused"])

    async def test_failed_mode_activation_restores_original_bindings_not_failed_target_mode(self):
        import copy
        current = {"mode": "app-playback"}
        state = default_output_state()
        state["input_modes"] = {"external-input:tv": "surround"}
        saved = copy.deepcopy(state)
        def selection(mode, key):
            current.update(mode=mode, selected_input_key=key)
            return dict(current)
        async def activate(previous, target):
            raise RuntimeError("activation failed")
        async def rollback(previous, target, original):
            self.assertEqual(original, saved)
            state.update(original)
            return target
        deps = SourceTransitionDependencies(
            get_lock=SourceTransitionLock, feed=SourceOverviewFeed(AsyncMock()),
            build_overview=lambda: dict(current), save_selection=selection, load_selection=lambda: dict(current),
            with_current_stdin_status=lambda value: value, sync_external_input=AsyncMock(side_effect=lambda value: value),
            sync_bluetooth_input=AsyncMock(side_effect=lambda value: value), get_stdin_input=lambda: None,
            note_source_selection=lambda mode: None, pause_app_playback=AsyncMock(), sync_peak_monitor=AsyncMock(),
            sync_peak_monitor_for_stdin=AsyncMock(), sync_output_mode=activate,
            capture_output_mode_state=lambda: copy.deepcopy(state), restore_output_mode_state=rollback)
        with self.assertRaisesRegex(RuntimeError, "activation failed"):
            await SourceTransitions(deps).apply_selection("external-input", "tv")
        self.assertEqual(state, saved)
        self.assertEqual(current["mode"], "app-playback")

    async def test_measurement_suspends_program_ingress_and_restores_it(self):
        links = set()
        async def run(args):
            if args == ("pw-link", "-l"):
                return CommandResult(0, "\n".join(f"{a} -> {b}" for a, b in links))
            if args[1] == "-d":
                links.discard(tuple(args[2:]))
            else:
                links.add(tuple(args[1:]))
            return CommandResult(0)
        native = DSPRuntime(SimpleNamespace(), command_runner=run)
        native._process = SimpleNamespace(returncode=None)
        native._config = DSPRuntimeConfig("surround", "A", 48000, ("playback_AUX0",), ())
        await native.set_surround_input(True)
        self.assertEqual(len([a for a, _ in links if a.startswith("fxroute_surround_input:")]), 8)
        previous = await native.enter_active_measurement()
        self.assertFalse(any(a.startswith("fxroute_surround_input:") for a, _ in links))
        await native.exit_active_measurement(previous)
        self.assertEqual(len([a for a, _ in links if a.startswith("fxroute_surround_input:")]), 8)

    async def test_clock_release_retires_owner_before_rate_restore_awaits(self):
        import main
        seen = []

        async def restore(policy, **kwargs):
            seen.append(main._wanted_digital_clock(3))
            self.assertFalse(main._surround_carrier_owned)

        with patch.object(main, "_surround_carrier_owned", True), patch.object(
            main, "_surround_clock_card", 3
        ), patch.object(main, "get_samplerate_status", return_value={"active_rate": 48000}), patch.object(
            main, "_select_surround_clock", AsyncMock()
        ), patch.object(main, "_transition_sample_rate_policy", side_effect=restore):
            await main._set_surround_carrier_owned(False)
            self.assertFalse(main._surround_carrier_owned)
        self.assertEqual(seen, [False], "clock guard must not reacquire a released carrier")

    async def test_clock_release_failure_restores_carrier_ownership(self):
        import main
        with patch.object(main, "_surround_carrier_owned", True), patch.object(
            main, "_surround_clock_card", 3
        ), patch.object(main, "get_samplerate_status", return_value={"active_rate": 48000}), patch.object(
            main, "_select_surround_clock", AsyncMock()
        ), patch.object(main, "_transition_sample_rate_policy", AsyncMock(side_effect=RuntimeError("rate failed"))):
            with self.assertRaisesRegex(RuntimeError, "rate failed"):
                await main._set_surround_carrier_owned(False)
            self.assertTrue(main._surround_carrier_owned)
            self.assertEqual(main._surround_clock_card, 3)

    async def test_carrier_rate_override_does_not_persist_the_playback_policy(self):
        class Adapter(_RuntimeSourceMixin, _RuntimeOutputModeMixin):
            pass
        adapter = Adapter()
        policy = {"mode": "auto", "rate": None}
        adapter._deps = SimpleNamespace(sample_rate_policy_request=AsyncMock(return_value=TransitionRequest(
            operation="sample-rate-policy", source="local", target_rate=44100, sample_rate_policy=policy)))
        request = TransitionRequest(operation="sample-rate-policy", source="local", target_rate=48000,
                                    sample_rate_policy=policy, input_rate_hz=48000)
        refreshed = await adapter.refresh_transition_request(request)
        self.assertEqual(refreshed.target_rate, 48000)
        self.assertFalse(refreshed.should_play)
        with patch("audio.samplerate.persist_sample_rate_policy", side_effect=AssertionError("policy overwritten")):
            await adapter.commit_sample_rate_policy(refreshed)
        self.assertEqual(policy, {"mode": "auto", "rate": None})

    async def test_surround_never_links_the_carrier_to_stereo_ingress(self):
        overview = {"mode": "external-input", "selected_input": {
            "key": "scarlett::pair:9-10", "source_key": "scarlett", "alsa_card": 3,
            "left_channel": "AUX8", "right_channel": "AUX9"}}
        calls = []
        async def digital(source, left, right, card):
            calls.append((source, left, right, card))
        routing = ExternalInputRouting(ExternalInputRoutingDependencies(
            get_audio_source_overview=lambda: overview, get_output_mode=lambda: "surround",
            sync_digital_input=digital, stop_digital_input=AsyncMock()))
        with patch("audio.pw_link.connect_ports", side_effect=AssertionError("carrier leaked to PCM")):
            await routing.sync()
            await routing.sync()
        self.assertEqual(calls, [("scarlett", "AUX8", "AUX9", 3)] * 2, "the card owns the carrier clock")

    async def test_paused_app_drift_cannot_override_external_rate_owner(self):
        def stale_track():
            raise AssertionError("external selection must not inspect paused playback")
        deps = SamplerateDriftDependencies(
            get_current_track_info=stale_track, get_player_instance=lambda: None,
            coordinator_source_rate=lambda *args: 44100, get_player_audio_samplerate=lambda: 44100,
            get_samplerate_status=lambda: {"active_rate": 48000}, playback_transition_is_active=lambda: False,
            is_measurement_window_open=lambda: False, measurement_session_active=lambda: False,
            measurement_audio_graph_owned=lambda: False, request_coordinated_recovery=AsyncMock(),
            get_source_mode=lambda: "external-input")
        await SamplerateDriftObserver(deps).observe()

    async def test_mode_activation_precedes_external_routing_and_rolls_back(self):
        current = {"mode": "app-playback", "selected_input_key": None}
        events = []

        def save(mode, key):
            current.update(mode=mode, selected_input_key=key)
            return {**current, "selected_input": {"key": key} if key else None}

        async def activate(previous, overview):
            events.append(("activate", overview["mode"]))
            return overview

        async def route(overview):
            events.append(("route", overview["mode"]))
            if overview["mode"] == "external-input":
                raise RuntimeError("decoder failed")
            return overview

        deps = SourceTransitionDependencies(
            get_lock=lambda: SourceTransitionLock(), feed=SourceOverviewFeed(AsyncMock()),
            build_overview=lambda: dict(current), save_selection=save, load_selection=lambda: dict(current),
            with_current_stdin_status=lambda overview: overview, sync_external_input=route,
            sync_bluetooth_input=AsyncMock(side_effect=lambda overview: overview), get_stdin_input=lambda: None,
            note_source_selection=lambda mode: None, pause_app_playback=AsyncMock(), sync_peak_monitor=AsyncMock(),
            sync_peak_monitor_for_stdin=AsyncMock(), sync_output_mode=activate)
        with self.assertRaisesRegex(RuntimeError, "decoder failed"):
            await SourceTransitions(deps).apply_selection("external-input", "tv")
        self.assertEqual(events, [("activate", "external-input"), ("route", "external-input"),
                                  ("activate", "app-playback"), ("route", "app-playback")])
        self.assertEqual(current["mode"], "app-playback")


if __name__ == "__main__":
    unittest.main()
