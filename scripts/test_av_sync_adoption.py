#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Legacy timing is adopted only from a verified normal activation."""
import copy
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from test_output_state_api import make_manager, make_service
from test_av_sync_src import graph
from audio.src_latency import graph_src_latency
from dsp.runtime import DSPRuntime, PipeWireLink

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

    async def test_guarded_adoption_retains_the_live_app_src_credit(self):
        native = DSPRuntime(self.manager)
        native._process = SimpleNamespace(returncode=None, pid=123)
        native._config = self.target.config
        native._config_text = self.target.text
        native._links = [PipeWireLink('fxroute_dsp:output_1', 'A:playback_FL')]
        native._output_gain_db = -12
        native.verify = self.verify
        native.configure_latency_tracking(input_latency_provider=main._electronic_input_latency_ms)
        frames = 0
        async def control(command, *, reply):
            nonlocal frames
            if command.startswith('input latency '):
                frames = round(float(command.split()[-1]) * 48)
                return 'ok'
            if command == 'latency get':
                payload = copy.deepcopy(self.latency)
                payload.update(input=frames, target=0, minimum=frames + 6480,
                               maximum=960000, common=0, actual=frames + 6480)
                for row, align in zip(payload['outputs'], (336, 0)):
                    row.update(align=align, sync=0, total=frames + 6144 + align)
                return json.dumps(payload)
            return 'ok'
        async def render(config, text, *, initial_output_gain_db):
            native._config = config
            native._config_text = text
            native._last_input_latency_ms = None
        native._control_unlocked = control
        native._run_sync = render
        reader = SimpleNamespace(read=lambda owner=None, **options: graph_src_latency(graph(), **options))
        with patch.object(main.runtime, 'dsp_runtime', native), \
                patch.object(main, '_src_latency_reader', reader), \
                patch.object(main, '_measurement_session_owns_live_rate', return_value=False):
            latency = await native.settled_latency()
            self.assertEqual(latency['input_ms'], 750)
            adopted = await adoption.adopt_latency_basis(
                service=self.service, native=native, build_target=self.build_target,
                latency=latency, measurement_active=lambda: False)
            self.assertTrue(adopted)
            self.assertEqual((await native.settled_latency())['input_ms'], 750)
            self.assertEqual(self.service.load()['modes']['stereo']['av_sync']['basis_origin'], 'adopted')
            self.assertEqual(self.service.load()['modes']['stereo']['processing'],
                             self.state['modes']['stereo']['processing'])

    async def test_output_mode_switch_in_flight_defers_the_basis_adoption(self):
        """The switch commit/revert pair owns the head while it is in flight.

        An adoption committing between the switch's commit and its revert
        moves the head past the revert's expected revision, which would leave
        a failed switch committed.  The adoption stays off such a window.
        """
        native = SimpleNamespace(snapshot=lambda: {"config": {
            "output_key": "A", "sample_rate": 48000,
            "hardware_ports": ["playback_FL", "playback_FR"]}})
        adopt = AsyncMock(return_value=True)
        with patch.object(main.runtime, "dsp_runtime", native), \
                patch.object(main, "_measurement_session_owns_live_rate", return_value=False), \
                patch.object(main, "_playback_transition_is_active", return_value=False), \
                patch.object(adoption, "adopt_latency_basis", adopt):
            try:
                main.runtime.output_mode_switch_depth = 1
                await main._adopt_av_sync_basis(self.latency)
                adopt.assert_not_awaited()
                main.runtime.output_mode_switch_depth = 0
                await main._adopt_av_sync_basis(self.latency)
                adopt.assert_awaited_once()
                # The adoption also refuses at commit time, so a switch that
                # starts after the entry check still owns the revision.
                write_owner = adopt.await_args.kwargs["measurement_active"]
                self.assertFalse(write_owner())
                main.runtime.output_mode_switch_depth = 1
                self.assertTrue(write_owner())
            finally:
                main.runtime.output_mode_switch_depth = 0

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
