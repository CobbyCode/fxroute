#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""AV Sync state, measured basis, IR metadata and compiler contracts."""
import copy
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.output_state import default_output_state, set_av_sync, validate_output_state, switch_mode
from audio.output_state_migration import upgrade_output_state
from dsp.latency import capture_basis, ir_latency, latency_signature
from dsp.manager import DSPManager, build_wav_bytes, parse_wav_frames
from dsp.native_config import layout_from_plan


class AVSyncTests(unittest.TestCase):
    def test_targets_are_per_mode_and_alignment_is_unchanged(self):
        state = default_output_state()
        state['modes']['stereo']['processing']['main_l']['alignment_ms'] = 7
        result = set_av_sync(state, 'stereo', target_ms=260)
        result = set_av_sync(result, 'surround', target_ms=350)
        self.assertEqual(result['modes']['stereo']['av_sync']['target_ms'], 260)
        self.assertEqual(result['modes']['surround']['av_sync']['target_ms'], 350)
        self.assertEqual(result['modes']['stereo-sub']['av_sync']['target_ms'], 0)
        self.assertEqual(result['modes']['stereo']['processing'], state['modes']['stereo']['processing'])

    def test_nonfinite_negative_and_boolean_targets_are_rejected(self):
        for value in (float('nan'), float('inf'), -1, True, '100', 20001):
            with self.subTest(value=value), self.assertRaises(ValueError):
                set_av_sync(default_output_state(), 'stereo', target_ms=value)

    def test_v5_upgrade_keeps_legacy_alignment_without_inventing_a_basis(self):
        state = default_output_state()
        state['version'] = 5
        for config in state['modes'].values():
            config.pop('av_sync', None)
        state['modes']['stereo']['processing']['main_r']['alignment_ms'] = -8
        result = upgrade_output_state(state)
        self.assertEqual(result['modes']['stereo']['av_sync']['basis'], {})
        self.assertEqual(result['modes']['stereo']['processing'], state['modes']['stereo']['processing'])
        self.assertNotIn('av_sync', state['modes']['stereo'])

    def test_calibration_basis_includes_existing_balance_but_not_align_or_common(self):
        state = default_output_state()
        state['modes']['stereo']['av_sync']['basis']['main_r'] = 42
        snapshot = {'ready': True, 'rate': 48000, 'input': 0, 'balance_limited': 0,
                    'outputs': [{'role': 'main_l', 'dsp': 0, 'balance': 6144,
                                 'align': 336, 'intentional': 480, 'sync': 10000, 'unknown': 0}],
                    'sources': [{'position': 'FL', 'dsp': 0, 'balance': 6144, 'unknown': 0}]}
        result = capture_basis(state, snapshot, origin='calibrated')
        sync = result['modes']['stereo']['av_sync']
        self.assertEqual(sync['basis'], {'main_l': 128.0, 'main_r': 42})
        self.assertEqual(sync['source_basis'], {'FL': 128.0})
        self.assertEqual(state['modes']['stereo']['av_sync']['basis'], {'main_r': 42})
        self.assertEqual(validate_output_state(result), result)

    def test_compiler_emits_named_bases_and_hq_8192(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = DSPManager(Path(directory)/'presets', Path(directory)/'irs')
            layout = [{'name': 'main_l', 'routes': [{'input': 0, 'gain': 1}],
                       'delay_ms': 7, 'av_sync': {'target_ms': 260, 'basis': {'main_l': 128},
                                                'source_basis': {'FL': 128}}},
                      {'name': 'main_r', 'routes': [{'input': 1, 'gain': 1}]}]
            text = manager.compile_engine_text(layout, preset_name='Direct')
            self.assertIn('avsync 260 0\n', text)
            self.assertIn('latency_basis 0 128\n', text)
            self.assertIn('source_basis 0 128\n', text)
            self.assertIn('output 0 0 7 normal\n', text)
            extras = manager.normalize_effects_extras({'upmixer': {'params': {'mode': 'hq', 'fftSize': 8192}}})
            self.assertEqual(extras['upmixer']['params']['fftSize'], 8192)

    def test_ir_metadata_survives_wav_and_separates_intentional_alignment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'ir.wav'
            info = {'version': 1, 'algorithmic_frames': [2048, 2048], 'intentional_frames': [48, 0]}
            path.write_bytes(build_wav_bytes(2, 48000, 32, 3, struct.pack('<4f', 0, 0, 1, 1), latency=info))
            parsed = parse_wav_frames(path)
            self.assertEqual(parsed['latency'], info)
            self.assertEqual(ir_latency(parsed), {'algorithmic_ms': [2048/48, 2048/48],
                                                  'intentional_ms': [1.0, 0.0], 'known': True})
            untagged = copy.deepcopy(parsed); untagged.pop('latency')
            self.assertEqual(ir_latency(untagged)['known'], False)

    def test_partial_recapture_keeps_output_and_source_epochs_coherent(self):
        state = switch_mode(default_output_state(), 'surround')
        config = state['modes']['surround']['av_sync']
        config['basis'] = {'FL': 0, 'FR': 0, 'FC': 0, 'SL': 42}
        config['source_basis'] = {'FL': 0, 'FR': 0, 'FC': 0, 'SL': 42}
        snapshot = {'ready': True, 'rate': 48000, 'input': 0, 'balance_limited': 0,
                    'outputs': [{'role': 'FL', 'dsp': 6144, 'balance': 0},
                                {'role': 'FR', 'dsp': 6144, 'balance': 0},
                                {'role': 'FC', 'dsp': 0, 'balance': 6144}],
                    'sources': [{'position': 'FL', 'dsp': 6144, 'balance': 0, 'roles': ['FL']},
                                {'position': 'FR', 'dsp': 6144, 'balance': 0, 'roles': ['FR']},
                                {'position': 'FC', 'dsp': 0, 'balance': 6144, 'roles': ['FC']}]}
        for _ in range(3):
            state = capture_basis(state, snapshot, origin='calibrated', roles=['FC'])
            config = state['modes']['surround']['av_sync']
            for name in ('basis', 'source_basis'):
                self.assertEqual(config[name], {'FL': 128, 'FR': 128, 'FC': 128, 'SL': 170})
                delta = [128 - config[name]['FL'], 128 - config[name]['FR'], 0 - config[name]['FC']]
                self.assertEqual([max(delta) - value for value in delta], [0, 0, 128])

    def test_negative_epoch_preserves_dormant_basis_without_negative_milliseconds(self):
        state = default_output_state()
        state['modes']['stereo']['av_sync']['basis'] = {'main_l': 128, 'main_r': 128, 'left_low': 0}
        snapshot = {'ready': True, 'rate': 48000, 'input': 0, 'outputs': [
            {'role': 'main_l', 'dsp': 0, 'balance': 0}, {'role': 'main_r', 'dsp': 0, 'balance': 0}]}
        state = capture_basis(state, snapshot, origin='calibrated', roles=['main_l'])
        self.assertEqual(state['modes']['stereo']['av_sync']['basis'], {'main_l': 128, 'main_r': 128, 'left_low': 0})
        self.assertEqual(state['modes']['stereo']['av_sync']['basis_context']['normalization_ms']['outputs'], 128)

    def test_raw_sub_capture_does_not_certify_unexcited_lfe_source(self):
        state = switch_mode(default_output_state(), 'surround')
        snapshot = {'ready': True, 'rate': 48000, 'input': 0, 'measured_positions': ['FL'],
                    'outputs': [{'role': 'sub1', 'dsp': 0, 'balance': 6144}],
                    'sources': [{'position': 'FL', 'dsp': 0, 'balance': 6144, 'roles': ['sub1']},
                                {'position': 'LFE', 'dsp': 0, 'balance': 6144, 'roles': ['sub1']}]}
        result = capture_basis(state, snapshot, origin='calibrated', roles=['sub1'])
        self.assertEqual(result['modes']['surround']['av_sync']['source_basis'], {'FL': 128})

    def test_upmixer_path_change_is_not_a_change_of_the_rendering(self):
        def snapshot(upmixer, preset=4096):
            return {'ready': True, 'rate': 48000, 'input': 2818, 'bypassed': 0,
                    'outputs': [{'dsp': preset + upmixer, 'adaptive': upmixer, 'intentional': 0,
                                 'balance': 0, 'unknown': 0, 'mixed': 0}],
                    'sources': [{'dsp': preset + upmixer, 'adaptive': upmixer, 'intentional': 0,
                                 'balance': 0, 'unknown': 0}]}
        # Stereo programme (HQ 8192, Fast) against multichannel content on the direct path.
        self.assertEqual(latency_signature(snapshot(8191)), latency_signature(snapshot(0)))
        self.assertEqual(latency_signature(snapshot(64)), latency_signature(snapshot(0)))
        # Everything the rendering fixes still counts, on either path.
        self.assertNotEqual(latency_signature(snapshot(0, preset=6144)), latency_signature(snapshot(0)))
        self.assertNotEqual(latency_signature(snapshot(8191, preset=6144)), latency_signature(snapshot(0)))
        for section in ('outputs', 'sources'):
            changed = snapshot(8191)
            changed[section][0]['dsp'] += 1024
            self.assertNotEqual(latency_signature(changed), latency_signature(snapshot(8191)), section)
        # A readback without the share compares as it always did.
        plain = snapshot(0)
        for section in ('outputs', 'sources'):
            del plain[section][0]['adaptive']
        self.assertEqual(latency_signature(plain), latency_signature(snapshot(0)))

    def test_odd_pcm24_data_is_padded_before_latency_metadata(self):
        with tempfile.TemporaryDirectory(dir='/tmp/opencode') as directory:
            path = Path(directory) / 'odd.wav'
            metadata = {'version': 1, 'algorithmic_frames': [0], 'intentional_frames': [0]}
            path.write_bytes(build_wav_bytes(1, 48000, 24, 1, b'\0\0\0', latency=metadata))
            self.assertEqual(parse_wav_frames(path).get('latency'), metadata)


if __name__ == '__main__':
    unittest.main()
