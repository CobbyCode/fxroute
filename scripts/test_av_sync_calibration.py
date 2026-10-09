#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Calibration commits retain the latency actually rendered during their sweeps."""
import copy
import sys
import unittest
from pathlib import Path

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
        return snapshot

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
