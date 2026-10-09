#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Legacy timing is adopted only from a verified normal activation."""
import copy
import importlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from test_output_state_api import make_manager, make_service

try:
    adoption = importlib.import_module('dsp.av_sync_service')
except ModuleNotFoundError as exc:
    if exc.name != 'dsp.av_sync_service': raise
    adoption = None


class AdoptionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir='/tmp/opencode')
        self.addCleanup(directory.cleanup)
        self.manager = make_manager(directory.name)
        self.service = make_service(directory.name, self.manager)
        self.state = self.service.load()
        self.state['modes']['stereo']['processing']['main_l']['alignment_ms'] = 7
        self.state = self.service.commit(self.state, expected_revision=0)
        plan = self.service.compile_plan(self.state, output_key='A', channels=2, sample_rate_hz=48000)
        self.target = self.build_target(plan, fingerprint=self.service.fingerprint_plan(plan))
        self.latency = {'ready': True, 'rate': 48000, 'input': 0, 'bypassed': 0, 'balance_limited': 0,
                        'context': {'output_key': 'A', 'plan_fingerprint': self.target.config.plan_fingerprint},
                        'outputs': [{'role': 'main_l', 'dsp': 6144, 'balance': 0, 'intentional': 0, 'unknown': 0},
                                    {'role': 'main_r', 'dsp': 6144, 'balance': 0, 'intentional': 0, 'unknown': 0}],
                        'sources': []}
        self.links_valid = True
        self.measurement_active = False
        self.stages = 0

    def build_target(self, plan, *, fingerprint):
        return main._build_plan_target(self.service, self.manager, plan, output_key='A', rate=48000,
                                       hardware_ports=['playback_FL', 'playback_FR'], fingerprint=fingerprint)

    def snapshot(self):
        return {'active': True, 'config': {'plan_fingerprint': self.target.config.plan_fingerprint,
                'sample_rate': 48000, 'output_mode': 'stereo', 'output_key': 'A',
                'hardware_ports': ['playback_FL', 'playback_FR']}, 'output_gain_db': -12}

    async def verify(self):
        return self.links_valid

    async def settled_latency(self):
        return copy.deepcopy(self.latency)

    async def guarded_rebuild_rendered(self, target, **options):
        self.stages += 1
        self.target = target
        self.latency['context']['plan_fingerprint'] = target.config.plan_fingerprint
        if options.get('before_ramp'): await options['before_ramp']()
        options['apply_candidate']()

    async def adopt(self):
        self.assertIsNotNone(adoption, 'AV Sync activation adoption is not implemented')
        return await adoption.adopt_latency_basis(
            service=self.service, native=self, build_target=self.build_target,
            latency=copy.deepcopy(self.latency), measurement_active=lambda: self.measurement_active)

    async def test_verified_activation_adopts_current_latency_without_touching_alignment(self):
        self.assertTrue(await self.adopt())
        committed = self.service.load()
        self.assertEqual(committed['modes']['stereo']['processing'], self.state['modes']['stereo']['processing'])
        self.assertEqual(committed['modes']['stereo']['av_sync']['basis'], {'main_l': 128, 'main_r': 128})
        self.assertEqual(committed['modes']['stereo']['av_sync']['basis_origin'], 'adopted')
        self.assertFalse(await self.adopt())
        self.assertEqual(self.stages, 1)

    async def test_stale_rendering_broken_links_and_measurement_do_not_adopt(self):
        for reason in ('stale', 'links', 'measurement'):
            self.latency['context']['plan_fingerprint'] = self.target.config.plan_fingerprint
            self.links_valid, self.measurement_active = True, False
            if reason == 'stale': self.latency['context']['plan_fingerprint'] = 'alignment-only-rendering'
            if reason == 'links': self.links_valid = False
            if reason == 'measurement': self.measurement_active = True
            with self.subTest(reason=reason):
                self.assertFalse(await self.adopt())
                self.assertEqual(self.service.load(), self.state)
        self.assertEqual(self.stages, 0)

    async def test_unknown_ir_never_becomes_a_guessed_basis(self):
        self.latency['outputs'][0]['unknown'] = 1
        await self.adopt()
        self.assertNotIn('main_l', self.service.load()['modes']['stereo']['av_sync']['basis'])


if __name__ == '__main__':
    unittest.main()
