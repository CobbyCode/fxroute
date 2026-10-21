#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Replacement sinks retain raw levels without inheriting unrelated devices."""
import copy
import json
import sys
import unittest
import asyncio
import threading
import tempfile
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audio.output_levels import OutputLevels
from audio.system_volume import SystemVolumeError

OLD = 'alsa_output.pci-test.hdmi-stereo'
NEW = 'alsa_output.pci-test.hdmi-surround'
OTHER = 'alsa_output.usb-other.analog-stereo'


def sink(name, index, volumes, muted):
    positions = ['front-left', 'front-right', 'rear-left', 'rear-right', 'front-center', 'lfe'][:len(volumes)]
    return {'name': name, 'index': index, 'mute': muted,
            'volume': {position: {'value': value, 'value_percent': '26%', 'db': '-35.10 dB'}
                       for position, value in zip(positions, volumes)}}


class OutputLevelsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sinks = {OLD: sink(OLD, 10, [17039, 16000], True)}
        self.writes = []
        self.levels = OutputLevels(command_runner=self.command, selected_key=lambda: OLD)
        self.cookie = 'test-server'
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state_path = Path(self.directory.name) / 'output-switch.json'

    def persistent_levels(self):
        return OutputLevels(command_runner=self.command, selected_key=lambda: OLD,
                            state_path=self.state_path)

    def command(self, args):
        if args == ['pactl', '-f', 'json', 'info']:
            return json.dumps({'cookie': self.cookie})
        if args == ['pactl', '-f', 'json', 'list', 'sinks']:
            return json.dumps(list(self.sinks.values()))
        self.writes.append(args)
        target = self.sinks[args[2]]
        if args[1] == 'set-sink-mute':
            target['mute'] = args[3] == '1'
        elif args[1] == 'set-sink-volume':
            for row, value in zip(target['volume'].values(), args[3:]):
                row['value'] = int(value)
        else:
            self.fail(f'Unexpected command: {args}')
        return ''

    async def test_crashed_switch_restores_only_its_own_mute_after_graph_success(self):
        for user_muted in (False, True):
            with self.subTest(user_muted=user_muted):
                self.sinks = {OLD: sink(OLD, 10, [17039, 16000], user_muted)}
                levels = self.persistent_levels()
                levels.observe_selected()
                self.sinks = {NEW: sink(NEW, 11, [65536] * 6, False)}
                original = levels._run
                def recorded(args):
                    if args[1] == 'set-sink-mute':
                        self.assertTrue(self.state_path.exists(), 'Ownership must precede mute')
                    return original(args)
                levels._run = recorded
                levels._begin(OLD, NEW)
                self.assertTrue(self.sinks[NEW]['mute'])
                restarted = self.persistent_levels()
                restarted.recover_pending()
                self.assertTrue(self.sinks[NEW]['mute'], 'Startup must not unmute before graph verification')
                with self.assertRaisesRegex(RuntimeError, 'Graph failed'):
                    async with restarted.switch(NEW, NEW):
                        raise RuntimeError('Graph failed')
                self.assertTrue(self.state_path.exists())
                async with restarted.switch(NEW, NEW):
                    self.assertTrue(self.sinks[NEW]['mute'])
                self.assertEqual(self.sinks[NEW]['mute'], user_muted)
                self.assertFalse(self.state_path.exists())

    async def test_restart_never_claims_unowned_or_replaced_hardware_mute(self):
        for change in ('no-marker', 'node', 'server', 'user-unmute', 'coordinator'):
            with self.subTest(change=change):
                self.sinks = {OLD: sink(OLD, 10, [17039, 16000], False)}
                levels = self.persistent_levels()
                levels.observe_selected()
                self.sinks = {NEW: sink(NEW, 11, [65536] * 6, False)}
                levels._begin(OLD, NEW)
                if change == 'no-marker':
                    self.state_path.unlink()
                elif change == 'node':
                    self.sinks[NEW]['index'] = 12
                elif change == 'server':
                    self.cookie += '-new'
                elif change == 'user-unmute':
                    self.sinks[NEW]['mute'] = False
                restarted = self.persistent_levels()
                restarted.recover_pending(mute_restored=change == 'coordinator')
                async with restarted.switch(NEW, NEW):
                    pass
                self.assertEqual(self.sinks[NEW]['mute'], change != 'user-unmute')

    async def test_marker_write_failure_prevents_temporary_mute(self):
        levels = self.persistent_levels()
        self.sinks[OTHER] = sink(OTHER, 20, [22222, 22222], False)
        with mock.patch('audio.output_levels.atomic_write_text', side_effect=OSError('Disk full')):
            with self.assertRaisesRegex(OSError, 'Disk full'):
                async with levels.switch(OLD, OTHER):
                    self.fail('Unrecorded mute must not enter graph scope')
        self.assertFalse(self.sinks[OTHER]['mute'])
        self.assertFalse(levels._switching)

    async def test_unreachable_server_preserves_recovery_marker_until_a_later_attempt(self):
        for command in ('info', 'sinks'):
            with self.subTest(command=command):
                levels = self.persistent_levels()
                self.sinks[OTHER] = sink(OTHER, 20, [22222, 22222], False)
                state = levels._begin(OLD, OTHER)
                marker = self.state_path.read_bytes()
                restarted = self.persistent_levels()
                def unavailable(args):
                    if args[-1] == command:
                        raise SystemVolumeError('Audio server unavailable')
                    return self.command(args)
                restarted._run = unavailable
                with self.assertLogs('audio.output_levels', level='WARNING'):
                    restarted.recover_pending()
                self.assertEqual(self.state_path.read_bytes(), marker)
                self.assertTrue(self.sinks[OTHER]['mute'])
                restarted._run = self.command
                restarted.recover_pending()
                async with restarted.switch(OTHER, OTHER):
                    pass
                self.assertFalse(self.sinks[OTHER]['mute'])
                self.assertFalse(self.state_path.exists())

    async def test_failed_initial_marker_does_not_overwrite_a_later_user_volume(self):
        levels = self.persistent_levels()
        self.sinks[OTHER] = sink(OTHER, 20, [20000, 20000], False)
        with mock.patch('audio.output_levels.atomic_write_text', side_effect=OSError('Disk full')):
            with self.assertRaises(OSError):
                async with levels.switch(OLD, OTHER):
                    pass
        for row in self.sinks[OTHER]['volume'].values():
            row['value'] = 8000
        levels._selected_key = lambda: OTHER
        levels.observe_selected()
        async with levels.switch(OTHER, OTHER):
            pass
        self.assertEqual([row['value'] for row in self.sinks[OTHER]['volume'].values()], [8000, 8000])
        self.assertFalse(self.sinks[OTHER]['mute'])

    async def test_replacement_safety_mute_survives_marker_write_failure(self):
        for server_changed in (False, True):
            with self.subTest(server_changed=server_changed):
                levels = self.persistent_levels()
                self.sinks[OTHER] = sink(OTHER, 20, [20000, 20000], False)
                with self.assertRaises(RuntimeError) as raised:
                    async with levels.switch(OLD, OTHER):
                        self.sinks[OTHER] = sink(OTHER, 21, [65536, 65536], False)
                        if server_changed:
                            self.cookie += '-new'
                        with mock.patch('audio.output_levels.atomic_write_text', side_effect=OSError('Disk full')):
                            levels._finish(OTHER, levels._pending[OTHER], True)
                self.assertTrue(self.sinks[OTHER]['mute'], 'Marker I/O must not suppress the safety mute')
                self.assertTrue(any('Disk full' in note for note in raised.exception.__notes__))

    async def test_successful_finish_keeps_user_levels_when_marker_update_or_unlink_fails(self):
        for failure in ('write', 'unlink'):
            with self.subTest(failure=failure):
                levels = self.persistent_levels()
                self.sinks[OTHER] = sink(OTHER, 20, [20000, 20000], False)
                state = levels._begin(OLD, OTHER)
                self.sinks[OTHER]['volume']['front-left']['value'] = 30000
                target = ('audio.output_levels.atomic_write_text' if failure == 'write'
                          else 'audio.output_levels.Path.unlink')
                with mock.patch(target, side_effect=OSError('Disk full')), \
                        self.assertLogs('audio.output_levels', level='WARNING'):
                    levels._finish(OTHER, state, True)
                self.assertFalse(self.sinks[OTHER]['mute'])
                self.assertEqual(self.sinks[OTHER]['volume']['front-left']['value'], 30000)
                self.assertFalse(levels._pending)
                self.state_path.unlink(missing_ok=True)

    async def test_stale_marker_cannot_replay_old_volume_after_a_successful_finish(self):
        levels = self.persistent_levels()
        self.sinks[OTHER] = sink(OTHER, 20, [20000, 20000], True)
        state = levels._begin(OLD, OTHER)
        for row in self.sinks[OTHER]['volume'].values():
            row['value'] = 8000
        with mock.patch('audio.output_levels.atomic_write_text', side_effect=OSError('Read-only storage')), \
                mock.patch('audio.output_levels.Path.unlink', side_effect=OSError('Read-only storage')), \
                self.assertLogs('audio.output_levels', level='WARNING'):
            levels._finish(OTHER, state, True)
        restarted = self.persistent_levels()
        restarted.recover_pending()
        async with restarted.switch(OTHER, OTHER):
            pass
        self.assertTrue(self.sinks[OTHER]['mute'], 'Uncertain hardware mute must remain user-owned')
        self.assertEqual([row['value'] for row in self.sinks[OTHER]['volume'].values()], [8000, 8000])

    async def test_server_restart_invalidates_pending_ownership_without_blocking_new_sync(self):
        self.sinks[OLD]['mute'] = False
        levels = self.persistent_levels()
        levels.observe_selected()
        self.sinks = {NEW: sink(NEW, 11, [65536] * 6, False)}
        levels._begin(OLD, NEW)
        self.cookie = 'replacement-server'
        self.sinks = {NEW: sink(NEW, 11, [22222] * 6, True)}
        for _ in range(2):
            async with levels.switch(NEW, NEW):
                pass
        self.assertTrue(self.sinks[NEW]['mute'], 'New-server hardware mute belongs to the user')
        self.assertEqual(self.sinks[NEW]['volume']['front-left']['value'], 22222)
        self.assertFalse(self.state_path.exists())

    async def test_server_restart_inside_switch_records_fresh_safety_mute_ownership(self):
        self.sinks[OLD]['mute'] = False
        levels = self.persistent_levels()
        levels.observe_selected()
        self.sinks = {NEW: sink(NEW, 11, [65536] * 6, False)}
        with self.assertRaisesRegex(RuntimeError, 'Audio server changed'):
            async with levels.switch(OLD, NEW):
                self.cookie = 'new-server-during-sync'
                self.sinks = {NEW: sink(NEW, 11, [22222] * 6, False)}
        self.assertTrue(self.sinks[NEW]['mute'])
        self.assertTrue(self.state_path.exists(), 'Any new safety mute must have fresh ownership')
        for _ in range(2):
            async with levels.switch(NEW, NEW):
                pass
        self.assertFalse(self.sinks[NEW]['mute'])
        self.assertEqual(self.sinks[NEW]['volume']['front-left']['value'], 22222)
        self.assertFalse(self.state_path.exists())

    async def test_malformed_marker_cannot_abort_startup_or_claim_hardware_mute(self):
        levels = self.persistent_levels()
        self.state_path.write_text(json.dumps({'version': 1, 'owner': 'fxroute-output-switch',
                                               'server_cookie': self.cookie, 'sinks': []}))
        levels.recover_pending()
        self.assertFalse(self.state_path.exists())
        self.assertTrue(self.sinks[OLD]['mute'])
        self.assertFalse(self.writes)

    async def test_recovered_switch_must_not_open_a_new_safety_hold(self):
        levels = self.persistent_levels()
        self.sinks[OTHER] = sink(OTHER, 20, [22222, 22222], False)
        levels._begin(OLD, OTHER)
        restarted = self.persistent_levels()
        restarted.recover_pending()
        restarted._mute_held = lambda: True
        async with restarted.switch(OTHER, OTHER):
            pass
        self.assertTrue(self.sinks[OTHER]['mute'])

    async def test_owned_user_intent_is_not_transferred_to_a_replaced_node(self):
        levels = self.persistent_levels()
        self.sinks[OTHER] = sink(OTHER, 20, [22222, 22222], False)
        levels._begin(OLD, OTHER)
        self.assertFalse(levels.user_mute_for(OTHER))
        self.assertIsNone(levels.user_mute_for(NEW))
        self.sinks[OTHER]['index'] = 21
        self.assertIsNone(levels.user_mute_for(OTHER))

    async def test_fixed_policy_startup_gate_adopts_recovered_user_mute_not_temporary_mute(self):
        import main
        from playback.runtime import FxrouteTransitionRuntime
        from playback.transition import PlaybackTransitionCoordinator, TransitionRequest
        from test_playback_transition_coordinator import FakeRuntime
        self.sinks[OLD]['mute'] = False
        levels = self.persistent_levels()
        levels.observe_selected()
        self.sinks = {NEW: sink(NEW, 11, [65536] * 6, False)}
        levels._begin(OLD, NEW)
        restarted = self.persistent_levels()
        restarted.recover_pending()
        sinks = self.sinks
        adapter = FxrouteTransitionRuntime(main.make_playback_runtime_deps())
        adapter._resolved_gate_sink = NEW

        class StartupRuntime(FakeRuntime):
            async def read_hardware_mute(self):
                return sinks[NEW]['mute']
            async def read_user_hardware_mute(self):
                return await adapter.read_user_hardware_mute()
            async def set_hardware_mute(self, muted, transition_id):
                sinks[NEW]['mute'] = muted
            async def establish_effects_and_helper(self, request):
                async with restarted.switch(NEW, NEW):
                    pass
                return {'helper_rebuilt': True}
            async def restore_output_mode_transport(self, *args):
                pass
            async def reconcile_post_start_graph(self, *args):
                return {'graph_complete': True}
            async def verify_output_mode_runtime(self, *args):
                return {'committed': True}
            async def commit_sample_rate_policy(self, *args):
                return {'policy': {'mode': 'fixed', 'rate': 48000}}

        coordinator = PlaybackTransitionCoordinator(StartupRuntime(), gate_settle_seconds=0,
            gate_state_path=Path(self.directory.name) / 'coordinator-gate.json')
        restarted._mute_held = lambda: coordinator.gate.closed
        with mock.patch.object(main, '_output_levels', restarted), \
                mock.patch('playback.runtime.mute._read_hardware_sink_mute', side_effect=lambda key: sinks[key]['mute']):
            self.assertTrue(await coordinator.reconcile_startup_gate())
            result = await coordinator.execute(TransitionRequest(operation='sample-rate-policy', source='local',
                target_rate=48000, should_play=False, rate_change=True, reload_source=False))
        self.assertTrue(result.committed)
        self.assertFalse(self.sinks[NEW]['mute'], 'Temporary mute must not become the coordinator user mute')
        self.assertFalse(restarted._pending)
        self.assertFalse(self.state_path.exists())

    async def test_vanished_sink_transfers_raw_levels_before_linking(self):
        self.levels.observe_selected()
        self.sinks = {NEW: sink(NEW, 11, [65536] * 6, False)}
        async with self.levels.switch(OLD, NEW):
            self.assertTrue(self.sinks[NEW]['mute'])
            self.assertEqual([row['value'] for row in self.sinks[NEW]['volume'].values()],
                             [17039, 16000, 17039, 17039, 17039, 17039])
            self.levels.observe_selected()
        self.assertTrue(self.sinks[NEW]['mute'])

    async def test_unmuted_state_opens_only_after_success(self):
        self.sinks[OLD]['mute'] = False
        self.levels.observe_selected()
        self.sinks = {NEW: sink(NEW, 11, [65536] * 6, True)}
        async with self.levels.switch(OLD, NEW):
            self.assertTrue(self.sinks[NEW]['mute'])
        self.assertFalse(self.sinks[NEW]['mute'])

    async def test_replacement_never_opens_an_owned_coordinator_gate(self):
        self.sinks[OLD]['mute'] = False
        self.levels.observe_selected()
        self.sinks = {NEW: sink(NEW, 11, [65536] * 6, False)}
        self.levels._mute_held = lambda: True
        async with self.levels.switch(OLD, NEW):
            pass
        self.assertTrue(self.sinks[NEW]['mute'])

    async def test_user_level_change_during_switch_is_not_a_sink_replacement(self):
        for unmute in (False, True):
            with self.subTest(unmute=unmute):
                self.sinks = {OLD: sink(OLD, 10, [17039, 16000], unmute)}
                self.levels.observe_selected()
                self.sinks = {NEW: sink(NEW, 11, [65536] * 6, False)}
                async with self.levels.switch(OLD, NEW):
                    self.sinks[NEW]['volume']['front-left']['value'] = 12345
                    if unmute:
                        self.sinks[NEW]['mute'] = False
                self.assertFalse(self.sinks[NEW]['mute'])
                self.assertEqual(self.sinks[NEW]['volume']['front-left']['value'], 12345)
                self.assertFalse(self.levels._pending)
                self.sinks = {NEW: sink(NEW, 12, [65536] * 6, False)}
                async with self.levels.switch(NEW, NEW):
                    pass
                self.assertEqual(self.sinks[NEW]['volume']['front-left']['value'], 12345)
                self.assertFalse(self.sinks[NEW]['mute'])

    async def test_failed_graph_never_opens_replacement_and_retry_keeps_original_mute(self):
        self.sinks[OLD]['mute'] = False
        self.levels.observe_selected()
        self.sinks = {NEW: sink(NEW, 11, [65536] * 6, False)}
        with self.assertRaisesRegex(RuntimeError, 'graph failed'):
            async with self.levels.switch(OLD, NEW):
                raise RuntimeError('graph failed')
        self.assertTrue(self.sinks[NEW]['mute'])
        async with self.levels.switch(OLD, NEW):
            self.assertTrue(self.sinks[NEW]['mute'])
        self.assertFalse(self.sinks[NEW]['mute'])

    async def test_cleanup_failure_cannot_mask_original_graph_error_or_cancellation(self):
        for primary in (RuntimeError('Graph failed'), asyncio.CancelledError('Switch cancelled')):
            with self.subTest(primary=primary):
                with mock.patch.object(self.levels, '_finish', side_effect=RuntimeError('Cleanup failed')), \
                        self.assertLogs('audio.output_levels', level='ERROR'):
                    with self.assertRaises(type(primary)) as raised:
                        async with self.levels.switch(OLD, OLD):
                            raise primary
                self.assertIs(raised.exception, primary)
                self.assertTrue(any('Cleanup failed' in note for note in primary.__notes__))

    async def test_finish_mute_failure_preserves_the_failed_verification(self):
        self.sinks[OLD]['mute'] = False
        self.levels.observe_selected()
        self.sinks = {NEW: sink(NEW, 11, [65536] * 6, False)}
        with self.assertRaisesRegex(RuntimeError, 'Hardware output changed during sync') as raised, \
                self.assertLogs('audio.output_levels', level='ERROR'):
            async with self.levels.switch(OLD, NEW):
                self.sinks[NEW]['index'] = 12
                self.levels._mute = mock.Mock(side_effect=RuntimeError('Safety mute failed'))
        self.assertFalse(self.levels._switching)
        self.assertTrue(any('Safety mute failed' in note for note in raised.exception.__notes__))

    async def test_failed_replacement_carries_pending_levels_to_the_next_successor(self):
        self.sinks[OLD]['mute'] = False
        self.levels.observe_selected()
        self.sinks = {NEW: sink(NEW, 11, [65536] * 6, False)}
        with self.assertRaises(RuntimeError):
            async with self.levels.switch(OLD, NEW):
                raise RuntimeError('New graph failed')
        next_key = 'alsa_output.pci-test.hdmi-surround71'
        self.sinks = {next_key: sink(next_key, 12, [65536] * 6, False)}
        async with self.levels.switch(NEW, next_key):
            self.assertTrue(self.sinks[next_key]['mute'])
        self.assertFalse(self.sinks[next_key]['mute'])
        self.assertEqual([row['value'] for row in self.sinks[next_key]['volume'].values()],
                         [17039, 16000, 17039, 17039, 17039, 17039])
        self.assertNotIn(NEW, self.levels._pending)

    async def test_ordinary_sync_does_not_capture_an_owned_gate_as_user_mute(self):
        self.sinks[OLD]['mute'] = False
        self.levels.observe_selected()
        self.sinks[OLD]['mute'] = True
        self.levels._mute_held = lambda: True
        async with self.levels.switch(OLD, OLD):
            pass
        self.levels._mute_held = lambda: False
        self.sinks = {NEW: sink(NEW, 11, [65536] * 6, False)}
        async with self.levels.switch(OLD, NEW):
            pass
        self.assertFalse(self.sinks[NEW]['mute'])

    async def test_unobserved_sink_is_muted_before_sync_when_gate_is_owned(self):
        self.sinks[OLD]['mute'] = False
        self.levels._mute_held = lambda: True
        async with self.levels.switch(OLD, OLD):
            self.assertTrue(self.sinks[OLD]['mute'])
        self.assertTrue(self.sinks[OLD]['mute'])

    async def test_observation_rechecks_gate_ownership_after_inventory(self):
        self.sinks[OLD]['mute'] = False
        self.levels.observe_selected()
        held = [False]
        self.levels._mute_held = lambda: held[0]
        original = self.levels._run

        def gated_read(args):
            held[0] = True
            self.sinks[OLD]['mute'] = True
            return original(args)

        self.levels._run = gated_read
        self.levels.observe_selected()
        self.levels._run = original
        held[0] = False
        self.sinks = {NEW: sink(NEW, 11, [65536] * 6, False)}
        async with self.levels.switch(OLD, NEW):
            pass
        self.assertFalse(self.sinks[NEW]['mute'])

    async def test_observation_rejects_a_gate_episode_that_already_finished(self):
        self.sinks[OLD]['mute'] = False
        generation = [0]
        self.levels._observation_owner = lambda: generation[0]
        self.levels.observe_selected()
        original = self.levels._run
        def crossed_gate(args):
            generation[0] += 1
            self.sinks[OLD]['mute'] = True
            return original(args)
        self.levels._run = crossed_gate
        self.levels.observe_selected()
        self.levels._run = original
        self.sinks = {NEW: sink(NEW, 11, [65536] * 6, False)}
        async with self.levels.switch(OLD, NEW):
            pass
        self.assertFalse(self.sinks[NEW]['mute'])

    async def test_pending_intent_survives_a_handoff_that_fails_before_config_adoption(self):
        self.levels.observe_selected()
        self.sinks = {NEW: sink(NEW, 11, [65536] * 6, False)}
        with self.assertRaises(RuntimeError):
            async with self.levels.switch(OLD, NEW):
                raise RuntimeError('Latency failed after config adoption')
        next_key = 'alsa_output.pci-test.hdmi-surround71'
        self.sinks = {next_key: sink(next_key, 12, [65536] * 6, False)}
        with self.assertRaises(RuntimeError):
            async with self.levels.switch(NEW, next_key):
                raise RuntimeError('Preflight failed before config adoption')
        self.sinks = {OLD: sink(OLD, 13, [65536, 65536], False)}
        async with self.levels.switch(NEW, OLD):
            pass
        self.assertTrue(self.sinks[OLD]['mute'])
        self.assertEqual([row['value'] for row in self.sinks[OLD]['volume'].values()], [17039, 16000])
        self.assertFalse(self.levels._pending)

    async def test_same_name_pending_levels_are_remapped_when_channel_capacity_changes(self):
        for before, after, expected in (([17039, 16000], 6, [17039, 16000, 17039, 17039, 17039, 17039]),
                                        ([17039, 16000, 15000, 14000, 13000, 12000], 2, [17039, 16000])):
            with self.subTest(before=len(before), after=after):
                self.levels = OutputLevels(command_runner=self.command, selected_key=lambda: OLD)
                self.sinks = {OLD: sink(OLD, 10, before, False)}
                self.levels.observe_selected()
                self.sinks[OLD] = sink(OLD, 11, [65536] * len(before), False)
                with self.assertRaisesRegex(RuntimeError, 'Graph failed'):
                    async with self.levels.switch(OLD, OLD):
                        raise RuntimeError('Graph failed')
                self.sinks[OLD] = sink(OLD, 12, [65536] * after, False)
                async with self.levels.switch(OLD, OLD):
                    pass
                self.assertEqual([row['value'] for row in self.sinks[OLD]['volume'].values()], expected)
                self.assertFalse(self.sinks[OLD]['mute'])

    async def test_latency_failure_after_relink_remains_recoverable_despite_valid_links(self):
        from types import SimpleNamespace
        from dsp.runtime import DSPRuntime, DSPRuntimeConfig, CommandResult
        self.sinks[OLD]['mute'] = False
        self.levels.observe_selected()
        self.sinks[OLD] = sink(OLD, 12, [65536, 65536], False)
        links = set()

        async def run(args):
            if args == ('pw-link', '-l'):
                return CommandResult(0, '\n'.join(f'{a}\n  |-> {b}' for a, b in links))
            links.add(tuple(args[1:]))
            return CommandResult(0)

        runtime = DSPRuntime(None, command_runner=run)
        runtime._process = SimpleNamespace(returncode=None, pid=123)
        runtime._config = DSPRuntimeConfig('stereo', OLD, 48000, ('playback_FL', 'playback_FR'), ())
        runtime.configure_output_level_tracking(self.levels.switch)
        failing = [True]
        def latency():
            if failing[0]:
                raise RuntimeError('SRC evidence unavailable')
            return 250
        async def control(*_args, **_kwargs):
            return 'ok'
        runtime._control_unlocked = control
        runtime.configure_latency_tracking(input_latency_provider=latency)
        with self.assertRaisesRegex(RuntimeError, 'SRC evidence unavailable'):
            await runtime.reclean_direct_dsp_links()
        self.assertTrue(await runtime.verify())
        self.assertFalse(runtime.snapshot()['active'], 'Pending latency must force authority recovery')
        from dataclasses import replace
        from dsp.orchestration import DspOrchestrator
        from test_runtime_watch import _watcher_deps
        deps = _watcher_deps(SimpleNamespace(get_dsp_runtime=lambda: runtime,
            get_audio_output_overview=lambda: {}, observe_playback_samplerate_drift=None, sleep=None))
        deps = replace(deps, read_output_default_state=lambda: (OLD, False))
        self.assertTrue(await DspOrchestrator(deps)._output_authority_needs_repair())
        self.assertTrue(self.sinks[OLD]['mute'])
        failing[0] = False
        await runtime.reclean_direct_dsp_links()
        self.assertTrue(runtime.snapshot()['active'])
        self.assertFalse(self.sinks[OLD]['mute'])

    async def test_same_name_new_node_does_not_overwrite_last_known_levels(self):
        self.levels.observe_selected()
        self.sinks[OLD] = sink(OLD, 12, [65536, 65536], False)
        self.levels.observe_selected()
        async with self.levels.switch(OLD, OLD):
            pass
        self.assertTrue(self.sinks[OLD]['mute'])
        self.assertEqual([row['value'] for row in self.sinks[OLD]['volume'].values()], [17039, 16000])

    async def test_same_name_runtime_link_repair_restores_levels_before_connecting(self):
        from dsp.runtime import DSPRuntime, DSPRuntimeConfig, CommandResult
        self.levels.observe_selected()
        self.sinks[OLD] = sink(OLD, 12, [65536, 65536], False)
        links = set()

        async def run(args):
            if args == ('pw-link', '-l'):
                return CommandResult(0, '\n'.join(f'{a}\n  |-> {b}' for a, b in links))
            if args[0] == 'pw-link' and len(args) == 3:
                self.assertTrue(self.sinks[OLD]['mute'], 'Replacement must be gated before relinking')
                links.add(tuple(args[1:]))
                return CommandResult(0)
            self.fail(f'Unexpected command: {args}')

        runtime = DSPRuntime(None, command_runner=run)
        runtime._config = DSPRuntimeConfig('stereo', OLD, 48000, ('playback_FL', 'playback_FR'), ())
        runtime.configure_output_level_tracking(self.levels.switch)
        await runtime.reclean_direct_dsp_links()
        self.assertEqual(runtime.snapshot()['graph_revision'], 1)
        self.assertEqual([row['value'] for row in self.sinks[OLD]['volume'].values()], [17039, 16000])
        self.assertTrue(self.sinks[OLD]['mute'])

    async def test_normal_sync_does_not_gate_or_change_live_user_levels(self):
        self.levels.observe_selected()
        self.sinks[OLD]['mute'] = False
        self.sinks[OLD]['volume']['front-left']['value'] = 12345
        expected = copy.deepcopy(self.sinks)
        async with self.levels.switch(OLD, OLD):
            pass
        self.assertEqual(self.sinks, expected)
        self.assertEqual(self.writes, [])

    async def test_virtual_split_group_keeps_existing_group_controls(self):
        async with self.levels.switch('alsa_output.usb-card.HiFi__combined',
                                      'alsa_output.usb-card.HiFi__combined'):
            pass
        self.assertEqual(self.writes, [])

    async def test_cancellation_during_mute_is_drained_before_scope_releases(self):
        self.levels.observe_selected()
        self.sinks = {NEW: sink(NEW, 11, [65536] * 6, False)}
        entered, release = threading.Event(), threading.Event()
        original = self.levels._run

        def delayed(args):
            if args[1] == 'set-sink-mute':
                entered.set()
                if not release.wait(2):
                    raise RuntimeError('Test mute was not released')
            return original(args)

        self.levels._run = delayed
        async def switch():
            async with self.levels.switch(OLD, NEW):
                self.fail('Cancelled switch must not link outputs')
        task = asyncio.create_task(switch())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(self.sinks[NEW]['mute'])
        async with self.levels.switch(OLD, NEW):
            pass

    async def test_unrelated_or_ambiguous_sink_never_inherits_old_levels(self):
        self.levels.observe_selected()
        for target, live in ((OTHER, {OTHER: sink(OTHER, 20, [22222, 22222], False)}),
                             (NEW, {NEW: sink(NEW, 11, [33333] * 6, False),
                                    OLD.replace('stereo', 'surround71'): sink(OLD.replace('stereo', 'surround71'), 12, [44444] * 6, False)})):
            self.sinks = live
            before = copy.deepcopy(self.sinks)
            async with self.levels.switch(OLD, target):
                pass
            self.assertEqual(self.sinks, before)


if __name__ == '__main__':
    unittest.main()
