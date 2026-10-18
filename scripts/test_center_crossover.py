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
                                set_crossover, set_mode_routing, set_output_processing, switch_mode)
from audio.output_topology import derive_topology
from dsp.manager import DSPManager
from dsp.native_config import layout_from_plan
from dsp.output_meter import meter_positions
from dsp.processing_plan import compile_processing_plan
from measurement.level_match import plan_scope
from measurement.target import freeze_measurement_target, target_input_mask, target_output_mask

FRONTS = ['left_low', 'left_high', 'right_low', 'right_high']
OVERVIEW = {'selected_output': {'key': 'device', 'channels': 16}}


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
    def test_two_and_three_way_centers_have_independent_mono_banks(self):
        for ways in (('low', 'high'), ('low', 'mid', 'high')):
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
                                     {'low': 'Low C', 'mid': 'Mid C', 'high': 'High C'}[way])
                self.assertNotIn('center_low', catalog['capabilities']['roles']['stereo'])

    def test_incomplete_or_mixed_center_never_activates(self):
        for center in (['center_low'], ['center_mid', 'center_high'],
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
        state = set_output_processing(center_state(('low', 'mid', 'high')), 'surround', 'center_mid',
                                      highpass=shape(300), lowpass=shape(2500), level_db=-3)
        disabled = set_crossover(state, 'surround', False)
        self.assertEqual(routing_for_device(disabled, 'surround', 'device'),
                         ['main_l', 'off', 'main_r', 'off', 'center', 'off', 'off', 'sub1'])
        restored = set_crossover(disabled, 'surround', True)
        self.assertEqual(routing_for_device(restored, 'surround', 'device'),
                         [*FRONTS, 'center_low', 'center_mid', 'center_high', 'sub1'])
        self.assertEqual(restored['modes']['surround']['processing']['center_mid'],
                         state['modes']['surround']['processing']['center_mid'])

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
        for ways in (('low', 'high'), ('low', 'mid', 'high')):
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
