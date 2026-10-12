#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Live AV Sync classification, readback and measurement timing safety."""
import asyncio
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dsp.runtime import DSPRuntime, DSPRuntimeConfig
from dsp.latency import capture_basis, latency_signature


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_src_credit_updates_the_native_total_control(self):
        runtime = DSPRuntime(object())
        runtime._process = SimpleNamespace(returncode=None)
        commands = []
        async def provider():
            await asyncio.sleep(0)
            return 750
        async def control(command, *, reply):
            commands.append(command)
            return 'ok'
        runtime.configure_latency_tracking(input_latency_provider=provider)
        runtime._control_unlocked = control
        await runtime.update_input_latency()
        self.assertEqual(commands, ['input latency 750'])
        self.assertEqual(runtime._last_input_latency_ms, 750)

    async def test_graph_read_cannot_send_credit_to_a_replacement_engine(self):
        runtime = DSPRuntime(object())
        runtime._process = SimpleNamespace(returncode=None)
        commands = []
        async def provider():
            runtime._process = SimpleNamespace(returncode=None)
            return 750
        async def control(command, *, reply):
            commands.append(command)
        runtime.configure_latency_tracking(input_latency_provider=provider)
        runtime._control_unlocked = control
        await runtime.update_input_latency()
        self.assertEqual(commands, [], 'Obsolete graph evidence must not reach a new engine')

    async def test_graph_read_cannot_credit_the_previous_ingress_owner(self):
        runtime = DSPRuntime(object())
        runtime._process = SimpleNamespace(returncode=None)
        commands = []
        async def provider():
            runtime._surround_input = True
            return 750
        async def control(command, *, reply):
            commands.append(command)
        runtime.configure_latency_tracking(input_latency_provider=provider)
        runtime._control_unlocked = control
        await runtime.update_input_latency()
        self.assertEqual(commands, [])

    async def test_control_lock_wait_cannot_credit_a_replacement_engine(self):
        runtime = DSPRuntime(object())
        runtime._process = SimpleNamespace(returncode=None)
        sampled = asyncio.Event()
        commands = []
        async def provider():
            sampled.set()
            return 750
        async def control(command, *, reply):
            commands.append(command)
            return 'ok'
        runtime._control_unlocked = control
        runtime.configure_latency_tracking(input_latency_provider=provider)
        await runtime._control_lock.acquire()
        task = asyncio.create_task(runtime.update_input_latency())
        try:
            await sampled.wait()
            runtime._process = SimpleNamespace(returncode=None)
        finally:
            runtime._control_lock.release()
        await task
        self.assertEqual(commands, [], 'A queued credit belongs to the sampled engine')
        self.assertIsNone(runtime._last_input_latency_ms)

    async def test_changed_ingress_during_ack_cannot_cache_obsolete_credit(self):
        runtime = DSPRuntime(object())
        runtime._process = SimpleNamespace(returncode=None)
        async def control(command, *, reply):
            await asyncio.sleep(0)
            runtime._surround_input = True
            return 'ok'
        runtime._control_unlocked = control
        runtime.configure_latency_tracking(input_latency_provider=lambda: 750)
        await runtime.update_input_latency()
        self.assertIsNone(runtime._last_input_latency_ms)

    async def test_stale_ack_invalidates_populated_cache_and_refreshes_new_ingress(self):
        runtime = DSPRuntime(object())
        runtime._process = SimpleNamespace(returncode=None)
        runtime._last_input_latency_ms = 0
        applied_ms = 0
        commands = []
        async def control(command, *, reply):
            nonlocal applied_ms
            commands.append(command)
            applied_ms = float(command.split()[-1])
            if applied_ms == 750:
                await asyncio.sleep(0)
                runtime._surround_input = True
            return 'ok'
        runtime._control_unlocked = control
        runtime.configure_latency_tracking(input_latency_provider=lambda: 0 if runtime._surround_input else 750)
        await runtime.update_input_latency()
        await runtime.update_input_latency()
        self.assertEqual(commands, ['input latency 750', 'input latency 0'])
        self.assertEqual(applied_ms, 0)
        self.assertEqual(runtime._last_input_latency_ms, 0)

    async def test_readback_wait_cannot_read_a_replacement_engine(self):
        runtime = DSPRuntime(object())
        runtime._process = SimpleNamespace(returncode=None)
        runtime._config = DSPRuntimeConfig('stereo', 'test', 48000, ('FL', 'FR'),
                                           ({'name': 'main_l'}, {'name': 'main_r'}))
        commands = []
        async def control(command, *, reply):
            commands.append(command)
            return '{"ready":false}'
        runtime._control_unlocked = control
        await runtime._control_lock.acquire()
        task = asyncio.create_task(runtime.read_latency())
        try:
            await asyncio.sleep(0)
            runtime._process = SimpleNamespace(returncode=None)
        finally:
            runtime._control_lock.release()
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            await task
        self.assertEqual(commands, [])
        self.assertEqual(runtime.snapshot()['av_sync'], {})

    async def test_readback_reply_cannot_cache_previous_ingress_evidence(self):
        runtime = DSPRuntime(object())
        runtime._process = SimpleNamespace(returncode=None)
        runtime._config = DSPRuntimeConfig('stereo', 'test', 48000, ('FL', 'FR'),
                                           ({'name': 'main_l'}, {'name': 'main_r'}))
        async def control(command, *, reply):
            await asyncio.sleep(0)
            runtime._mapped_ingress = (('capture:FL', 'fxroute_dsp:input_1'),)
            return '{"ready":false}'
        runtime._control = control
        runtime._control_unlocked = control
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            await runtime.read_latency()
        self.assertEqual(runtime.snapshot()['av_sync'], {})

    async def test_settled_latency_rejects_ingress_changed_during_readback(self):
        runtime = DSPRuntime(object())
        async def read():
            await asyncio.sleep(0)
            runtime._surround_input = True
            return {'ready': True, 'bypassed': False}
        runtime.read_latency = read
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            await runtime.settled_latency()

    async def test_settled_latency_rejects_an_engine_stopped_during_graph_read(self):
        runtime = DSPRuntime(object())
        runtime._process = SimpleNamespace(returncode=None)
        async def provider():
            runtime._process = None
            return 750
        async def control(command, *, reply):
            return 'ok'
        async def read():
            return {'ready': True, 'bypassed': False}
        runtime._control = control
        runtime.read_latency = read
        runtime.configure_latency_tracking(input_latency_provider=provider)
        with self.assertRaisesRegex(RuntimeError, 'changed|unavailable'):
            await runtime.settled_latency()

    async def test_raw_child_certifies_only_excited_unmuted_roles_and_sources(self):
        runtime = DSPRuntime(object())
        runtime._latency_tracking = True
        layout = ({'name': 'main_l', 'routes': [{'input': 0, 'gain': 1}]},
                  {'name': 'main_r', 'routes': [{'input': 1, 'gain': 1}]},
                  {'name': 'sub1', 'routes': [{'input': 0, 'gain': 0.5}, {'input': 1, 'gain': 0.5}],
                   'post_crossover_routes': [{'input': 3, 'gain': 1}]},
                  {'name': 'center', 'routes': [{'input': 2, 'gain': 1}]},
                  {'name': 'side_l', 'routes': [{'input': 4, 'gain': 1}]})
        runtime._config = DSPRuntimeConfig('surround', 'dev', 48000, ('FL', 'FR', 'FC', 'LFE', 'SL', 'SR', 'BL', 'BR'), layout)
        snapshot = {'ready': True, 'rate': 48000, 'input': 0, 'bypassed': 1,
                    'outputs': [{'role': row['name'], 'dsp': 0, 'balance': 0} for row in layout]}
        async def settled():
            return dict(snapshot)
        runtime.settled_latency = settled
        for muted, expected in ((0, ['main_l', 'sub1']), (4, ['main_l'])):
            runtime._exact_sub_mute_mask = muted
            runtime.freeze_measurement_latency(snapshot)
            result = await runtime.finish_measurement_latency({'id': 'child', 'channel': 'left', 'measurement_scope': 'raw_helper'})
            self.assertEqual(result['measured_roles'], expected)
            self.assertEqual(result['measured_positions'], ['FL'])

    async def test_measurement_readiness_failure_restores_bypass_and_ingress(self):
        for raw in (False, True):
            with self.subTest(raw=raw):
                runtime = DSPRuntime(object())
                runtime._latency_tracking = True
                runtime._surround_input = True
                runtime._mapped_ingress = (('capture', 'input'),)
                async def control(command, *, reply):
                    if command == 'effects bypass get':
                        return str(int(runtime._effect_bypass))
                    return 'ok'
                async def surround(enabled):
                    runtime._surround_input = enabled
                async def mapped(links):
                    runtime._mapped_ingress = links
                calls = 0
                async def unavailable():
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        return {'ready': True, 'input': 2818}
                    self.assertFalse(runtime._surround_input)
                    self.assertEqual(runtime._mapped_ingress, ())
                    self.assertEqual(runtime._effect_bypass, raw)
                    raise RuntimeError('latency readiness failed')
                runtime._control_unlocked = control
                runtime.set_surround_input = surround
                runtime.set_mapped_ingress = mapped
                runtime.settled_latency = unavailable
                entry = runtime.enter_raw_measurement if raw else runtime.enter_active_measurement
                with self.assertRaisesRegex(RuntimeError, 'readiness failed'):
                    await entry()
                self.assertFalse(runtime._effect_bypass)
                self.assertTrue(runtime._surround_input)
                self.assertEqual(runtime._mapped_ingress, (('capture', 'input'),))
                self.assertFalse(runtime._measurement_scope_lock.locked())
                self.assertFalse(runtime.snapshot()['measurement_active'])

    async def test_post_crossover_and_group_sources_are_active_in_real_surround_layout(self):
        from audio.output_state import default_output_state, set_mode_routing, switch_mode
        from dsp.processing_plan import compile_processing_plan
        from dsp.native_config import layout_from_plan
        state = switch_mode(default_output_state(), 'surround')
        state = set_mode_routing(state, 'surround', 'dev', ['main_l', 'main_r', 'center', 'sub1'])
        plan = compile_processing_plan(state, output_key='dev', channels=4, sample_rate_hz=48000,
                                       preset_loader=lambda name: {'chain': []})
        layout = layout_from_plan(plan, resolve_ir=lambda name: None)
        runtime = DSPRuntime(object())
        runtime._process = SimpleNamespace(returncode=None)
        runtime._config = DSPRuntimeConfig.from_plan(plan, layout=layout, output_key='dev', sample_rate_hz=48000,
                                                    hardware_ports=['a', 'b', 'c', 'd'], plan_fingerprint='surround')
        raw = {'ready': True, 'rate': 48000, 'input': 0, 'target': 0, 'minimum': 0, 'maximum': 24000,
               'common': 0, 'actual': 0,
               'outputs': [{'dsp': 0, 'intentional': 0, 'align': 0, 'balance': 0, 'sync': 0, 'total': 0} for _ in layout],
               'sources': [{'dsp': 0, 'balance': 0} for _ in range(8)]}
        async def control(command, *, reply):
            return json.dumps(raw)
        runtime._control_unlocked = control
        result = await runtime.read_latency()
        sources = {row['position']: row for row in result['sources']}
        self.assertIn('sub1', sources['LFE']['roles'])
        self.assertIn('sub1', sources['FC']['roles'])
        adopted = capture_basis(state, result, origin='adopted')
        self.assertIn('LFE', adopted['modes']['surround']['av_sync']['source_basis'])

    async def test_readback_reports_the_upmixer_share_of_the_dsp_latency(self):
        from audio.output_state import default_output_state, set_mode_routing, switch_mode
        from dsp.processing_plan import compile_processing_plan
        from dsp.native_config import layout_from_plan
        state = switch_mode(default_output_state(), 'surround')
        state = set_mode_routing(state, 'surround', 'dev', ['main_l', 'main_r', 'center', 'sub1'])
        plan = compile_processing_plan(state, output_key='dev', channels=4, sample_rate_hz=48000,
                                       preset_loader=lambda name: {'chain': []})
        layout = layout_from_plan(plan, resolve_ir=lambda name: None)
        runtime = DSPRuntime(object())
        runtime._process = SimpleNamespace(returncode=None)
        runtime._config = DSPRuntimeConfig.from_plan(plan, layout=layout, output_key='dev', sample_rate_hz=48000,
                                                    hardware_ports=['a', 'b', 'c', 'd'], plan_fingerprint='surround')

        def raw(upmixer, *, named=True):
            share = {'adaptive': upmixer} if named else {}
            return {'ready': True, 'rate': 48000, 'input': 2818, 'target': 0, 'minimum': 2818 + upmixer,
                    'maximum': 26818, 'common': 0, 'actual': 2818 + upmixer,
                    'outputs': [{'dsp': upmixer, 'intentional': 0, 'align': 0, 'balance': 0, 'sync': 0,
                                 'total': 2818 + upmixer, **share} for _ in layout],
                    'sources': [{'dsp': upmixer, 'balance': 0, **share} for _ in range(8)]}
        answer = {}
        async def control(command, *, reply):
            return json.dumps(answer['value'])
        runtime._control_unlocked = control
        answer['value'] = raw(8191)
        stereo = await runtime.read_latency()
        answer['value'] = raw(0)
        multichannel = await runtime.read_latency()
        for row in stereo['outputs'] + stereo['sources']:
            self.assertEqual(row['adaptive'], 8191)
            self.assertAlmostEqual(row['adaptive_ms'], 8191 * 1000 / 48000)
        for row in multichannel['outputs'] + multichannel['sources']:
            self.assertEqual((row['adaptive'], row['adaptive_ms'], row['dsp_ms']), (0, 0.0, 0.0))
        self.assertAlmostEqual(stereo['actual_ms'] - multichannel['actual_ms'], 8191 * 1000 / 48000)
        self.assertEqual(latency_signature(stereo), latency_signature(multichannel))
        # An engine reply that does not name the share has none.
        answer['value'] = raw(0, named=False)
        for row in (await runtime.read_latency())['outputs']:
            self.assertEqual((row['adaptive'], row['adaptive_ms']), (0, 0.0))
        answer['value'] = raw(0)
        answer['value']['outputs'][0]['adaptive'] = -1
        with self.assertRaisesRegex(RuntimeError, 'invalid frames'):
            await runtime.read_latency()

    async def test_latency_json_is_accepted_by_the_real_control_client(self):
        runtime = DSPRuntime(object())
        with tempfile.TemporaryDirectory(prefix='av-sync-', dir='/tmp/opencode') as directory:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            server.bind(f'{directory}/server')
            client.bind(f'{directory}/client')
            server.setblocking(False)
            client.setblocking(False)
            runtime._control_socket = client
            runtime._control_path = Path(directory) / 'server'
            async def respond():
                loop = asyncio.get_running_loop()
                command, address = await loop.sock_recvfrom(server, 1024)
                if command == b'latency get':
                    server.sendto(b'{"ready":false}\n', address)
            task = asyncio.create_task(respond())
            try:
                self.assertEqual(json.loads(await runtime._control('latency get', reply=True)), {'ready': False})
                self.assertFalse(runtime._control_poisoned)
            finally:
                await task
                client.close()
                server.close()

    async def test_settled_readback_waits_for_requested_bypass(self):
        runtime = DSPRuntime(object())
        runtime._effect_bypass = True
        calls = []
        async def read():
            bypassed = len(calls) > 0
            calls.append(bypassed)
            runtime._effect_bypass = bypassed
            return {'ready': True, 'bypassed': bypassed}
        runtime.read_latency = read
        result = await runtime.settled_latency()
        self.assertTrue(result['bypassed'])
        self.assertEqual(len(calls), 2)

    def test_target_is_live_but_basis_changes_require_a_new_rendering(self):
        text = 'rate 48000\ninputs 2\noutputs 2\noutput 0 0 7 normal\noutput 1 0 3 normal\navsync 100 0\nlatency_basis 0 0\nsource_basis 0 0\n'
        old_shape, old_values = DSPRuntime._live_config(text)
        new_shape, new_values = DSPRuntime._live_config(text.replace('avsync 100', 'avsync 260'))
        self.assertEqual(old_shape, new_shape)
        self.assertNotEqual(old_values, new_values)
        basis_shape, _ = DSPRuntime._live_config(text.replace('latency_basis 0 0', 'latency_basis 0 128'))
        self.assertNotEqual(old_shape, basis_shape)

    async def test_readback_maps_frames_and_output_names(self):
        runtime = DSPRuntime(object())
        runtime._process = SimpleNamespace(returncode=None)
        runtime._config = DSPRuntimeConfig('stereo', 'test', 48000, ('FL', 'FR'),
                                           ({'name': 'main_l'}, {'name': 'main_r'}))
        raw = {'ready': True, 'rate': 48000, 'reference': 0, 'input': 8192,
               'minimum': 8192, 'maximum': 32192, 'actual': 14400, 'target': 14400,
               'common': 6208, 'bypassed': 0, 'balance_limited': 0, 'limited': 0,
               'outputs': [{'dsp': 0, 'balance': 0, 'align': 0, 'intentional': 0, 'sync': 6208, 'total': 14400}]*2,
               'sources': [{'dsp': 0, 'balance': 0}, {'dsp': 0, 'balance': 0}]}
        async def control(command, *, reply=False):
            self.assertEqual(command, 'latency get')
            return json.dumps(raw)
        runtime._control_unlocked = control
        result = await runtime.read_latency()
        self.assertEqual(result['actual_ms'], 300)
        self.assertAlmostEqual(result['input_ms'], 170.666666667)
        self.assertEqual([row['role'] for row in result['outputs']], ['main_l', 'main_r'])
        self.assertEqual(result['sources'][0]['position'], 'FL')

    async def test_small_latency_change_invalidates_measurement_but_align_is_owned(self):
        runtime = DSPRuntime(object())
        first = {'ready': True, 'rate': 48000, 'input': 0, 'bypassed': 1,
                 'outputs': [{'dsp': 0, 'intentional': 0, 'balance': 0, 'align': 7}]}
        runtime.freeze_measurement_latency(first)
        runtime.validate_measurement_latency({**first, 'outputs': [{**first['outputs'][0], 'align': 19}]})
        with self.assertRaisesRegex(RuntimeError, 'latency changed'):
            runtime.validate_measurement_latency({**first, 'outputs': [{**first['outputs'][0], 'dsp': 1024}]})

    async def test_final_capture_validation_rejects_last_moment_drift(self):
        runtime = DSPRuntime(object())
        runtime._latency_tracking = True
        first = {'ready': True, 'rate': 48000, 'input': 0, 'bypassed': 1,
                 'outputs': [{'role': 'main_l', 'dsp': 0, 'intentional': 0, 'balance': 0}]}
        runtime.freeze_measurement_latency(first)
        async def changed():
            return {**first, 'input': 8192}
        runtime.settled_latency = changed
        with self.assertRaisesRegex(RuntimeError, 'latency changed'):
            await runtime.finish_measurement_latency({'id': 'capture-1'})
        self.assertFalse(runtime.snapshot()['measurement_latency'].get('verified', False))

    async def test_final_capture_keeps_sweep_evidence_after_normal_restore(self):
        runtime = DSPRuntime(object())
        runtime._latency_tracking = True
        first = {'ready': True, 'rate': 48000, 'input': 0, 'bypassed': 1,
                 'outputs': [{'role': 'main_l', 'dsp': 0, 'intentional': 0, 'balance': 6144}],
                 'context': {'timing_key': 'sweep-rendering'}}
        runtime.freeze_measurement_latency(first)
        async def unchanged():
            return first
        runtime.settled_latency = unchanged
        await runtime.finish_measurement_latency({'id': 'capture-1', 'measurement_target': {'measured_roles': ['main_l']}})
        runtime._latency_snapshot = {**first, 'bypassed': 0, 'outputs': [{'dsp': 6144, 'balance': 0}]}
        evidence = runtime.snapshot()['measurement_latency']
        self.assertTrue(evidence['verified'])
        self.assertEqual(evidence['outputs'][0]['balance'], 6144)
        self.assertEqual(evidence['measured_roles'], ['main_l'])

    async def test_secondary_mixed_source_drift_also_invalidates_measurement(self):
        runtime = DSPRuntime(object())
        first = {'ready': True, 'rate': 48000, 'input': 0, 'bypassed': 0,
                 'outputs': [{'dsp': 6144, 'balance': 0, 'intentional': 0}],
                 'sources': [{'dsp': 6144, 'balance': 0, 'unknown': 0},
                             {'dsp': 0, 'balance': 0, 'unknown': 0}]}
        runtime.freeze_measurement_latency(first)
        changed = {**first, 'sources': [first['sources'][0], {'dsp': 1024, 'balance': 0, 'unknown': 0}]}
        with self.assertRaisesRegex(RuntimeError, 'latency changed'):
            runtime.validate_measurement_latency(changed)


if __name__ == '__main__':
    unittest.main()
