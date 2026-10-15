#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Calibration commits retain the latency actually rendered during their sweeps."""
import copy
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_measurement_speaker_commit import SessionFixture
import test_autosub_candidate_session as autosub_support

AutoSubFixture = type('AutoSubFixture', (unittest.TestCase,), {
    name: getattr(autosub_support.AutoSubCandidateSessionTests, name)
    for name in ('setUp', 'target_for', 'build_target', 'make_owner', 'proposal')})


def evidence(roles, *, raw=False, dsp=0, balance=6144):
    return {'ready': True, 'verified': True, 'capture_id': 'successful-sweep',
            'rate': 48000, 'input': 0, 'bypassed': int(raw), 'balance_limited': 0,
            'context': {'timing_key': 'same-timing', 'output_key': 'dev'},
            'measured_roles': list(roles),
            'outputs': [{'role': role, 'dsp': dsp, 'balance': balance, 'intentional': 480,
                         'align': 336, 'sync': 12000, 'unknown': 0} for role in roles],
            'sources': [{'position': 'FL', 'dsp': dsp, 'balance': balance, 'unknown': 0,
                         'roles': list(roles)}]}


class SpeakerBasisTests(SessionFixture, unittest.IsolatedAsyncioTestCase):
    async def timing_readback(self):
        snapshot = await self.readback()
        snapshot.update(latency_tracking=True, av_sync=copy.deepcopy(self.live_latency),
                        measurement_latency=copy.deepcopy(self.sweep_latency))
        return snapshot

    def setup_timing(self):
        from measurement.speaker_commit import compile_speaker_candidate
        fingerprint, plan = compile_speaker_candidate(self.base, service=self.service, **self.context)
        target = self.build_target(plan, fingerprint=fingerprint)
        self.assertIn('bypass 1\n', target.text)
        self.sweep_latency = evidence(['left_low', 'left_high'], raw=True)
        self.live_latency = copy.deepcopy(self.sweep_latency)

    async def test_commit_keeps_existing_balance_and_unmeasured_roles(self):
        self.setup_timing()
        self.base['modes']['stereo-sub']['av_sync']['basis']['right_low'] = 42
        self.base = self.store.commit(self.base, expected_revision=self.base['revision'])
        candidate = copy.deepcopy(self.base)
        candidate['modes']['stereo-sub']['processing']['left_low']['alignment_ms'] = 3
        session = self.session(readback=self.timing_readback)
        await session.stage_candidate(candidate)
        committed = await session.commit_candidate(candidate)
        sync = committed['modes']['stereo-sub']['av_sync']
        self.assertEqual(sync['basis'], {'left_low': 128, 'left_high': 128, 'right_low': 42})
        self.assertEqual(sync['basis_origin'], 'calibrated')
        self.assertEqual(committed['modes']['stereo-sub']['processing'], candidate['modes']['stereo-sub']['processing'])

    async def test_equal_to_start_success_still_captures_basis(self):
        self.setup_timing()
        session = self.session(readback=self.timing_readback)
        await session.stage_candidate(self.base)
        committed = await session.commit_candidate(self.base)
        self.assertEqual(committed['revision'], self.base['revision'] + 1)
        self.assertEqual(committed['modes']['stereo-sub']['av_sync']['basis']['left_low'], 128)

    async def test_noop_basis_disk_failure_keeps_session_uncommitted(self):
        self.setup_timing()
        session = self.session(readback=self.timing_readback)
        await session.stage_candidate(self.base)
        def fail(*args, **kwargs):
            raise OSError('disk full')
        self.service.commit = fail
        with self.assertRaises(OSError):
            await session.commit_candidate(self.base)
        self.assertFalse(session.committed)

    async def test_drifted_or_unverified_capture_never_commits(self):
        for change in ('missing', 'unverified', 'rendering', 'input', 'dsp', 'bypass'):
            with self.subTest(change=change):
                self.setup_timing()
                if change == 'missing': self.sweep_latency = {}
                if change == 'unverified': self.sweep_latency['verified'] = False
                if change == 'rendering': self.live_latency['context']['timing_key'] = 'new-timing'
                if change == 'input': self.live_latency['input'] = 8192
                if change == 'dsp': self.live_latency['outputs'][0]['dsp'] = 1024
                if change == 'bypass': self.live_latency['bypassed'] = 0
                session = self.session(readback=self.timing_readback)
                await session.stage_candidate(self.base)
                with self.assertRaisesRegex((ValueError, RuntimeError), 'latency|timing'):
                    await session.commit_candidate(self.base)
                self.assertFalse(session.committed)
                self.assertEqual(self.store.load()['revision'], self.base['revision'])


class AutoSubBasisTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = AutoSubFixture('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.sweep = evidence(['main_l', 'main_r', 'sub1', 'sub2'], raw=True)
        self.live = evidence(['main_l', 'main_r', 'sub1', 'sub2'], dsp=6144, balance=0)

    def make_timed_owner(self):
        owner = self.fixture.make_owner(readback=self.readback)
        owner.activate(48000)
        owner.record_latency_capture(self.sweep)
        return owner

    async def readback(self):
        snapshot = await self.fixture.hardware.readback()
        snapshot.update(latency_tracking=True, measurement_latency=copy.deepcopy(self.sweep),
                        av_sync=copy.deepcopy(self.live))
        snapshot.update(copy.deepcopy(getattr(self, 'live_ingress', {})))
        return snapshot

    async def capture_external_ingress(self, *, kind='decoded', raw=True, fail_restore=False, hardware_ms=0):
        from test_measurement_external_ingress import IngressFixture
        from dsp.runtime import CommandResult, PipeWireLink
        from measurement.job_runner import MeasurementJobRunner
        ingress = IngressFixture()
        self.ingress_fixture = ingress
        native = ingress.native
        native._config = self.fixture.initial.config
        native._config_text = self.fixture.initial.text
        native._process = SimpleNamespace(returncode=None, pid=123)
        native._links = [PipeWireLink(f'fxroute_dsp:output_{index}', f'dev:playback_AUX{index - 1}')
                         for index in range(1, 5)]
        ingress.links = {(link.source, link.target) for link in native._links}
        native.configure_latency_tracking(input_latency_provider=lambda: hardware_ms + (2818 / 48 if native._surround_input else 0))
        frames = 0
        restore_started = False
        restore_readbacks = 0

        async def command(args):
            nonlocal restore_readbacks
            if restore_started and args == ('pw-link', '-l'):
                restore_readbacks += 1
                if fail_restore == 'readback' and restore_readbacks == 3:
                    ingress.links = {edge for edge in ingress.links
                                     if not edge[0].startswith('fxroute_surround_input:')}
            if (restore_started and fail_restore is True and args[0] == 'pw-link' and len(args) == 3
                    and args[1].startswith('fxroute_surround_input:')):
                return CommandResult(1, stderr='Injected ingress restore failure')
            return await ingress.command(args)

        async def control(command, *, reply=False):
            nonlocal frames
            if command.startswith('input latency '):
                frames = round(float(command.split()[-1]) * 48)
                return 'ok'
            if command == 'latency get':
                raw = native._effect_bypass
                payload = evidence(['main_l', 'main_r', 'sub1', 'sub2'], raw=raw,
                                   dsp=0 if raw else 6144, balance=6144 if raw else 0)
                payload.update(input=frames, target=0, minimum=frames, maximum=960000,
                               common=0, actual=frames)
                for row in payload['outputs']:
                    row['total'] = frames + row['dsp'] + row['balance']
                return json.dumps(payload)
            return await ingress.control(command, reply=reply)

        native._control = control
        native._control_unlocked = control
        native._run = command
        await ingress.select(kind)
        original = set(ingress.links)
        enter = native.enter_raw_measurement if raw else native.enter_active_measurement
        exit_scope = native.exit_raw_measurement if raw else native.exit_active_measurement

        async def finish(job):
            nonlocal restore_started
            self.assertFalse(native._surround_input)
            self.assertEqual(native._mapped_ingress, ())
            captured = await native.finish_measurement_latency(job)
            self.assertEqual(captured['input'], round(hardware_ms * 48))
            self.assertEqual(captured['bypassed'], int(raw))
            restore_started = fail_restore
            return captured

        job = {'id': 'external-child', 'channel': 'stereo', 'status': 'queued',
               'measurement_scope': 'raw_helper' if raw else 'active_chain'}
        runner = MeasurementJobRunner(
            get_job=lambda job_id: job, persist_job=lambda job: None,
            public_result=lambda result: result, cleanup_job=lambda job_id: None,
            retain_history=lambda: None, utc_now=lambda: 'now',
            is_terminal=lambda status: status in ('completed', 'failed', 'cancelled'),
            raw_scope_enter=enter, raw_scope_exit=exit_scope,
            active_scope_enter=enter, active_scope_exit=exit_scope, latency_capture_finish=finish)
        if fail_restore:
            with self.assertLogs('measurement.job_runner', level='ERROR'):
                await runner.run(job['id'], job, lambda job: {'message': 'Captured'})
        else:
            await runner.run(job['id'], job, lambda job: {'message': 'Captured'})
            self.assertEqual(ingress.links, original)
        self.assertEqual(job['status'], 'completed')
        self.child_capture = job['latency_capture']
        self.sweep = native.snapshot()['measurement_latency']
        self.live = await native.settled_latency()
        self.live_ingress = {key: native.snapshot()[key] for key in ('surround_input', 'mapped_ingress')}
        return native

    async def test_auto_sub_failed_physical_ingress_restore_never_commits(self):
        import main
        native = await self.capture_external_ingress(fail_restore=True)
        self.assertTrue(native.snapshot()['surround_input'])
        self.assertIsNot(self.sweep.get('ingress_restored'), True)
        self.assertEqual(self.live['input'], 2818)
        self.assertFalse(any(source.startswith('fxroute_surround_input:')
                             for source, target in self.ingress_fixture.links))
        with patch.object(main.runtime, 'dsp_runtime', native), \
                patch.object(main, '_require_dsp_manager', return_value=self.fixture.manager), \
                patch.object(main, 'build_plan_target', side_effect=lambda service, manager, plan, **kwargs:
                    self.fixture.build_target(plan, fingerprint=kwargs['fingerprint'])):
            owner = main._create_auto_sub_candidate_session(
                service=self.fixture.service, start_state=self.fixture.base,
                output_key='dev', channels=4, hardware_ports=list(native._config.hardware_ports))
            owner.activate(48000)
            owner.record_latency_capture(self.child_capture)
            with self.assertRaisesRegex(RuntimeError, 'latency|timing|ingress'):
                await owner.commit_staged()
        self.assertFalse(owner.committed)
        self.assertEqual(self.fixture.store.load(), self.fixture.base)

    async def test_scope_restoration_requires_a_final_physical_readback(self):
        await self.capture_external_ingress(fail_restore='readback')
        self.assertIsNot(self.sweep.get('ingress_restored'), True)
        self.assertFalse(any(source.startswith('fxroute_surround_input:')
                             for source, target in self.ingress_fixture.links))
        owner = self.make_timed_owner()
        with self.assertRaisesRegex(RuntimeError, 'latency|timing|ingress'):
            await owner.commit_staged()
        self.assertEqual(self.fixture.store.load(), self.fixture.base)

    async def test_auto_sub_external_scope_restore_commits_changed_winner(self):
        await self.capture_external_ingress()
        self.assertEqual(self.live['input'], 2818)
        owner = self.make_timed_owner()
        proposal = self.fixture.proposal(sub_delays={'sub1': 4, 'sub2': 0})
        await owner.stage(proposal)
        committed = await owner.commit_winner(proposal)
        self.assertTrue(owner.committed)
        self.assertEqual(committed['modes']['stereo-sub']['processing']['sub1']['alignment_ms'], 4)
        self.assertEqual(committed['modes']['stereo-sub']['av_sync']['basis']['sub1'], 128)

    async def test_auto_sub_external_scope_restore_commits_noop_winner(self):
        await self.capture_external_ingress()
        owner = self.make_timed_owner()
        committed = await owner.commit_staged()
        self.assertEqual(committed['revision'], self.fixture.base['revision'] + 1)
        self.assertEqual(committed['modes']['stereo-sub']['av_sync']['basis']['sub1'], 128)

    async def test_external_calibration_retains_hardware_src_without_recrediting_ingress(self):
        await self.capture_external_ingress(hardware_ms=750)
        self.assertEqual(self.sweep['input'], 36000)
        self.assertEqual(self.live['input'], 38818)
        owner = self.make_timed_owner()
        committed = await owner.commit_staged()
        self.assertTrue(owner.committed)
        self.assertEqual(committed['modes']['stereo-sub']['av_sync']['basis']['sub1'], 878)

    async def test_active_capture_retains_actual_sweep_and_original_ingress_timing(self):
        native = await self.capture_external_ingress(raw=False)
        self.assertEqual(self.live['input'], 2818)
        self.assertEqual(self.sweep['ingress_restore'], {
            'input': 2818, 'surround_input': True, 'mapped_ingress': [], 'sweep_input': 0})
        self.sweep['ingress_restore']['input'] = 1
        self.assertEqual(native.snapshot()['measurement_latency']['ingress_restore']['input'], 2818)

    async def test_auto_sub_rejects_unproven_or_changed_restored_input(self):
        await self.capture_external_ingress()
        original = copy.deepcopy(self.sweep)
        for change in ('missing-evidence', 'unconfirmed', 'input', 'routing', 'output', 'rate', 'timing'):
            with self.subTest(change=change):
                self.sweep = copy.deepcopy(original)
                self.live['input'] = 2818
                self.live['rate'] = 48000
                self.live['context'] = copy.deepcopy(original['context'])
                self.live_ingress['surround_input'] = True
                if change == 'missing-evidence': self.sweep.pop('ingress_restore', None)
                if change == 'unconfirmed': self.sweep.pop('ingress_restored', None)
                if change == 'input': self.live['input'] += 1
                if change == 'routing': self.live_ingress['surround_input'] = False
                if change == 'output': self.live['context']['output_key'] = 'other-device'
                if change == 'rate': self.live['rate'] = 44100
                if change == 'timing': self.live['context']['timing_key'] = 'other-timing'
                owner = self.make_timed_owner()
                with self.assertRaisesRegex(RuntimeError, 'latency|timing|ingress'):
                    await owner.commit_staged()
                self.assertFalse(owner.committed)
                self.assertEqual(self.fixture.store.load()['revision'], self.fixture.base['revision'])

    async def test_auto_sub_requires_the_same_restoration_destination_for_every_child(self):
        await self.capture_external_ingress()
        owner = self.make_timed_owner()
        self.sweep['ingress_restore']['input'] += 1
        with self.assertRaisesRegex(RuntimeError, 'run changed'):
            await owner.commit_staged()
        self.assertFalse(owner.committed)

    async def test_auto_sub_rejects_missing_restoration_evidence_for_one_child(self):
        await self.capture_external_ingress()
        owner = self.make_timed_owner()
        child = copy.deepcopy(self.sweep)
        child.pop('ingress_restore')
        owner.record_latency_capture(child)
        with self.assertRaisesRegex(RuntimeError, 'run changed'):
            await owner.commit_staged()
        self.assertFalse(owner.committed)

    async def test_auto_sub_verified_mapped_restore_keeps_sweep_basis(self):
        await self.capture_external_ingress(kind='mapped')
        self.assertEqual(len(self.sweep['ingress_restore']['mapped_ingress']), 3)
        self.assertFalse(any(target == 'fxroute_dsp:input_3'
                             for source, target in self.ingress_fixture.links))
        self.assertTrue(await self.ingress_fixture.native.verify())
        owner = self.make_timed_owner()
        committed = await owner.commit_staged()
        self.assertEqual(committed['modes']['stereo-sub']['av_sync']['basis']['sub1'], 128)
        self.assertTrue(owner.committed)

    async def test_auto_sub_rejects_replaced_zero_latency_mapped_ingress(self):
        await self.capture_external_ingress(kind='mapped')
        owner = self.make_timed_owner()
        self.live_ingress['mapped_ingress'][0][0] = 'other:capture_AUX8'
        with self.assertRaisesRegex(RuntimeError, 'latency|timing|ingress'):
            await owner.commit_staged()
        self.assertFalse(owner.committed)

    async def test_auto_sub_still_rejects_replaced_dormant_mapping(self):
        await self.capture_external_ingress(kind='mapped')
        owner = self.make_timed_owner()
        self.live_ingress['mapped_ingress'][2][0] = 'other:capture_AUX2'
        with self.assertRaisesRegex(RuntimeError, 'latency|timing|ingress'):
            await owner.commit_staged()
        self.assertFalse(owner.committed)

    async def test_auto_sub_changed_mapped_destination_between_children_is_rejected(self):
        await self.capture_external_ingress(kind='mapped')
        owner = self.make_timed_owner()
        self.sweep['ingress_restore']['mapped_ingress'][2][0] = 'other:capture_AUX2'
        with self.assertRaisesRegex(RuntimeError, 'run changed'):
            await owner.commit_staged()
        self.assertFalse(owner.committed)

    async def test_auto_sub_anchors_raw_sweep_not_restored_dsp(self):
        owner = self.make_timed_owner()
        proposal = self.fixture.proposal(sub_delays={'sub1': 4, 'sub2': 0})
        await owner.stage(proposal)
        result = await owner.commit_winner(proposal)
        self.assertEqual(result['modes']['stereo-sub']['av_sync']['basis']['sub1'], 128)
        self.assertEqual(result['modes']['stereo-sub']['processing']['sub1']['alignment_ms'], 4)

    async def test_auto_sub_noop_still_anchors_successful_sweep(self):
        owner = self.make_timed_owner()
        result = await owner.commit_staged()
        self.assertEqual(result['revision'], self.fixture.base['revision'] + 1)
        self.assertEqual(result['modes']['stereo-sub']['av_sync']['basis']['sub1'], 128)

    async def test_auto_sub_rejects_changed_rendering_after_sweep(self):
        self.live['context']['timing_key'] = 'other-IR'
        owner = self.make_timed_owner()
        with self.assertRaisesRegex((ValueError, RuntimeError), 'latency|timing'):
            await owner.commit_staged()
        self.assertEqual(self.fixture.store.load()['revision'], self.fixture.base['revision'])

    async def test_auto_sub_cannot_reuse_another_runs_runtime_evidence(self):
        owner = self.fixture.make_owner(readback=self.readback)
        owner.activate(48000)
        with self.assertRaisesRegex(RuntimeError, 'run|capture|evidence'):
            await owner.commit_staged()

    async def test_auto_sub_accumulates_only_this_runs_successfully_measured_roles(self):
        owner = self.fixture.make_owner(readback=self.readback)
        owner.activate(48000)
        for roles in (['main_l', 'sub1'], ['main_r', 'sub2']):
            child = copy.deepcopy(self.sweep)
            child['measured_roles'] = roles
            owner.record_latency_capture(child)
        self.sweep['measured_roles'] = ['main_r', 'sub2']
        committed = await owner.commit_staged()
        self.assertEqual(set(committed['modes']['stereo-sub']['av_sync']['basis']),
                         {'main_l', 'main_r', 'sub1', 'sub2'})


if __name__ == '__main__':
    unittest.main()
