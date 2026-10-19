#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Replacement sinks retain raw levels without inheriting unrelated devices."""
import copy
import json
import sys
import unittest
import asyncio
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audio.output_levels import OutputLevels

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

    def command(self, args):
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
