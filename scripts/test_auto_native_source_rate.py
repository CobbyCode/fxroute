#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Auto policy follows the active source's native sample rate (TDD)."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import asyncio
import json
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio import source_native_rate as native_rate
import main
import playback.orchestration as orchestration
from audio.bluetooth import BluetoothInputDependencies, BluetoothInputMonitor
from audio.source_transitions import SourceTransitionDependencies, SourceTransitions
from audio.source_feed import SourceOverviewFeed, SourceTransitionLock


BT_NODE = "bluez_input.84_9E_56_57_19_94.2"


def _dump(node_name=BT_NODE, *, node_rate="1/48000", latency="512/48000",
          fmt_rate=48000, enum_rates=(48000,)):
    return json.dumps([{
        "id": 209, "type": "PipeWire:Interface:Node",
        "info": {
            "props": {"node.name": node_name, "node.rate": node_rate,
                      "node.latency": latency},
            "params": {
                "Format": [{"mediaType": "audio", "rate": fmt_rate}],
                "EnumFormat": [{"mediaType": "audio", "rate": rate} for rate in enum_rates],
            },
        },
    }])


def _bt_overview(source_name=BT_NODE):
    return {"bluetooth": {"source_name": source_name, "streaming": True}}


class BluetoothNodeRateTests(unittest.TestCase):
    def test_aac_48k_live_shape(self):
        self.assertEqual(native_rate.parse_bluetooth_node_rate(_dump(), BT_NODE), 48000)

    def test_sbc_441k(self):
        dump = _dump(node_rate="1/44100", latency="512/44100", fmt_rate=44100,
                     enum_rates=(44100,))
        self.assertEqual(native_rate.parse_bluetooth_node_rate(dump, BT_NODE), 44100)

    def test_node_rate_only_without_format(self):
        dump = json.dumps([{"id": 1, "type": "PipeWire:Interface:Node",
                            "info": {"props": {"node.name": BT_NODE, "node.rate": "1/48000"},
                                     "params": {}}}])
        self.assertEqual(native_rate.parse_bluetooth_node_rate(dump, BT_NODE), 48000)

    def test_unknown_node_is_none(self):
        self.assertIsNone(native_rate.parse_bluetooth_node_rate(_dump(), "bluez_input.XX.9"))

    def test_unparseable_dump_is_none(self):
        self.assertIsNone(native_rate.parse_bluetooth_node_rate("not json", BT_NODE))

    def test_non_candidate_rate_is_none(self):
        dump = _dump(node_rate="1/12345", latency="512/12345", fmt_rate=12345,
                     enum_rates=(12345,))
        self.assertIsNone(native_rate.parse_bluetooth_node_rate(dump, BT_NODE))

    def test_source_rate_from_overview_session(self):
        self.assertEqual(
            native_rate.bluetooth_source_rate(overview=_bt_overview(), pw_dump=_dump()), 48000)

    def test_source_rate_without_session_is_none(self):
        self.assertIsNone(
            native_rate.bluetooth_source_rate(overview={"bluetooth": {}}, pw_dump=_dump()))


class StdinAndExternalRateTests(unittest.TestCase):
    def test_stdin_active_session_rate(self):
        self.assertEqual(
            native_rate.stdin_source_rate({"session_id": "abc", "rate": 96000}), 96000)

    def test_stdin_without_session_is_none(self):
        self.assertIsNone(native_rate.stdin_source_rate({"session_id": None, "rate": 48000}))
        self.assertIsNone(native_rate.stdin_source_rate({}))

    def test_external_analog_has_no_native_rate(self):
        entry = {"source_key": "alsa_input.usb-Focusrite-Pro", "device_label": "Scarlett",
                 "port_label": "Line In", "pair_label": "Input 1-2", "alsa_card": 3}
        self.assertIsNone(native_rate.external_source_rate(entry, helper_sample_rate=48000))

    def test_external_digital_follows_helper(self):
        entry = {"source_key": "alsa_input.usb-SPDIF", "device_label": "S/PDIF In",
                 "port_label": "IEC958", "pair_label": "Input 1-2", "alsa_card": 2}
        self.assertEqual(
            native_rate.external_source_rate(entry, helper_sample_rate=48000), 48000)

    def test_external_digital_without_helper_is_none(self):
        entry = {"source_key": "alsa_input.usb-SPDIF", "device_label": "S/PDIF In",
                 "port_label": "IEC958", "pair_label": "Input 1-2", "alsa_card": 2}
        self.assertIsNone(native_rate.external_source_rate(entry, helper_sample_rate=None))

    def test_resolve_dispatch(self):
        self.assertEqual(
            native_rate.resolve_active_source_native_rate(
                "bluetooth-input", bluetooth_overview=_bt_overview(), pw_dump=_dump()), 48000)
        self.assertEqual(
            native_rate.resolve_active_source_native_rate(
                "stdin-input", stdin_snapshot={"session_id": "s", "rate": 96000}), 96000)
        self.assertIsNone(
            native_rate.resolve_active_source_native_rate("app-playback"))
        self.assertIsNone(native_rate.resolve_active_source_native_rate("bogus-mode"))


def _feed():
    return SourceOverviewFeed(broadcast=lambda message: asyncio.sleep(0),
                              decorate=lambda overview: overview)


def _transition_deps(**overrides):
    base = dict(
        get_lock=lambda: SourceTransitionLock(),
        feed=_feed(),
        build_overview=lambda: {"mode": "app-playback"},
        save_selection=lambda mode, key: {"mode": mode, "selected_input_key": key,
                                          "selected_input": None},
        load_selection=lambda: {"mode": "app-playback", "selected_input_key": None},
        with_current_stdin_status=lambda overview: overview,
        sync_external_input=AsyncMock(side_effect=lambda overview: overview),
        sync_bluetooth_input=AsyncMock(side_effect=lambda overview: overview),
        get_stdin_input=lambda: None,
        note_source_selection=lambda mode: 1,
        pause_app_playback=AsyncMock(),
        sync_peak_monitor=AsyncMock(),
        sync_peak_monitor_for_stdin=AsyncMock(),
    )
    base.update(overrides)
    return SourceTransitionDependencies(**base)


class SourceSwitchFollowTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_selection_calls_rate_follow_after_commit(self):
        follow = AsyncMock()
        transitions = SourceTransitions(_transition_deps(request_source_rate_follow=follow))
        result = await transitions.save_selection("bluetooth-input", None)
        self.assertEqual(result.get("mode"), "bluetooth-input")
        follow.assert_awaited_once()
        self.assertEqual(follow.await_args.args[0].get("mode"), "bluetooth-input")

    async def test_no_follow_without_dependency(self):
        transitions = SourceTransitions(_transition_deps())
        result = await transitions.save_selection("bluetooth-input", None)
        self.assertEqual(result.get("mode"), "bluetooth-input")


class OrchestratorLineRateTests(unittest.IsolatedAsyncioTestCase):
    async def test_bluetooth_mode_policy_request_uses_native_rate(self):
        orchestrator = orchestration.configured()
        deps = replace(orchestrator._deps,
                       get_audio_output_overview=lambda: {
                           "selected_output": {"supported_rates": [44100, 48000]}},
                       get_samplerate_status=lambda: {"active_rate": 44100, "force_rate": 0},
                       get_source_mode=lambda: "bluetooth-input",
                       get_bluetooth_overview=lambda: _bt_overview(),
                       get_pw_dump=lambda: _dump())
        with patch.object(orchestrator, "_deps", deps):
            request = await orchestrator.sample_rate_policy_request(
                {"mode": "auto", "rate": None}, detail="test-native-bt")
        self.assertEqual(request.target_rate, 48000)

    async def test_analog_external_keeps_hardware_rate(self):
        orchestrator = orchestration.configured()
        entry = {"source_key": "alsa_input.usb-Focusrite-Pro", "device_label": "Scarlett",
                 "port_label": "Line In", "pair_label": "Input 1-2", "alsa_card": 3}
        deps = replace(orchestrator._deps,
                       get_audio_output_overview=lambda: {
                           "selected_output": {"supported_rates": [44100, 48000]}},
                       get_samplerate_status=lambda: {"active_rate": 44100, "force_rate": 0},
                       get_source_mode=lambda: "external-input",
                       get_external_entry=lambda: entry,
                       get_external_helper_rate=lambda: 48000)
        with patch.object(orchestrator, "_deps", deps):
            request = await orchestrator.sample_rate_policy_request(
                {"mode": "auto", "rate": None}, detail="test-native-analog")
        self.assertEqual(request.target_rate, 44100)

    async def test_fixed_policy_ignores_native_line_rate(self):
        orchestrator = orchestration.configured()
        deps = replace(orchestrator._deps,
                       get_audio_output_overview=lambda: {
                           "selected_output": {"supported_rates": [44100, 48000]}},
                       get_samplerate_status=lambda: {"active_rate": 44100, "force_rate": 0},
                       get_source_mode=lambda: "bluetooth-input",
                       get_bluetooth_overview=lambda: _bt_overview(),
                       get_pw_dump=lambda: _dump())
        with patch.object(orchestrator, "_deps", deps):
            request = await orchestrator.sample_rate_policy_request(
                {"mode": "fixed", "rate": 44100}, detail="test-native-fixed")
        self.assertEqual(request.target_rate, 44100)


def _bt_streaming_overview(source_name=BT_NODE):
    return {"mode": "bluetooth-input",
            "bluetooth": {"source_name": source_name, "state": "streaming",
                          "selectable": True, "discoverable": True, "pairable": True}}


def _bt_monitor_deps(**overrides):
    base = dict(
        sync_peak_monitor_for_source_mode_state=AsyncMock(),
        get_samplerate_status=lambda: {"active_rate": 44100, "force_rate": 0},
        get_pw_dump=lambda: _dump(),
        sample_rate_policy_is_auto=lambda: True,
        measurement_owns_graph=lambda: False,
        playback_transition_is_active=lambda: False,
        request_native_rate_follow=AsyncMock(),
    )
    base.update(overrides)
    return BluetoothInputDependencies(**base)


class BluetoothMidStreamFollowTests(unittest.IsolatedAsyncioTestCase):
    def _dump_441(self):
        return _dump(node_rate="1/44100", latency="512/44100",
                     fmt_rate=44100, enum_rates=(44100,))

    async def test_stable_mismatch_fires_follow_after_two_ticks(self):
        # Default dump is 48000 while the graph runs at 44100: mismatch.
        monitor = BluetoothInputMonitor(_bt_monitor_deps())
        overview = _bt_streaming_overview()
        await monitor._maybe_follow_native_rate(overview)
        monitor._deps.request_native_rate_follow.assert_not_awaited()
        await monitor._maybe_follow_native_rate(overview)
        monitor._deps.request_native_rate_follow.assert_awaited_once_with("bluetooth-native-rate")

    async def test_aligned_graph_never_fires(self):
        monitor = BluetoothInputMonitor(_bt_monitor_deps(get_pw_dump=self._dump_441))
        overview = _bt_streaming_overview()
        await monitor._maybe_follow_native_rate(overview)
        await monitor._maybe_follow_native_rate(overview)
        monitor._deps.request_native_rate_follow.assert_not_awaited()

    async def test_fixed_policy_never_fires(self):
        monitor = BluetoothInputMonitor(_bt_monitor_deps(sample_rate_policy_is_auto=lambda: False))
        overview = _bt_streaming_overview()
        await monitor._maybe_follow_native_rate(overview)
        await monitor._maybe_follow_native_rate(overview)
        monitor._deps.request_native_rate_follow.assert_not_awaited()


class FollowGuardTests(unittest.IsolatedAsyncioTestCase):
    def _guards(self, **overrides):
        values = dict(policy={"mode": "auto", "rate": None},
                      selection={"mode": "bluetooth-input", "selected_input_key": None},
                      owns_graph=False, transition_active=False, carrier_owned=False)
        values.update(overrides)
        return values

    def _patch_guards(self, guards):
        return (
            patch.object(main.samplerate, "load_sample_rate_policy",
                         return_value=dict(guards["policy"])),
            patch.object(main.samplerate, "_load_audio_source_selection",
                         return_value=dict(guards["selection"])),
            patch.object(main, "_measurement_session_owns_live_rate",
                         return_value=guards["owns_graph"]),
            patch.object(main, "_playback_transition_is_active",
                         return_value=guards["transition_active"]),
            patch.object(main, "_surround_carrier_owned", guards["carrier_owned"]),
        )

    async def test_stale_commit_is_discarded(self):
        guards = self._guards(selection={"mode": "app-playback", "selected_input_key": None})
        patches = self._patch_guards(guards)
        request = AsyncMock(return_value=None)
        transition = AsyncMock()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patch.object(orchestration.configured(), "sample_rate_policy_request", request), \
                patch.object(main, "_transition_sample_rate_policy", transition):
            await main._follow_active_source_native_rate({"mode": "bluetooth-input"},
                                                         reason="test-stale")
        request.assert_not_awaited()
        transition.assert_not_awaited()

    async def test_aligned_graph_skips_transition(self):
        guards = self._guards()
        patches = self._patch_guards(guards)
        fake_request = SimpleNamespace(rate_change=False, target_rate=48000)
        request = AsyncMock(return_value=fake_request)
        transition = AsyncMock()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patch.object(orchestration.configured(), "sample_rate_policy_request", request), \
                patch.object(main, "_transition_sample_rate_policy", transition):
            await main._follow_active_source_native_rate({"mode": "bluetooth-input"},
                                                         reason="test-aligned")
        request.assert_awaited_once()
        transition.assert_not_awaited()

    async def test_rate_change_runs_transition(self):
        guards = self._guards()
        patches = self._patch_guards(guards)
        fake_request = SimpleNamespace(rate_change=True, target_rate=48000)
        request = AsyncMock(return_value=fake_request)
        transition = AsyncMock()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patch.object(orchestration.configured(), "sample_rate_policy_request", request), \
                patch.object(main, "_transition_sample_rate_policy", transition):
            await main._follow_active_source_native_rate({"mode": "bluetooth-input"},
                                                         reason="test-change")
        request.assert_awaited_once()
        transition.assert_awaited_once()

    async def test_fixed_policy_skips_everything(self):
        guards = self._guards(policy={"mode": "fixed", "rate": 44100})
        patches = self._patch_guards(guards)
        request = AsyncMock(return_value=None)
        transition = AsyncMock()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patch.object(orchestration.configured(), "sample_rate_policy_request", request), \
                patch.object(main, "_transition_sample_rate_policy", transition):
            await main._follow_active_source_native_rate({"mode": "bluetooth-input"},
                                                         reason="test-fixed")
        request.assert_not_awaited()
        transition.assert_not_awaited()


if __name__ == "__main__":
    unittest.main(verbosity=2)
