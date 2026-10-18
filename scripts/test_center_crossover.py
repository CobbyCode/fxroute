#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Center ways reuse mono FC routing, output processing and whole-speaker measurements."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.output_api import crossover_response, output_state_response
from audio.output_state import (default_output_state, routing_for_device, set_bass_management,
                                set_bank_preset, set_crossover, set_mode_routing, set_output_processing,
                                switch_mode)
from audio.output_state_store import OutputStateStore
from audio.output_topology import derive_topology
from dsp.manager import DSPManager
from dsp.native_config import layout_from_plan
from dsp.output_meter import meter_positions
from dsp.processing_plan import compile_processing_plan
from measurement.level_match import plan_scope
from measurement.target import freeze_measurement_target, target_input_mask, target_output_mask

FRONTS = ['left_low', 'left_high', 'right_low', 'right_high']
OVERVIEW = {'selected_output': {'key': 'device', 'channels': 16}}
CENTER_WAY_SETS = (('low', 'high'), ('low', 'mid', 'high'), ('low', 'low_mid', 'mid', 'high'))


def shape(frequency, family='linkwitz-riley', slope=24):
    return {'family': family, 'slope_db_oct': slope, 'frequency_hz': frequency}


def center_state(ways=('low', 'high')):
    state = switch_mode(set_crossover(default_output_state(), 'surround', True), 'surround')
    return set_mode_routing(state, 'surround', 'device',
                            [*FRONTS, *(f'center_{way}' for way in ways), 'sub1'])


def compile_plan(state):
    return compile_processing_plan(state, output_key='device', channels=16, sample_rate_hz=48000,
                                   preset_loader=lambda *_: {'chain': []})


class CenterCrossoverTests(unittest.TestCase):
    def test_centers_have_independent_mono_banks_for_every_front_way_set(self):
        for ways in CENTER_WAY_SETS:
            with self.subTest(ways=ways):
                state = center_state(ways)
                catalog = output_state_response(state, OVERVIEW)
                config = catalog['modes']['surround']
                self.assertEqual(config['topology']['issues'], [])
                self.assertEqual(config['topology']['way_count'], 2)
                self.assertEqual(config['topology']['center_way_count'], len(ways))
                self.assertEqual(config['topology']['center_ways'], [f'center_{way}' for way in ways])
                for way in ways:
                    self.assertEqual(config['banks'][f'center_{way}']['channel_mode'], 'mono')
                    self.assertEqual(config['banks'][f'center_{way}']['label'],
                                     {'low': 'Low C', 'low_mid': 'Low-Mid C', 'mid': 'Mid C', 'high': 'High C'}[way])
                self.assertNotIn('center_low', catalog['capabilities']['roles']['stereo'])

    def test_incomplete_or_mixed_center_never_activates(self):
        for center in (['center_low'], ['center_mid', 'center_high'],
                       ['center_low', 'center_low_mid', 'center_high'],
                       ['center', 'center_low', 'center_high']):
            with self.subTest(center=center):
                topology = derive_topology('surround', [*FRONTS, *center], crossover_enabled=True)
                self.assertTrue(topology.issues)
                with self.assertRaises(ValueError):
                    topology.require_activatable()
        for mode, enabled in (('stereo', True), ('stereo-sub', True), ('surround', False)):
            with self.subTest(mode=mode, enabled=enabled), self.assertRaises(ValueError):
                derive_topology(mode, ['center_low', 'center_high'], crossover_enabled=enabled)

    def test_existing_full_range_center_and_fronts_are_unchanged(self):
        state = switch_mode(set_mode_routing(default_output_state(), 'surround', 'device',
                                             ['main_l', 'main_r', 'center']), 'surround')
        state = set_crossover(state, 'surround', True)
        self.assertEqual(routing_for_device(state, 'surround', 'device'), ['left_low', 'right_low', 'center'])
        self.assertEqual(output_state_response(state, OVERVIEW)['modes']['surround']['banks']['center']['label'],
                         'Center')
        state = set_mode_routing(state, 'surround', 'device', [*FRONTS, 'center', 'sub1'])
        plain = compile_plan(state)
        multi = compile_plan(center_state())
        self.assertEqual(multi['outputs'][:4], plain['outputs'][:4])
        self.assertEqual(multi['outputs'][-1], plain['outputs'][-1])

    def test_toggle_restores_center_ports_and_retains_processing(self):
        for ways in CENTER_WAY_SETS:
            with self.subTest(ways=ways):
                state = set_output_processing(center_state(ways), 'surround', 'center_low',
                                              lowpass=shape(300), level_db=-3)
                disabled = set_crossover(state, 'surround', False)
                self.assertEqual(routing_for_device(disabled, 'surround', 'device'),
                                 ['main_l', 'off', 'main_r', 'off', 'center',
                                  *(['off'] * (len(ways) - 1)), 'sub1'])
                restored = set_crossover(disabled, 'surround', True)
                self.assertEqual(routing_for_device(restored, 'surround', 'device'),
                                 [*FRONTS, *(f'center_{way}' for way in ways), 'sub1'])
                for key in ('banks', 'processing'):
                    for role, settings in state['modes']['surround'][key].items():
                        self.assertEqual(restored['modes']['surround'][key][role], settings)

    def test_four_way_filters_banks_and_trims_survive_storage_and_compile_like_fronts(self):
        state = center_state(CENTER_WAY_SETS[-1])
        definitions = {
            'low': (None, shape(200, 'butterworth', 18)),
            'low_mid': (shape(200, 'bessel', 12), shape(800, 'butterworth', 36)),
            'mid': (shape(800), shape(3000, 'bessel', 24)),
            'high': (shape(3000, 'linkwitz-riley', 48), None),
        }
        for index, (way, (highpass, lowpass)) in enumerate(definitions.items()):
            state = set_output_processing(state, 'surround', f'center_{way}', highpass=highpass,
                                          lowpass=lowpass, level_db=-index, alignment_ms=index / 2,
                                          polarity='invert' if index % 2 else 'normal')
        state = set_bank_preset(state, 'surround', 'center_low_mid', preset='Center band EQ')
        with tempfile.TemporaryDirectory() as directory:
            store = OutputStateStore(Path(directory) / 'output-state.json')
            saved = store.commit(state, expected_revision=0)
            loaded = store.load(required=True)
        self.assertEqual(loaded, saved)
        self.assertEqual(loaded['modes']['surround']['banks']['center_low_mid']['preset'], 'Center band EQ')
        response = crossover_response(loaded, OVERVIEW, {'active_rate': 48000})
        plan = compile_plan(loaded)
        layout = layout_from_plan(plan, resolve_ir=lambda *_: self.fail('Unexpected IR'))
        for index, (way, (highpass, lowpass)) in enumerate(definitions.items()):
            role = f'center_{way}'
            row = plan['outputs'][4 + index]
            self.assertEqual(row['role'], role)
            self.assertEqual(row['routes'], [{'input': 2, 'gain': 1.0}])
            self.assertEqual((row['gain_db'], row['delay_ms'], row['invert']),
                             (-index, index / 2, bool(index % 2)))
            self.assertTrue(response['ways'][role]['complete'])
            self.assertEqual(response['ways'][role]['filters'], {'highpass': highpass, 'lowpass': lowpass})
            self.assertEqual(len(response['ways'][role]['points']), 180)
            front_state = set_output_processing(loaded, 'surround', 'left_low',
                                                highpass=highpass, lowpass=lowpass)
            front_layout = layout_from_plan(compile_plan(front_state), resolve_ir=lambda *_: {})
            self.assertEqual(layout[4 + index]['sos'], front_layout[0]['sos'])
        area = freeze_measurement_target(loaded, bank_id='center_low_mid', output_key='device', channels=16,
                                        sample_rate_hz=48000, fingerprint='center-band')
        self.assertEqual(area['measured_roles'], ['center_low_mid'])

    def test_toggle_preserves_incomplete_low_only_center_draft(self):
        state = center_state(('low',))
        disabled = set_crossover(state, 'surround', False)
        self.assertEqual(routing_for_device(disabled, 'surround', 'device')[4], 'off')
        restored = set_crossover(disabled, 'surround', True)
        self.assertEqual(routing_for_device(restored, 'surround', 'device')[4], 'center_low')
        self.assertTrue(output_state_response(restored, OVERVIEW)['modes']['surround']['topology']['issues'])

    def test_fc_filters_trims_and_physical_fanout_compile_without_front_fold_down(self):
        state = center_state(('low', 'mid', 'high'))
        state = set_mode_routing(state, 'surround', 'device',
                                [*FRONTS, 'center_low', 'center_mid', 'center_high', 'sub1', 'center_high'])
        state = set_output_processing(state, 'surround', 'center_mid',
                                      highpass=shape(300, 'butterworth', 18), lowpass=shape(2500, 'bessel', 12),
                                      level_db=-4, alignment_ms=2.5, polarity='invert')
        plan = compile_plan(state)
        center = next(row for row in plan['outputs'] if row['role'] == 'center_mid')
        self.assertEqual(center['routes'], [{'input': 2, 'gain': 1.0}])
        self.assertEqual((center['gain_db'], center['delay_ms'], center['invert']), (-4, 2.5, True))
        self.assertEqual([f['frequency_hz'] for f in center['crossover']], [300, 2500, 80])
        self.assertFalse(any(send['input'] == 2 for row in plan['outputs'][:4] for send in row['routes']))
        self.assertEqual([edge for edge in plan['physical_routes'] if edge['output'] == 6],
                         [{'output': 6, 'channel': 6}, {'output': 6, 'channel': 8}])
        layout = layout_from_plan(plan, resolve_ir=lambda *_: self.fail('Unexpected IR'))
        # BW18: two sections, BS12: one, LR24 bass high-pass: two.
        self.assertEqual(len(layout[5]['sos']), 5)
        self.assertEqual(layout[5]['routes'], [{'input': 2, 'gain': 1.0}])

    def test_center_response_uses_its_group_bass_filter(self):
        state = set_output_processing(center_state(), 'surround', 'center_low', lowpass=shape(2000))
        state = set_bass_management(state, 'surround', sub_link=False, sub_filters={
            'center': {**shape(120, 'butterworth', 12), 'highpass_enabled': True}})
        response = crossover_response(state, OVERVIEW, {'active_rate': 48000})
        low = response['ways']['center_low']
        self.assertTrue(low['complete'])
        self.assertEqual(low['derived_highpass'], shape(120, 'butterworth', 12))
        self.assertLess(low['points'][0][1], -25)
        state = set_bass_management(state, 'surround', sub_filters={
            'center': {**shape(120), 'highpass_enabled': False}})
        self.assertIsNone(crossover_response(state, OVERVIEW, {})['ways']['center_low']['derived_highpass'])

    def test_global_center_and_level_matching_cover_all_ways_as_one_speaker(self):
        for ways in CENTER_WAY_SETS:
            with self.subTest(ways=ways):
                state = center_state(ways)
                roles = [row['role'] for row in compile_plan(state)['outputs']]
                target = freeze_measurement_target(state, bank_id='global', output_key='device', channels=16,
                                                   sample_rate_hz=48000, fingerprint='center-plan', group='center')
                self.assertEqual(target['measured_roles'], [*(f'center_{way}' for way in ways), 'sub1'])
                self.assertEqual(target_output_mask(target, roles=roles), 0b1111)
                self.assertEqual(target_input_mask(target), 0b11)
                scope = plan_scope('surround', 'global', roles)
                center_units = [unit for unit in scope['units'] if unit['roles'][0].startswith('center_')]
                self.assertEqual(len(center_units), 1)
                self.assertEqual(center_units[0]['roles'], [f'center_{way}' for way in ways])
                positions = meter_positions('surround', tuple(roles))
                self.assertEqual([position.key for position in positions], ['fl', 'fr', 'c', 'lfe'])
                self.assertEqual(positions[2].outputs, tuple(range(4, 4 + len(ways))))

    def test_upmixer_still_detects_multiway_center_destination(self):
        layout = layout_from_plan(compile_plan(center_state()), resolve_ir=lambda *_: {})
        with tempfile.TemporaryDirectory() as directory:
            manager = DSPManager(home=Path(directory))
            text = manager.compile_engine_text(layout, preset_name='Neutral', extras_override={
                'upmixer': {'enabled': True, 'params': {'mode': 'fast', 'center': 6, 'surround': 6}}})
        self.assertIn('param center_out 1', text)


if __name__ == '__main__':
    unittest.main()
