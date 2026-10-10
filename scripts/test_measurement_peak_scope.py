#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Native sweep peaks exclude program ingress and retain every retry's safety evidence."""

import asyncio
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dsp.runtime import DSPRuntime, DSPRuntimeConfig
import test_measurement_capture_evidence as capture_support
from test_measurement_external_ingress import IngressFixture

CaptureFixture = type('CaptureFixture', (unittest.TestCase,), {
    name: getattr(capture_support.CaptureEvidenceTests, name)
    for name in ('setUp', 'spawn', 'start', 'complete')})


class PeakScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = CaptureFixture('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.ingress = IngressFixture()
        self.native = self.ingress.native
        layout = ({'name': 'main_l', 'routes': [{'input': 0, 'gain': 1}]},
                  {'name': 'main_r', 'routes': [{'input': 1, 'gain': 1}]})
        self.native._config = DSPRuntimeConfig('surround', 'hardware', 48000,
                                               ('playback_AUX0', 'playback_AUX1'), layout)
        self.native._config_text = 'rate 48000\ninputs 8\noutputs 2\n'
        self.native.configure_latency_tracking(
            input_latency_provider=lambda: 2818 / 48 if self.native._surround_input else 0)
        self.frames = 0
        self.peaks = [0.0, 0.4]
        self.events = []
        self.play_count = 0
        self.fail_control = None
        self.play_peak = lambda attempt: 0.1

        async def control(command, *, reply=False):
            if command == self.fail_control:
                raise RuntimeError('Injected peak control failure')
            if command.startswith('input latency '):
                self.frames = round(float(command.split()[-1]) * 48)
                return 'ok'
            if command == 'latency get':
                if any(source.startswith('fxroute_surround_input:') for source, target in self.ingress.links):
                    self.peaks[1] = max(self.peaks[1], 0.4)
                return json.dumps({
                    'ready': True, 'rate': 48000, 'input': self.frames,
                    'target': 0, 'minimum': self.frames, 'maximum': 960000,
                    'common': 0, 'actual': self.frames, 'bypassed': int(self.native._effect_bypass),
                    'balance_limited': 0,
                    'outputs': [{'dsp': 0, 'intentional': 0, 'align': 0, 'balance': 0,
                                 'sync': 0, 'total': self.frames, 'unknown': 0} for _ in layout],
                    'sources': [{'dsp': 0, 'balance': 0, 'unknown': 0} for _ in range(8)]})
            if command == 'peaks reset':
                self.events.append('peak-reset')
                self.peaks[:] = [0.0, 0.0]
                return 'ok'
            if command == 'peaks get':
                self.events.append('peak-read')
                return json.dumps({'peaks': list(self.peaks)})
            return await self.ingress.control(command, reply=reply)

        self.native._control = control
        self.native._control_unlocked = control
        await self.native.set_surround_input(True)
        self.original_links = set(self.ingress.links)
        store = self.fixture.store
        store.raw_scope_enter = self.native.enter_raw_measurement
        store.raw_scope_exit = self.native.exit_raw_measurement
        store.active_scope_enter = self.native.enter_active_measurement
        store.active_scope_exit = self.native.exit_active_measurement
        store.latency_capture_finish = self.native.finish_measurement_latency
        store.runtime_snapshot_provider = self.native.snapshot
        loop = asyncio.get_running_loop()
        store.peak_capture_begin_sync = lambda job_id: asyncio.run_coroutine_threadsafe(
            self.native.begin_measurement_peak_capture(job_id), loop).result()
        spawn = self.fixture.spawn

        def play(job_id, command):
            if job_id in store._job_runner.cancelled_jobs:
                raise RuntimeError('Measurement cancelled.')
            if command[0] == 'pw-play':
                self.events.append('play')
                self.play_count += 1
                self.peaks[0] = max(self.peaks[0], self.play_peak(self.play_count))
            else:
                self.events.append('record')
            return spawn(job_id, command)

        store._start_job_process = play
        sleeping = patch('measurement.host_capture.time.sleep',
                         lambda seconds: self.events.append(('sleep', seconds)))
        sleeping.start()
        self.addCleanup(sleeping.stop)

    async def capture(self):
        return await self.fixture.complete(measurement_scope='raw_helper')

    async def test_capture_window_excludes_pre_entry_and_restored_program_peaks(self):
        job = await self.capture()
        self.assertEqual(job['status'], 'completed', job.get('error'))
        evidence = job['latency_capture']
        self.assertEqual(evidence['capture_id'], job['id'])
        self.assertTrue(evidence['verified'])
        self.assertEqual(evidence['output_peaks'], {'output_1': 0.1, 'output_2': 0.0})
        self.assertEqual(await self.native.read_output_peaks(), {'output_1': 0.1, 'output_2': 0.4})
        self.assertEqual(self.ingress.links, self.original_links)
        reset = self.events.index('peak-reset')
        self.assertLess(self.events.index('record'), reset)
        self.assertEqual(self.events[reset - 1], ('sleep', 0.75))
        self.assertLess(reset, self.events.index('play'))
        evidence['output_peaks']['output_1'] = 99
        self.assertEqual(self.native.snapshot()['measurement_latency']['output_peaks']['output_1'], 0.1)

    async def test_retry_keeps_an_unsafe_earlier_take_in_the_same_scope(self):
        self.fixture.store._capture_policy._try_raise_mic = lambda analysis, **kw: kw['attempt_index'] == 0
        self.play_peak = lambda attempt: 2.0 if attempt == 1 else 0.1
        job = await self.capture()
        self.assertEqual(job['status'], 'completed', job.get('error'))
        self.assertEqual(self.play_count, 2)
        self.assertEqual(job['latency_capture']['output_peaks'], {'output_1': 2.0, 'output_2': 0.0})

    async def test_reset_failure_cannot_play_or_certify_a_sweep(self):
        self.fail_control = 'peaks reset'
        job = await self.capture()
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(self.play_count, 0)
        self.assertIsNone(job.get('latency_capture'))
        self.assertEqual(self.ingress.links, self.original_links)
        self.assertFalse(self.native._measurement_scope_lock.locked())

    async def test_final_peak_read_failure_cannot_certify_worker_success(self):
        self.fail_control = 'peaks get'
        job = await self.capture()
        self.assertEqual(job['status'], 'failed')
        self.assertIsNone(job.get('latency_capture'))
        self.assertIsNot(self.native.snapshot()['measurement_latency'].get('verified'), True)
        self.assertEqual(self.ingress.links, self.original_links)
        self.assertFalse(self.native._measurement_scope_lock.locked())

    async def test_capture_id_and_scope_cannot_be_replaced_or_reused(self):
        with self.assertRaisesRegex(RuntimeError, 'scope'):
            await self.native.begin_measurement_peak_capture('outside')
        previous = await self.native.enter_raw_measurement()
        try:
            await self.native.begin_measurement_peak_capture('first')
            self.peaks[0] = 2.0
            with self.assertRaisesRegex(RuntimeError, 'another job'):
                await self.native.begin_measurement_peak_capture('second')
            with self.assertRaisesRegex(RuntimeError, 'another job'):
                await self.native.finish_measurement_latency({'id': 'second'})
            captured = await self.native.finish_measurement_latency({'id': 'first', 'channel': 'left'})
            self.assertEqual(captured['output_peaks']['output_1'], 2.0)
        finally:
            await self.native.exit_raw_measurement(previous)
        previous = await self.native.enter_raw_measurement()
        try:
            uncaptured = await self.native.finish_measurement_latency({'id': 'next', 'channel': 'left'})
            self.assertNotIn('output_peaks', uncaptured)
        finally:
            await self.native.exit_raw_measurement(previous)

    async def test_cancellation_waits_for_peak_control_before_releasing_scope(self):
        started, release = asyncio.Event(), asyncio.Event()
        control = self.native._control_unlocked

        async def held_reset(command, *, reply=False):
            if command == 'peaks reset':
                started.set()
                await release.wait()
            return await control(command, reply=reply)

        self.native._control_unlocked = held_reset
        job = await self.fixture.start(measurement_scope='raw_helper')
        task = self.fixture.store._job_tasks[job['id']]
        try:
            await asyncio.wait_for(started.wait(), 3)
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertTrue(self.native._measurement_scope_lock.locked())
                self.assertFalse(self.native._surround_input)
        finally:
            release.set()
            await asyncio.wait_for(task, 3)
        final = self.fixture.store.get_job(job['id'])
        self.assertEqual(final['status'], 'cancelled')
        self.assertIsNone(final.get('latency_capture'))
        self.assertEqual(self.play_count, 0)
        self.assertEqual(self.ingress.links, self.original_links)
        self.assertFalse(self.native._measurement_scope_lock.locked())

    async def test_cancel_during_final_peak_ack_finishes_exchange_before_restoring(self):
        started, release = asyncio.Event(), asyncio.Event()
        control = self.native._control_unlocked
        home = tempfile.TemporaryDirectory(prefix='peak-control-')
        self.addCleanup(home.cleanup)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        server.setblocking(False)
        client.setblocking(False)
        self.native._control_path = Path(home.name) / 'server.sock'
        server.bind(str(self.native._control_path))
        client.bind(str(Path(home.name) / 'client.sock'))
        self.native._control_socket = client
        self.native._control = DSPRuntime._control.__get__(self.native)
        self.native._control_unlocked = DSPRuntime._control_unlocked.__get__(self.native)
        loop = asyncio.get_running_loop()
        final_ack_sent = False

        async def serve():
            nonlocal final_ack_sent
            while True:
                data, address = await loop.sock_recvfrom(server, 65536)
                command = data.decode()
                if command == 'peaks get':
                    started.set()
                    await release.wait()
                answer = await control(command, reply=True)
                await loop.sock_sendto(server, ('ok\n' if answer == 'ok' else answer).encode(), address)
                if command == 'peaks get':
                    final_ack_sent = True

        endpoint = asyncio.create_task(serve())
        job = await self.fixture.start(measurement_scope='raw_helper')
        task = self.fixture.store._job_tasks[job['id']]
        try:
            await asyncio.wait_for(started.wait(), 3)
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertTrue(self.native._measurement_scope_lock.locked())
                self.assertFalse(self.native._surround_input)
                self.assertTrue(self.native._effect_bypass)
                self.assertFalse(self.native._control_poisoned)
        finally:
            release.set()
            try:
                await asyncio.wait_for(task, 3)
            finally:
                endpoint.cancel()
                await asyncio.gather(endpoint, return_exceptions=True)
        final = self.fixture.store.get_job(job['id'])
        self.assertTrue(final_ack_sent)
        self.assertEqual(final['status'], 'cancelled')
        self.assertIsNone(final.get('latency_capture'))
        self.assertIsNot(self.native.snapshot()['measurement_latency'].get('verified'), True)
        self.assertFalse(self.native._control_poisoned)
        self.assertFalse(self.native._effect_bypass)
        self.assertEqual(self.ingress.links, self.original_links)
        self.assertFalse(self.native._measurement_scope_lock.locked())


if __name__ == '__main__':
    unittest.main()
