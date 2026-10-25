#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""HDMI layout changes run through the verified output-selection transaction."""

import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from audio import hdmi
from playback.transition import PlaybackTransitionCoordinator
from test_playback_transition_coordinator import FakeRuntime
from audio.output_state import routing_for_device, set_mode_routing, switch_mode
from scripts.test_output_state_api import make_manager, make_service
from audio.samplerate import _load_audio_output_selection, _save_audio_output_selection
from test_hdmi_transport import (CARD, MARANTZ_ELD, PORTS_51, PORTS_71, STEREO, SURROUND, SURROUND71, card,
                                 surround_eld, tv_eld)

APPLY_OUTPUT_SELECTION = main._apply_audio_output_selection


class HdmiTransportSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        environment = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.directory.name})
        environment.start()
        self.addCleanup(environment.stop)
        _save_audio_output_selection(STEREO)
        self.card = card()
        self.sink = STEREO
        self.eld = MARANTZ_ELD
        self.commands = []
        self.selections = []
        self.fail_selection = set()
        self.manager = make_manager(self.directory.name)
        self.service = make_service(self.directory.name, self.manager)
        self.gate_runtime = FakeRuntime()
        self.coordinator = PlaybackTransitionCoordinator(self.gate_runtime, gate_settle_seconds=0)
        self.session = SimpleNamespace(lock=asyncio.Lock(), has_active_jobs=False)
        patches = [
            mock.patch.object(main, "playback_transition_coordinator", self.coordinator),
            mock.patch.object(main, "measurement_sr_session", self.session),
            mock.patch.object(main.samplerate, "_run_command", side_effect=self.run_command),
            mock.patch.object(main.alsa_ctl, "read_pcm_eld", side_effect=lambda card_number, device: self.eld),
            mock.patch.object(main, "_apply_audio_output_selection", side_effect=self.select),
            mock.patch.object(main, "dsp_orchestrator", SimpleNamespace(
                refresh_peak_monitor_after_effects_change=self.refresh)),
            mock.patch.object(main.runtime, "dsp_runtime", SimpleNamespace(
                snapshot=lambda: {"active": True, 'config': {'output_key': self.sink}}, sync_in_progress=False)),
            mock.patch.object(main.playback_orchestration.configured(), 'playback_graph_diagnosis',
                              side_effect=self.diagnose),
            mock.patch.object(main, "_measurement_session_owns_live_rate", return_value=False),
            mock.patch.object(main, "_hdmi_transport_watch", hdmi.TransportWatch()),
            mock.patch.object(main, "_hdmi_eld_cache", hdmi.EldCache()),
            mock.patch.object(main, "get_output_service", return_value=self.service),
            mock.patch.object(main, "_require_dsp_manager", return_value=self.manager),
            mock.patch.object(main, "get_samplerate_status", return_value={"active_rate": 44100}),
            mock.patch.object(main, "get_audio_output_overview", side_effect=self.overview),
            mock.patch.object(main, "_run_coordinated_transition", side_effect=self.transition),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    async def refresh(self, reason):
        return None

    async def diagnose(self, target=None, **kwargs):
        return {'output_key': self.sink, 'output_links_complete': True, 'links_complete': True}

    async def transition(self, request):
        change = request.output_state_transition
        await asyncio.to_thread(self.service.commit_unowned, change["candidate_state"],
                                expected_revision=change["expected_revision"])

    def overview(self, *, selection_key=None):
        selected = selection_key or self.sink
        parsed = hdmi.parse_sink_key(selected or STEREO)
        channels = hdmi.transport(parsed.transport)["channels"]
        return {"selected_output": {"key": selected, "channels": channels,
                                    "hdmi": hdmi.describe(selected, card=self.card, eld=hdmi.parse_eld(self.eld))},
                "outputs": [{"key": self.sink, "name": self.sink, "channels": channels, "selectable": True}],
                "default_output": {"key": self.sink},
                "output_mode": {"effective_output_key": selected, "effective_output_channels": channels,
                                 "hardware_playback_ports": PORTS_71[:channels]}}

    async def automatic(self, mode):
        main._seed_hdmi_routing(self.sink, self.overview())
        return await main.output_application.apply_candidate({
            "expected_revision": self.service.load()["revision"],
            "mutation": {"kind": "switch_mode", "mode": mode}})

    async def test_automatic_modes_switch_stereo_sub_surround_and_back(self):
        for mode, expected in (("stereo-sub", SURROUND), ("surround", SURROUND71), ("stereo", STEREO)):
            result = await self.automatic(mode)
            if mode == "stereo":
                self.assertEqual(self.sink, SURROUND71, "A mode edit must not downshift the card")
                await main._reconcile_hdmi_transport()
                await main._reconcile_hdmi_transport()
            self.assertEqual(self.sink, expected)
            self.assertEqual(self.service.load()["active_mode"], mode)
            self.assertTrue(result["live_applied"])
        self.assertEqual(self.profile_changes(), ["output:hdmi-surround+input:analog-stereo",
                                                 "output:hdmi-surround71+input:analog-stereo",
                                                 "output:hdmi-stereo+input:analog-stereo"])

    async def test_routing_edits_upgrade_immediately_but_defer_downgrades_to_watch(self):
        await self.automatic("surround")
        for assignments in (["main_l", "main_r", "rear_l", "rear_r", "center", "sub1", "off", "off"],
                            ["main_l", "main_r", "rear_l", "rear_r", "center", "sub1", "side_l", "side_r"]):
            before = self.profile_changes()
            await main.output_application.apply_candidate({"expected_revision": self.service.load()["revision"],
                "mutation": {"kind": "set_routing", "mode": "surround", "assignments": assignments}})
            self.assertEqual(self.sink, SURROUND71)
            self.assertEqual(self.profile_changes(), before)
            self.assertEqual(routing_for_device(self.service.load(), "surround", self.sink, 8), assignments)

    async def test_automatic_failure_restores_mode_and_transport(self):
        self.fail_selection.add(SURROUND71)
        with self.assertRaises(Exception):
            await self.automatic("surround")
        self.assertEqual(self.service.load()["active_mode"], "stereo")
        self.assertEqual(self.sink, STEREO)
        self.assertEqual(_load_audio_output_selection()["selected_key"], STEREO)

    async def test_automatic_failure_has_one_output_rollback_after_state_revert(self):
        original = RuntimeError('Target selection failed')
        select = self.select
        restored_modes = []
        async def failing(key, **kwargs):
            if key == SURROUND71:
                self.selections.append(key)
                raise original
            restored_modes.append(self.service.load()['active_mode'])
            return await select(key, **kwargs)
        with mock.patch.object(main, '_apply_audio_output_selection', side_effect=failing):
            with self.assertRaises(RuntimeError) as raised:
                await self.automatic('surround')
        self.assertIs(raised.exception, original)
        self.assertEqual(self.selections, [SURROUND71, STEREO])
        self.assertEqual(restored_modes, ['stereo'], 'Restore output only after reverting the candidate')
        self.assertEqual(hdmi.load_connectors(), {})
        self.assertEqual(self.profile_changes(), ['output:hdmi-surround71+input:analog-stereo',
                                                 'output:hdmi-stereo+input:analog-stereo'])

    async def test_real_selection_defers_all_graph_rollback_to_the_automatic_edit(self):
        original = RuntimeError('Original native apply failure')
        graph = [STEREO]
        syncs = []
        async def sync(*, reason, target_overview, **kwargs):
            key = target_overview['selected_output']['key']
            syncs.append((reason, self.service.load()['active_mode'], key))
            if reason == 'output-selection-rollback':
                raise RuntimeError('Premature rollback under candidate state')
            if key == SURROUND71:
                graph[0] = None
                raise original
            graph[0] = key
        async def diagnose(target, **kwargs):
            return {'output_key': graph[0], 'output_links_complete': bool(graph[0]), 'links_complete': bool(graph[0])}
        native = SimpleNamespace(snapshot=lambda: {'active': bool(graph[0]), 'config': {'output_key': graph[0]}},
                                 fail_closed=mock.AsyncMock())
        with mock.patch.object(main, '_apply_audio_output_selection', wraps=APPLY_OUTPUT_SELECTION), \
                mock.patch.object(main.samplerate, 'prepare_audio_output_selection', side_effect=lambda key: {'name': key}), \
                mock.patch.object(main.samplerate, '_set_default_sink', return_value=None), \
                mock.patch.object(main.dsp_orchestrator, 'sync_runtime', side_effect=sync, create=True), \
                mock.patch.object(main.runtime, 'dsp_runtime', native), \
                mock.patch.object(main.playback_orchestration.configured(), 'playback_graph_diagnosis', side_effect=diagnose):
            with self.assertRaises(RuntimeError) as raised:
                await self.automatic('surround')
        self.assertIs(raised.exception, original)
        self.assertEqual(syncs, [('output-selection', 'surround', SURROUND71),
                                 ('output-selection', 'stereo', STEREO)])
        self.assertEqual(graph[0], STEREO)
        self.assertEqual(_load_audio_output_selection()['selected_key'], STEREO)
        native.fail_closed.assert_not_awaited()
        self.assertFalse(self.coordinator.gate.closed)

    async def test_verified_automatic_rollback_restores_user_mute_and_opens_its_gate(self):
        for user_muted in (False, True):
            with self.subTest(user_muted=user_muted):
                self.gate_runtime.muted = user_muted
                self.fail_selection.add(SURROUND71)
                async def verify(key, overview):
                    self.assertEqual(key, STEREO)
                    self.assertEqual(self.sink, STEREO)
                    self.assertEqual(self.service.load()['active_mode'], 'stereo')
                    self.assertTrue(self.coordinator.gate.closed)
                    self.assertTrue(self.gate_runtime.muted)
                with mock.patch.object(main, '_verify_audio_output_selection', side_effect=verify):
                    with self.assertRaisesRegex(RuntimeError, 'graph apply failed'):
                        await self.automatic('surround')
                self.assertEqual(self.gate_runtime.muted, user_muted)
                self.assertFalse(self.coordinator.gate.closed)
                self.assertFalse(self.coordinator.gate.failure_latched)

    async def test_unverified_automatic_rollback_keeps_gate_latched(self):
        self.fail_selection.add(SURROUND71)
        with mock.patch.object(main, '_verify_audio_output_selection',
                               side_effect=RuntimeError('Restored graph is incomplete')):
            with self.assertRaisesRegex(RuntimeError, 'graph apply failed') as raised:
                await self.automatic('surround')
        self.assertTrue(self.gate_runtime.muted)
        self.assertTrue(self.coordinator.gate.failure_latched)
        self.assertTrue(any('Restored graph is incomplete' in note for note in getattr(raised.exception, '__notes__', [])))

    async def test_automatic_mode_upgrade_updates_mpv_channels_and_relinks_current_track(self):
        from playback.player import MPVWrapper
        player = MPVWrapper()
        player._state.update(current_file='/music/current.flac', playing=True, paused=False)
        player._notify_callbacks()
        commands = []
        async def ingress():
            commands.append(('ingress', self.sink))
            return True
        with mock.patch.object(main.runtime, 'player_instance', player), \
                mock.patch.object(main, '_player_is_running', return_value=True), \
                mock.patch.object(player, 'set_property', side_effect=lambda *args: commands.append(args)), \
                mock.patch.object(main.playback_orchestration.configured(), 'ensure_mpv_to_dsp_links', side_effect=ingress):
            await self.automatic('surround')
        self.assertEqual(commands, [('audio-channels', 'auto'), ('ingress', SURROUND71)])
        self.assertEqual(player.state['current_file'], '/music/current.flac')
        self.assertFalse(player.state['paused'])

    async def test_automatic_channel_change_and_ingress_are_gated_and_keep_user_mute(self):
        from playback.player import MPVWrapper
        for user_muted in (False, True):
            with self.subTest(user_muted=user_muted):
                self.gate_runtime.muted = user_muted
                player = MPVWrapper()
                player._state.update(current_file='/music/current.flac')
                player._notify_callbacks()
                def property_write(*args):
                    self.assertTrue(self.gate_runtime.muted, 'Channel change requires a physical mute')
                    self.assertTrue(self.coordinator.gate.closed)
                async def ingress():
                    self.assertTrue(self.gate_runtime.muted, 'Ingress relink requires a physical mute')
                    return True
                with mock.patch.object(main.runtime, 'player_instance', player), \
                        mock.patch.object(main, '_player_is_running', return_value=True), \
                        mock.patch.object(player, 'set_property', side_effect=property_write), \
                        mock.patch.object(main.playback_orchestration.configured(), 'ensure_mpv_to_dsp_links',
                                          side_effect=ingress):
                    await self.automatic('surround')
                self.assertEqual(self.gate_runtime.muted, user_muted)
                self.assertFalse(self.coordinator.gate.closed)
                # Reset transport and stored mode for the next independent upgrade.
                await self.automatic('stereo')
                await self.change(SURROUND71, 'stereo')

    async def test_failed_hdmi_downshift_still_restores_channels_and_connector(self):
        from playback.player import MPVWrapper
        player = MPVWrapper()
        player._state.update(current_file='/music/current.flac')
        player._notify_callbacks()
        original = RuntimeError('Original ingress failure')
        channels = []
        connectors = hdmi.load_connectors()
        transport = main._apply_hdmi_transport
        async def fail_downshift(sink, target, **kwargs):
            if target == 'stereo':
                raise RuntimeError('Downshift failed')
            return await transport(sink, target, **kwargs)
        with mock.patch.object(main.runtime, 'player_instance', player), \
                mock.patch.object(main, '_player_is_running', return_value=True), \
                mock.patch.object(player, 'set_property', side_effect=lambda *args: channels.append(args)), \
                mock.patch.object(main, '_apply_hdmi_transport', side_effect=fail_downshift), \
                mock.patch.object(main.playback_orchestration.configured(), 'ensure_mpv_to_dsp_links',
                                  side_effect=[original, True]):
            with self.assertRaises(RuntimeError) as raised:
                await self.automatic('surround')
        self.assertIs(raised.exception, original)
        self.assertEqual(channels, [('audio-channels', 'auto'), ('audio-channels', 'stereo')])
        self.assertEqual(hdmi.load_connectors(), connectors)
        self.assertEqual(self.sink, SURROUND71, 'Failed downshift must not be reported as restored')
        self.assertEqual(self.service.load()['active_mode'], 'stereo')
        self.assertTrue(self.coordinator.gate.failure_latched)
        self.assertTrue(any('Downshift failed' in note for note in original.__notes__))

    async def _production_gate_upgrade(self, user_muted):
        from dataclasses import replace
        from playback.runtime import FxrouteTransitionRuntime
        from playback.player import MPVWrapper
        adapter = FxrouteTransitionRuntime(replace(main.make_playback_runtime_deps(), output_switch_user_mute=None))
        coordinator = PlaybackTransitionCoordinator(adapter, gate_settle_seconds=0)
        coordinator._startup_gate_reconciled = True
        mutes = {STEREO: user_muted}
        def read(key):
            if key != self.sink:
                raise RuntimeError(f'No such sink: {key}')
            return mutes.get(key, False)
        def write(key, muted):
            read(key)
            mutes[key] = muted
        player = MPVWrapper()
        player._state.update(current_file='/music/current.flac')
        player._notify_callbacks()
        def property_write(*args):
            self.assertTrue(read(self.sink), 'Replacement sink must be gated before changing channels')
        with mock.patch.object(main, 'playback_transition_coordinator', coordinator), \
                mock.patch('playback.runtime.mute._hardware_sink_for_transition', side_effect=lambda deps: self.sink), \
                mock.patch('playback.runtime.mute._read_hardware_sink_mute', side_effect=read), \
                mock.patch('playback.runtime.mute._set_hardware_sink_mute', side_effect=write), \
                mock.patch.object(main.runtime, 'player_instance', player), \
                mock.patch.object(main, '_player_is_running', return_value=True), \
                mock.patch.object(player, 'set_property', side_effect=property_write), \
                mock.patch.object(main.playback_orchestration.configured(), 'ensure_mpv_to_dsp_links', return_value=True):
            await self.automatic('surround')
        self.assertEqual(read(SURROUND71), user_muted)
        self.assertFalse(coordinator.gate.closed)

    async def test_production_gate_rebinds_a_replaced_sink_before_channels_and_restore(self):
        await self._production_gate_upgrade(False)

    async def test_production_gate_preserves_user_mute_across_sink_replacement(self):
        await self._production_gate_upgrade(True)

    async def test_failed_state_rollback_uses_the_mode_that_is_still_published(self):
        from playback.player import MPVWrapper
        player = MPVWrapper()
        player._state.update(current_file='/music/current.flac')
        player._notify_callbacks()
        original = RuntimeError('Original ingress failure')
        channels = []
        with mock.patch.object(main.runtime, 'player_instance', player), \
                mock.patch.object(main, '_player_is_running', return_value=True), \
                mock.patch.object(player, 'set_property', side_effect=lambda *args: channels.append(args)), \
                mock.patch.object(self.service, 'revert', side_effect=OSError('State rollback failed')), \
                mock.patch.object(main.playback_orchestration.configured(), 'ensure_mpv_to_dsp_links',
                                  side_effect=[original, True]):
            with self.assertRaises(RuntimeError) as raised:
                await self.automatic('surround')
        self.assertIs(raised.exception, original)
        self.assertEqual(self.service.load()['active_mode'], 'surround')
        self.assertEqual(channels, [('audio-channels', 'auto')])
        self.assertEqual(self.sink, STEREO)
        self.assertTrue(self.coordinator.gate.failure_latched)
        self.assertTrue(any('State rollback failed' in note for note in original.__notes__))

    async def test_automatic_mode_failure_restores_mpv_channel_policy(self):
        from playback.player import MPVWrapper
        connectors_before = hdmi.load_connectors()
        player = MPVWrapper()
        player._state.update(current_file='/music/current.flac')
        player._notify_callbacks()
        commands = []
        with mock.patch.object(main.runtime, 'player_instance', player), \
                mock.patch.object(main, '_player_is_running', return_value=True), \
                mock.patch.object(player, 'set_property', side_effect=lambda *args: commands.append(args)), \
                mock.patch.object(player, 'get_property', return_value='stereo'), \
                mock.patch.object(main, '_verify_audio_output_selection', return_value=None), \
                mock.patch.object(main.playback_orchestration.configured(), 'ensure_mpv_to_dsp_links',
                                  side_effect=[False, True]):
            with self.assertRaisesRegex(RuntimeError, 'MPV channel-mode ingress'):
                await self.automatic('surround')
        self.assertEqual(commands, [('audio-channels', 'auto'), ('audio-channels', 'stereo')])
        self.assertEqual(self.service.load()['active_mode'], 'stereo')
        self.assertEqual(self.sink, STEREO)
        self.assertEqual(hdmi.load_connectors(), connectors_before)
        self.assertFalse(self.coordinator.gate.closed)
        self.assertFalse(self.gate_runtime.muted)

    async def test_uncertain_mpv_channel_restore_does_not_open_gate(self):
        from playback.player import MPVWrapper
        player = MPVWrapper()
        player._state.update(current_file='/music/current.flac')
        player._notify_callbacks()
        with mock.patch.object(main.runtime, 'player_instance', player), \
                mock.patch.object(main, '_player_is_running', return_value=True), \
                mock.patch.object(player, 'set_property', return_value=None), \
                mock.patch.object(player, 'get_property', return_value='auto'), \
                mock.patch.object(main, '_verify_audio_output_selection', return_value=None), \
                mock.patch.object(main.playback_orchestration.configured(), 'ensure_mpv_to_dsp_links',
                                  side_effect=[False, True]):
            with self.assertRaisesRegex(RuntimeError, 'MPV channel-mode ingress') as raised:
                await self.automatic('surround')
        self.assertTrue(self.coordinator.gate.failure_latched)
        self.assertTrue(self.gate_runtime.muted)
        self.assertTrue(any('restoration was not confirmed' in note for note in raised.exception.__notes__))

    async def test_unchanged_mpv_channels_still_require_verified_ingress_on_rollback(self):
        from playback.player import MPVWrapper
        player = MPVWrapper()
        player._state.update(current_file='/music/current.flac')
        player._notify_callbacks()
        self.fail_selection.add(SURROUND71)
        with mock.patch.object(main.runtime, 'player_instance', player), \
                mock.patch.object(main, '_player_is_running', return_value=True), \
                mock.patch.object(player, 'get_property', return_value='stereo'), \
                mock.patch.object(main, '_verify_audio_output_selection', return_value=None), \
                mock.patch.object(main.playback_orchestration.configured(), 'ensure_mpv_to_dsp_links', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'graph apply failed'):
                await self.automatic('surround')
        self.assertTrue(self.coordinator.gate.failure_latched)
        self.assertTrue(self.gate_runtime.muted)

    async def test_complete_outputs_do_not_open_rollback_gate_with_incomplete_source_graph(self):
        from playback.player import MPVWrapper
        player = MPVWrapper()
        player._state.update(current_file='/music/current.flac')
        player._notify_callbacks()
        self.fail_selection.add(SURROUND71)
        async def diagnosis(target, **kwargs):
            return {'output_key': self.sink, 'output_links_complete': True,
                    'source_links_complete': False, 'links_complete': False}
        with mock.patch.object(main.runtime, 'player_instance', player), \
                mock.patch.object(main.playback_state, 'current_playback_owner', None), \
                mock.patch.object(main, '_player_is_running', return_value=True), \
                mock.patch.object(player, 'get_property', return_value='stereo'), \
                mock.patch.object(main.playback_orchestration.configured(), 'ensure_mpv_to_dsp_links', return_value=True), \
                mock.patch.object(main.playback_orchestration.configured(), 'playback_graph_diagnosis',
                                  side_effect=diagnosis):
            with self.assertRaisesRegex(RuntimeError, 'graph apply failed') as raised:
                await self.automatic('surround')
        self.assertTrue(self.coordinator.gate.failure_latched)
        self.assertTrue(self.gate_runtime.muted)
        self.assertTrue(any('playback graph was not restored' in note for note in raised.exception.__notes__))

    async def test_failed_profile_rollback_preserves_original_error_and_safe_gate(self):
        original = RuntimeError('Original target failure')
        select = self.select
        run_command = self.run_command
        def command(args, *rest, **kwargs):
            if args[:2] == ['pactl', 'set-card-profile'] and 'hdmi-stereo+' in args[3]:
                raise RuntimeError('Profile restore failed')
            return run_command(args, *rest, **kwargs)
        async def failing(key, **kwargs):
            if key == SURROUND71:
                raise original
            return await select(key, **kwargs)
        with mock.patch.object(main.samplerate, '_run_command', side_effect=command), \
                mock.patch.object(main, '_apply_audio_output_selection', side_effect=failing), \
                mock.patch.object(main, '_verify_audio_output_selection', return_value=None):
            with self.assertRaises(RuntimeError) as raised:
                await self.automatic('surround')
        self.assertIs(raised.exception, original)
        self.assertTrue(self.coordinator.gate.failure_latched)
        self.assertTrue(self.gate_runtime.muted)
        self.assertTrue(any('Profile restore failed' in note for note in raised.exception.__notes__))

    async def test_uninitialized_routing_cannot_supersede_failure_rollback(self):
        before = self.service.load()
        self.fail_selection.add(SURROUND)
        with self.assertRaises(Exception):
            await main.output_application.apply_candidate({"expected_revision": before["revision"],
                "mutation": {"kind": "switch_mode", "mode": "stereo-sub"}})
        self.assertEqual(self.service.load()["modes"], before["modes"])
        self.assertEqual(self.service.load()["active_mode"], "stereo")
        self.assertEqual(self.sink, STEREO)

    async def test_each_side_can_be_configured_in_a_separate_routing_edit(self):
        await self.automatic("surround")
        assignments = ["main_l", "main_r", "rear_l", "rear_r", "center", "sub1", "off", "off"]
        await main.output_application.apply_candidate({"expected_revision": self.service.load()["revision"],
            "mutation": {"kind": "set_routing", "mode": "surround", "assignments": assignments}})
        await main._reconcile_hdmi_transport()
        await main._reconcile_hdmi_transport()
        assignments[-2] = "side_l"
        result = await main.output_application.apply_candidate({"expected_revision": self.service.load()["revision"],
            "mutation": {"kind": "set_routing", "mode": "surround", "assignments": assignments}})
        self.assertFalse(result["live_applied"])
        self.assertEqual(result["live_reason"], "not-activatable")
        self.assertEqual(self.sink, SURROUND)
        self.commands.clear()
        await main._reconcile_hdmi_transport()
        await main._reconcile_hdmi_transport()
        self.assertEqual(self.profile_changes(), [])
        assignments[-1] = "side_r"
        result = await main.output_application.apply_candidate({"expected_revision": self.service.load()["revision"],
            "mutation": {"kind": "set_routing", "mode": "surround", "assignments": assignments}})
        self.assertTrue(result["live_applied"])
        self.assertEqual(self.sink, SURROUND71)

    async def test_watch_does_not_activate_a_draft_with_an_unrenderable_layout(self):
        with mock.patch.object(self.service, "compile_layout", side_effect=ValueError("Convolver IR is missing")):
            result = await self.automatic("surround")
            self.assertFalse(result["live_applied"])
            await main._reconcile_hdmi_transport()
            await main._reconcile_hdmi_transport()
        self.assertEqual(self.profile_changes(), [])
        self.assertEqual(self.sink, STEREO)

    async def test_implicit_default_output_does_not_need_reselection(self):
        main.samplerate._audio_output_selection_path().unlink()
        await self.automatic("stereo-sub")
        self.assertEqual(self.sink, SURROUND)

    async def test_watch_automates_an_implicit_default_output(self):
        main._seed_hdmi_routing(self.sink, self.overview())
        self.service.apply(lambda state: switch_mode(state, "surround"), expected_revision=self.service.load()["revision"])
        main.samplerate._audio_output_selection_path().unlink()
        await main._reconcile_hdmi_transport()
        await main._reconcile_hdmi_transport()
        self.assertEqual(self.sink, SURROUND71)

    async def test_measurement_and_stale_edits_do_not_change_the_profile(self):
        main._seed_hdmi_routing(self.sink, self.overview())
        before = self.service.load()
        for revision, busy, expected in ((before["revision"] - 1, False, 409),
                                         (before["revision"], True, 423)):
            self.session.has_active_jobs = busy
            with self.assertRaises(HTTPException) as failure:
                await main.output_application.apply_candidate({"expected_revision": revision,
                    "mutation": {"kind": "switch_mode", "mode": "surround"}})
            self.assertEqual(failure.exception.status_code, expected)
            self.assertEqual(self.service.load(), before)
            self.assertEqual(self.profile_changes(), [])

    async def test_caller_cancellation_drains_the_owned_profile_change(self):
        main._seed_hdmi_routing(self.sink, self.overview())
        entered, release = asyncio.Event(), asyncio.Event()
        select = self.select

        async def delayed(key, **kwargs):
            if key == SURROUND71:
                entered.set()
                await release.wait()
            return await select(key, **kwargs)

        with mock.patch.object(main, "_apply_audio_output_selection", side_effect=delayed):
            task = asyncio.create_task(main.output_application.apply_candidate({
                "expected_revision": self.service.load()["revision"],
                "mutation": {"kind": "switch_mode", "mode": "surround"}}))
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            self.assertTrue(self.coordinator.lock.locked())
            self.assertTrue(self.session.lock.locked())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.sink, SURROUND71)
        self.assertEqual(self.service.load()["active_mode"], "surround")
        self.assertFalse(self.coordinator.lock.locked())

    async def test_transport_result_fingerprint_matches_the_rendered_target(self):
        result = await self.automatic("stereo-sub")
        expected = self.service.fingerprint(self.service.load(), output_key=SURROUND, channels=6, sample_rate_hz=44100)
        self.assertEqual(result["fingerprint"], expected)

    async def test_cancelled_selection_rolls_back_mode_and_transport(self):
        main._seed_hdmi_routing(self.sink, self.overview())
        before = self.service.load()
        select = self.select

        async def cancel_target(key, **kwargs):
            if key == SURROUND71:
                raise asyncio.CancelledError()
            return await select(key, **kwargs)

        with mock.patch.object(main, "_apply_audio_output_selection", side_effect=cancel_target):
            with self.assertRaises(asyncio.CancelledError):
                await main.output_application.apply_candidate({"expected_revision": before["revision"],
                    "mutation": {"kind": "switch_mode", "mode": "surround"}})
        self.assertEqual(self.service.load()["modes"], before["modes"])
        self.assertEqual(self.service.load()["active_mode"], "stereo")
        self.assertEqual(self.sink, STEREO)

    def run_command(self, args, *rest, **kwargs):
        self.commands.append(list(args))
        if args[:4] == ["pactl", "-f", "json", "list"]:
            if args[4] == "cards":
                return json.dumps([self.card])
            return json.dumps([{"name": self.sink, "properties": {"alsa.device": "3"}}] if self.sink else [])
        if args == ["pactl", "list", "sinks", "short"]:
            raise AssertionError("sink presence uses the JSON listing")
        if args[:2] == ["pactl", "set-card-profile"]:
            profile = args[3]
            self.card["active_profile"] = profile
            output, _ = hdmi.split_card_profile(profile)
            match = hdmi._MAPPING.match(output or "")
            self.sink = (hdmi.sink_key(CARD, int(match["extra"] or 0), hdmi._BY_MAPPING[match["mapping"]]["id"])
                         if match else None)
            return ""
        if args == ["pw-link", "-i"]:
            return "\n".join(f"{self.sink}:{port}" for port in PORTS_71) if self.sink else ""
        raise AssertionError(f"unexpected command {args}")

    async def select(self, key, *, seed_routing=True, rollback_on_failure=True):
        self.selections.append(key)
        if seed_routing:
            await asyncio.to_thread(main._seed_hdmi_routing, key, self.overview())
        # The connector record is stored before the selection returns.
        self.connector_during_selection = hdmi.load_connectors().get(f"{CARD}#hdmi0")
        if key in self.fail_selection:
            raise RuntimeError("graph apply failed")
        channels = hdmi.transport(hdmi.parse_sink_key(key).transport)["channels"]
        state = await asyncio.to_thread(self.service.load)
        plan = await asyncio.to_thread(self.service.compile_plan, state, output_key=key, channels=channels, sample_rate_hz=44100)
        self.rendered_layout = await asyncio.to_thread(self.service.compile_layout, plan)
        _save_audio_output_selection(key)
        return self.overview()

    async def change(self, key=STEREO, transport="7.1"):
        try:
            return await main._owned_output_change(
                lambda: main._apply_hdmi_transport(hdmi.parse_sink_key(key), transport, remember=True),
                what="HDMI transport test")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    def profile_changes(self):
        return [command[3] for command in self.commands if command[:2] == ["pactl", "set-card-profile"]]

    async def test_layout_change_keeps_the_analog_input_of_the_connector(self):
        result = await self.change()
        self.assertEqual(result["selected_output"]["key"], SURROUND71)
        self.assertEqual(self.profile_changes(), ["output:hdmi-surround71+input:analog-stereo"])
        self.assertEqual(self.selections, [SURROUND71])
        self.assertEqual(hdmi.load_connectors()[f"{CARD}#hdmi0"],
                         {"input": "analog-stereo", "pcm_device": 3})
        self.assertEqual(self.connector_during_selection, {"input": "analog-stereo", "pcm_device": 3})

    async def test_failed_selection_restores_the_previous_profile_and_output(self):
        self.fail_selection.add(SURROUND71)
        with self.assertRaises(HTTPException) as failure:
            await self.change()
        self.assertEqual(failure.exception.status_code, 500)
        self.assertEqual(self.profile_changes(), ["output:hdmi-surround71+input:analog-stereo",
                                                  "output:hdmi-stereo+input:analog-stereo"])
        self.assertEqual(self.selections, [SURROUND71, STEREO])
        self.assertEqual(_load_audio_output_selection()["selected_key"], STEREO)
        self.assertEqual(hdmi.load_connectors(), {})

    async def test_failed_change_keeps_the_previous_choice(self):
        await self.change(transport="5.1")
        self.fail_selection.add(SURROUND71)
        with self.assertRaises(HTTPException):
            await self.change(key=hdmi.sink_key(CARD, 0, "5.1"))
        self.assertEqual(hdmi.load_connectors()[f"{CARD}#hdmi0"],
                         {"input": "analog-stereo", "pcm_device": 3})

    async def test_receiver_without_the_layout_is_refused(self):
        self.eld = tv_eld()
        with self.assertRaises(HTTPException) as failure:
            await self.change()
        self.assertEqual(failure.exception.status_code, 400)
        self.assertIn("at most 2 LPCM channels", failure.exception.detail)
        self.assertEqual(self.profile_changes(), [])

    async def test_measurement_blocks_the_change(self):
        self.session.has_active_jobs = True
        with self.assertRaises(HTTPException) as failure:
            await self.change()
        self.assertEqual(failure.exception.status_code, 423)
        self.assertEqual(self.profile_changes(), [])

    async def test_watch_restores_the_chosen_layout_after_a_lost_connection(self):
        await self.automatic("surround")
        self.commands.clear()
        self.selections.clear()
        # Receiver off: WirePlumber turns the card off.
        self.card = card(connected=False, active="off")
        self.sink = None
        self.eld = None
        await main._reconcile_hdmi_transport()
        # Receiver back, but WirePlumber restored Stereo.
        self.card = card(active="output:hdmi-stereo+input:analog-stereo")
        self.sink = STEREO
        self.eld = MARANTZ_ELD
        await main._reconcile_hdmi_transport()
        self.assertEqual(self.profile_changes(), [])
        await main._reconcile_hdmi_transport()
        self.assertEqual(self.profile_changes(), ["output:hdmi-surround71+input:analog-stereo"])
        self.assertEqual(self.selections, [SURROUND71])
        await main._reconcile_hdmi_transport()
        self.assertEqual(len(self.profile_changes()), 1)

    async def test_watch_skips_while_the_coordinator_is_busy(self):
        await self.change()
        self.card = card(active="output:hdmi-stereo+input:analog-stereo")
        self.sink = STEREO
        self.eld = tv_eld()
        self.commands.clear()
        async with self.coordinator.lock:
            await main._reconcile_hdmi_transport()
        self.assertEqual(self.commands, [])

    async def test_watch_skips_while_a_mode_switch_request_is_in_flight(self):
        await self.change()
        self.card = card(active="output:hdmi-stereo+input:analog-stereo")
        self.sink = STEREO
        self.eld = tv_eld()
        self.commands.clear()
        async with main._source_transition_lock():
            await main._reconcile_hdmi_transport()
        self.assertEqual(self.commands, [])

    async def test_transport_change_marks_the_graph_unstable_until_it_settles(self):
        observed = []
        original = main._apply_hdmi_transport_change

        async def observe(*args, **kwargs):
            observed.append((main.runtime.hdmi_transport_change_depth,
                             main.runtime.hdmi_transport_change_owner is asyncio.current_task()))
            return await original(*args, **kwargs)

        with mock.patch.object(main, "_apply_hdmi_transport_change", side_effect=observe):
            await self.change()
        self.assertEqual(observed, [(1, True)])
        self.assertEqual(main.runtime.hdmi_transport_change_depth, 0)
        self.assertIsNone(main.runtime.hdmi_transport_change_owner)

    async def test_transport_wait_is_bounded_and_reports_the_timeout(self):
        self.addCleanup(self.clear_transport_change)
        main.runtime.hdmi_transport_change_depth = 1
        with mock.patch.object(main, "HDMI_TRANSPORT_CHANGE_WAIT_SECONDS", 0.05):
            self.assertFalse(await main._wait_for_hdmi_transport_change("test"),
                             "A stuck change must report the timeout")
        self.clear_transport_change()
        self.assertTrue(await main._wait_for_hdmi_transport_change("test"))

    def switch_mode_request(self, mode="surround"):
        return SimpleNamespace(json=lambda: asyncio.sleep(0, result={
            "expected_revision": self.service.load()["revision"],
            "mutation": {"kind": "switch_mode", "mode": mode}}))

    def latency_patches(self):
        runtime = SimpleNamespace(snapshot=lambda: {"config": {"output_key": "key"}, "helper_pid": 1},
                                  sync_in_progress=False)
        return [mock.patch.object(main.runtime, "dsp_runtime", runtime),
                mock.patch.object(main, "_src_latency_reader", SimpleNamespace(read=lambda **kwargs: 0.0)),
                mock.patch.object(main, "_deterministic_input_latency_ms", return_value=0.0)]

    def clear_transport_change(self):
        main.runtime.hdmi_transport_change_depth = 0
        main.runtime.hdmi_transport_change_owner = None

    async def test_transition_latency_read_waits_out_a_transport_change(self):
        self.addCleanup(self.clear_transport_change)
        main.runtime.hdmi_transport_change_depth = 1
        runtime_patch, reader_patch, helper_patch = self.latency_patches()

        async def read():
            with main._hdmi_transition_scope():
                return await main._electronic_input_latency_ms()

        with runtime_patch, reader_patch, helper_patch:
            task = asyncio.create_task(read())
            await asyncio.sleep(0.2)
            self.assertFalse(task.done(), "A mode-switch SRC read must wait for the connector swap")
            self.clear_transport_change()
            self.assertEqual(await asyncio.wait_for(task, 5), 0.0)

    async def test_independent_latency_read_never_waits_for_a_transport_change(self):
        self.addCleanup(self.clear_transport_change)
        main.runtime.hdmi_transport_change_depth = 1
        runtime_patch, reader_patch, helper_patch = self.latency_patches()
        with runtime_patch, reader_patch, helper_patch:
            self.assertEqual(await asyncio.wait_for(main._electronic_input_latency_ms(), 1), 0.0)

    async def test_scoped_latency_read_reports_a_stuck_transport_change(self):
        self.addCleanup(self.clear_transport_change)
        main.runtime.hdmi_transport_change_depth = 1
        runtime_patch, reader_patch, helper_patch = self.latency_patches()
        with runtime_patch, reader_patch, helper_patch, \
                mock.patch.object(main, "HDMI_TRANSPORT_CHANGE_WAIT_SECONDS", 0.05), \
                main._hdmi_transition_scope(), \
                self.assertRaisesRegex(RuntimeError, "still in progress"):
            await main._electronic_input_latency_ms()

    async def test_transport_change_reads_its_own_input_latency_without_waiting(self):
        self.addCleanup(self.clear_transport_change)
        main.runtime.hdmi_transport_change_depth = 1
        main.runtime.hdmi_transport_change_owner = asyncio.current_task()
        runtime_patch, reader_patch, helper_patch = self.latency_patches()
        with runtime_patch, reader_patch, helper_patch:
            self.assertEqual(await asyncio.wait_for(main._electronic_input_latency_ms(), 1), 0.0)

    async def test_switch_mode_route_rejects_a_stuck_transport_change(self):
        applied = asyncio.Event()

        async def apply(body):
            applied.set()
            return {"status": "ok"}

        self.addCleanup(self.clear_transport_change)
        main.runtime.hdmi_transport_change_depth = 1
        with mock.patch.object(main, "HDMI_TRANSPORT_CHANGE_WAIT_SECONDS", 0.05), \
                mock.patch.object(main.output_application, "apply", side_effect=apply):
            with self.assertRaises(HTTPException) as failure:
                await main.apply_audio_output_state(self.switch_mode_request())
        self.assertEqual(failure.exception.status_code, 503)
        self.assertFalse(applied.is_set(), "A stuck change must stop the transition")

    async def test_migrated_routing_cannot_request_a_wider_transport(self):
        self.eld = surround_eld()
        legacy = ["main_l", "main_r", "side_l", "side_r", "center", "sub1", "side_l", "side_r"]
        state = set_mode_routing(switch_mode(self.service.load(), "surround"), "surround", "legacy-device", legacy)
        state["modes"]["surround"]["routing"][SURROUND71] = \
            state["modes"]["surround"]["routing"].pop("legacy-device")
        self.service.commit_unowned(state, expected_revision=self.service.load()["revision"])
        main._seed_hdmi_routing(self.sink, self.overview())
        connector = f"{CARD}#hdmi0"
        self.assertEqual(self.service.load()["modes"]["surround"]["routing"][connector], legacy[:6])
        # The 5.1 receiver reaches the transport its bounded routing needs.
        await main._reconcile_hdmi_transport()
        await main._reconcile_hdmi_transport()
        self.assertEqual(self.profile_changes(), ["output:hdmi-surround+input:analog-stereo"])
        self.assertEqual(self.service.load()["modes"]["surround"]["routing"][connector], legacy[:6])

    async def test_switch_mode_route_waits_out_a_transport_change_before_applying(self):
        applied = asyncio.Event()

        async def apply(body):
            applied.set()
            return {"status": "ok", "revision": 1}

        main.runtime.hdmi_transport_change_depth = 1
        try:
            with mock.patch.object(main.output_application, "apply", side_effect=apply):
                task = asyncio.create_task(main.apply_audio_output_state(self.switch_mode_request()))
                await asyncio.sleep(0.2)
                self.assertFalse(applied.is_set(), "The transition must not start mid change")
                main.runtime.hdmi_transport_change_depth = 0
                self.assertEqual(await asyncio.wait_for(task, 5), {"status": "ok", "revision": 1})
        finally:
            main.runtime.hdmi_transport_change_depth = 0
        self.assertTrue(applied.is_set())

    async def test_non_hdmi_selection_never_enumerates_the_overview_or_connector(self):
        _save_audio_output_selection("alsa_output.usb-other.analog-stereo")
        with mock.patch.object(main, "get_audio_output_overview", side_effect=AssertionError("Redundant overview")):
            await main._reconcile_hdmi_transport()
        self.assertEqual(self.commands, [])

    async def test_watch_cancellation_drains_its_seeding_worker(self):
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        probe = main._probe_hdmi_connector
        snapshot = main._hdmi_watch_snapshot
        def draining_snapshot():
            try:
                return snapshot()
            finally:
                completed.set()
        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("Probe was not released")
            return probe(*args, **kwargs)
        with mock.patch.object(main, "_probe_hdmi_connector", side_effect=delayed), \
                mock.patch.object(main, "_hdmi_watch_snapshot", side_effect=draining_snapshot):
            task = asyncio.create_task(main._reconcile_hdmi_transport())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                task.cancel()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assertFalse(task.done(), "Cancellation must drain pending routing persistence")
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(await asyncio.to_thread(completed.wait, 2))
        self.assertIn(hdmi.routing_key(STEREO), self.service.load()["modes"]["surround"]["routing"])

    async def test_initialized_connector_watch_skips_overview_and_seeding(self):
        await self.automatic("surround")
        self.commands.clear()
        with mock.patch.object(main, "get_audio_output_overview", side_effect=AssertionError("Redundant overview")), \
                mock.patch.object(main, "_seed_hdmi_routing", side_effect=AssertionError("Redundant seeding")):
            for _ in range(3):
                await main._reconcile_hdmi_transport()
        self.assertEqual(len([command for command in self.commands if command[:4] == ["pactl", "-f", "json", "list"]]), 6)

    async def test_stereo_only_receiver_never_retries_surround_routing_seeding(self):
        self.eld = tv_eld()
        with mock.patch.object(main, "get_audio_output_overview", side_effect=AssertionError("Redundant stereo overview")):
            for _ in range(3):
                await main._reconcile_hdmi_transport()
        self.assertEqual(self.service.load()["revision"], 0)

    async def test_hdmi_service_work_does_not_run_on_the_event_loop(self):
        main._seed_hdmi_routing(self.sink, self.overview())
        state = self.service.load()
        candidate = switch_mode(state, "surround")
        overview = self.overview()
        loop_thread = threading.get_ident()
        with mock.patch.multiple(self.service, **{
                name: self.off_loop(getattr(self.service, name), loop_thread)
                for name in ("load", "compile_plan", "compile_layout", "commit_unowned", "revert")}):
            await main._apply_hdmi_output_state({"mutation": {"kind": "switch_mode"}}, state, candidate, overview)
            await main._reconcile_hdmi_transport()
        self.assertEqual(self.sink, SURROUND71)

    def off_loop(self, operation, loop_thread):
        def run(*args, **kwargs):
            self.assertNotEqual(threading.get_ident(), loop_thread, operation.__name__)
            return operation(*args, **kwargs)
        return run


class SeedRoutingIntegrationTests(unittest.TestCase):
    def test_first_play_stores_the_port_defaults(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}), \
                mock.patch.object(main, "_output_service_instance", None), \
                mock.patch.object(main, "measurement_sr_session", None):
            main._seed_hdmi_routing(SURROUND71, {"output_mode": {"hardware_playback_ports": PORTS_71}})
            state = main.get_output_service().load()
            self.assertEqual(routing_for_device(state, "surround", SURROUND71, 8),
                             ["main_l", "main_r", "rear_l", "rear_r", "center", "sub1", "side_l", "side_r"])
            revision = state["revision"]
            main._seed_hdmi_routing(SURROUND71, {"output_mode": {"hardware_playback_ports": PORTS_71}})
            self.assertEqual(main.get_output_service().load()["revision"], revision)
            main._seed_hdmi_routing(STEREO, {"output_mode": {"hardware_playback_ports": PORTS_71[:2]}})
            self.assertEqual(main.get_output_service().load()["revision"], revision)


if __name__ == "__main__":
    unittest.main()
