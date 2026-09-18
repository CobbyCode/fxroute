#!/usr/bin/env python3
"""Independent mode/crossover and routing-to-DSP contracts."""
import copy
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audio import output_state as model
from audio.output_state_store import OutputStateStore
from audio.output_topology import derive_topology, roles_for_mode
from dsp.processing_plan import compile_processing_plan


class UnifiedOutputTests(unittest.TestCase):
    def test_modes_have_independent_crossover_role_domains(self):
        state = model.default_output_state()
        self.assertEqual(set(state['modes']), {'stereo', 'stereo-sub'})
        self.assertEqual(roles_for_mode('stereo'), ('main_l', 'main_r'))
        self.assertEqual(set(roles_for_mode('stereo-sub')),
                         {'main_l', 'main_r', 'sub1', 'sub2', 'sub_l', 'sub_r'})
        roles = roles_for_mode('stereo-sub', crossover_enabled=True)
        self.assertNotIn('main_l', roles)
        self.assertIn('left_low_mid', roles)
        self.assertIn('right_high', roles)
        self.assertIn('sub1', roles)
        with self.assertRaises(ValueError):
            model.switch_mode(state, 'crossover')

    def test_sub_structure_and_compiled_input_routes_come_only_from_routing(self):
        for subs, kind, routes in [
            (['sub1'], 'mono', [{'input': 0, 'gain': .5}, {'input': 1, 'gain': .5}]),
            (['sub1', 'sub2'], 'dual-mono', [{'input': 0, 'gain': .5}, {'input': 1, 'gain': .5}]),
            (['sub_l', 'sub_r'], 'stereo', [{'input': 0, 'gain': 1.0}]),
        ]:
            with self.subTest(subs=subs):
                state = model.switch_mode(model.default_output_state(), 'stereo-sub')
                state = model.set_mode_routing(state, 'stereo-sub', 'A', ['main_l', 'main_r', *subs])
                plan = compile_processing_plan(state, output_key='A', channels=4,
                    sample_rate_hz=48000, preset_loader=lambda _: {'chain': []})
                self.assertEqual(plan['mode'], 'stereo-sub')
                self.assertEqual(plan['sub_mode'], kind)
                self.assertEqual(plan['outputs'][2]['routes'], routes)
                stereo = model.switch_mode(state, 'stereo')
                plan = compile_processing_plan(stereo, output_key='A', channels=4,
                    sample_rate_hz=48000, preset_loader=lambda _: {'chain': []})
                self.assertEqual([row['role'] for row in plan['outputs']], ['main_l', 'main_r'])

    def test_crossover_toggle_uses_one_routing_and_preserves_subs_and_banks(self):
        state = model.switch_mode(model.default_output_state(), 'stereo-sub')
        state = model.set_mode_routing(state, 'stereo-sub', 'A', ['main_l', 'main_r', 'sub1', 'off'])
        state = model.set_crossover(state, 'stereo-sub', True)
        config = state['modes']['stereo-sub']
        self.assertTrue(config['crossover_enabled'])
        self.assertEqual(config['routing']['A'], ['left_low', 'right_low', 'sub1', 'off'])
        state = model.set_mode_routing(state, 'stereo-sub', 'A',
            ['left_low', 'right_low', 'sub1', 'left_high', 'right_high'])
        state['modes']['stereo-sub']['processing']['left_high']['level_db'] = -8
        topology = derive_topology('stereo-sub', state['modes']['stereo-sub']['routing']['A'],
                                   crossover_enabled=True)
        self.assertEqual((topology.way_count, topology.sub_mode), (2, 'mono'))
        state = model.set_crossover(state, 'stereo-sub', False)
        self.assertEqual(state['modes']['stereo-sub']['routing']['A'],
                         ['main_l', 'main_r', 'sub1', 'off', 'off'])
        self.assertEqual(state['modes']['stereo-sub']['processing']['left_high']['level_db'], -8)
        with self.assertRaises(ValueError):
            model.set_mode_routing(state, 'stereo-sub', 'A', ['left_low'])

    def test_version_one_upgrade_preserves_active_processing_and_revision(self):
        bank = {'preset': 'Neutral', 'preset_a': 'Neutral', 'preset_b': None}
        def config(roles):
            return {'routing': {'A': roles}, 'selected_bank': 'global',
                    'banks': {role: copy.deepcopy(bank) for role in ['global', *roles]},
                    'processing': {role: model.default_processing() for role in roles},
                    'bass_management': {'frequency_hz': 80, 'main_highpass_enabled': True}, 'extras': {}}
        old = {'schema': 'fxroute.output-state', 'version': 1, 'revision': 12,
               'active_mode': 'crossover', 'legacy': {}, 'modes': {
                   'stereo': config(['main_l', 'main_r']),
                   'crossover': config(['left_low', 'left_high', 'right_low', 'right_high', 'sub1'])}}
        old['modes']['crossover']['processing']['sub1']['alignment_ms'] = 3
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            path.write_text(json.dumps(old))
            store = OutputStateStore(path)
            upgraded = store.load()
            self.assertEqual(upgraded['version'], 2)
            self.assertEqual(upgraded['revision'], 12)
            self.assertEqual(upgraded['active_mode'], 'stereo-sub')
            self.assertNotIn('crossover', upgraded['modes'])
            self.assertTrue(upgraded['modes']['stereo-sub']['crossover_enabled'])
            self.assertEqual(upgraded['modes']['stereo-sub']['processing']['sub1']['alignment_ms'], 3)
            self.assertEqual(store.commit(upgraded, expected_revision=12)['revision'], 13)
            self.assertEqual(store.load()['version'], 2)

    def test_crossover_compiles_and_measurement_targets_follow_active_roles(self):
        from measurement.target import freeze_measurement_target
        from measurement.autosub.roles import autosub_topology_from_state, main_roles_for_side
        state = model.set_crossover(model.default_output_state(), 'stereo', True)
        state = model.set_mode_routing(state, 'stereo', 'A',
            ['right_high', 'left_low', 'left_high', 'right_low'])
        for role in ['left_low', 'right_low', 'left_high', 'right_high']:
            state = model.set_output_processing(state, 'stereo', role,
                **{'lowpass' if role.endswith('low') else 'highpass':
                   {'family': 'linkwitz-riley', 'slope_db_oct': 24, 'frequency_hz': 1800}})
        plan = compile_processing_plan(state, output_key='A', channels=4,
            sample_rate_hz=48000, preset_loader=lambda _: {'chain': []})
        self.assertEqual(plan['mode'], 'stereo')
        self.assertTrue(plan['crossover_enabled'])
        self.assertEqual(plan['way_count'], 2)
        self.assertEqual(plan['physical_routes'][0], {'output': 3, 'channel': 0})
        target = freeze_measurement_target(state, bank_id='left_high', output_key='A',
            channels=4, sample_rate_hz=48000, fingerprint='test')
        self.assertEqual(target['mode'], 'stereo')
        topology = autosub_topology_from_state(state, output_key='A', channels=4)
        self.assertEqual(main_roles_for_side(topology, 'left'), ('left_low', 'left_high'))

    def test_api_catalog_and_atomic_sub_edit_use_one_state(self):
        import main
        from audio.output_service import OutputService, OutputServiceDeps
        with tempfile.TemporaryDirectory() as directory:
            service = OutputService(OutputServiceDeps(OutputStateStore(Path(directory) / 'state.json'),
                lambda _: {'chain': []}, lambda _: {}, lambda: False))
            initial = model.switch_mode(service.load(), 'stereo-sub')
            initial = model.set_mode_routing(initial, 'stereo-sub', 'A', ['main_l', 'main_r', 'sub_l', 'sub_r'])
            service.commit(initial, expected_revision=0)
            with patch.object(main, 'get_output_service', return_value=service), patch.object(
                    main, 'get_audio_output_overview', return_value={
                        'selected_output': {'key': 'A', 'channels': 4}}):
                catalog = asyncio.run(main.get_audio_output_state())
            self.assertEqual(catalog['capabilities']['modes'], ['stereo', 'stereo-sub'])
            self.assertFalse(catalog['modes']['stereo-sub']['crossover_enabled'])
            self.assertEqual(catalog['modes']['stereo-sub']['topology']['sub_mode'], 'stereo')
            mutation = main._build_output_state_mutation({'kind': 'set_subwoofers', 'mode': 'stereo-sub',
                'frequency_hz': 95, 'main_highpass_enabled': False, 'processing': {
                    'sub_l': {'level_db': -4, 'alignment_ms': 3, 'polarity': 'normal'},
                    'sub_r': {'level_db': -7, 'alignment_ms': 2, 'polarity': 'invert'}}}, output_key='A', channels=4)
            updated = service.apply(mutation, expected_revision=1)
            self.assertEqual(updated['revision'], 2)
            self.assertEqual(updated['modes']['stereo-sub']['bass_management']['frequency_hz'], 95)
            self.assertEqual(updated['modes']['stereo-sub']['processing']['sub_r']['level_db'], -7)
            self.assertEqual(updated['modes']['stereo-sub']['routing'], initial['modes']['stereo-sub']['routing'])


if __name__ == '__main__':
    unittest.main()
