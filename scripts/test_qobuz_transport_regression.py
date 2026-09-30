#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Qobuz queue-only navigation and independent track/renderer rate regressions."""

import copy
import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import sys
import threading
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import playback.media_readiness as readiness
import streaming.api as streaming_api
from playback.runtime import FxrouteTransitionRuntime
from playback.transition import PlaybackTransitionCoordinator, TransitionRequest
from streaming.qobuz import backend
from streaming.qobuz.provider import QobuzProvider
from test_external_source_failure_restore import HandoffFixture


class QueueOnlyDaemon:
    """The fork navigation endpoint selects metadata but never loads audio."""

    def __init__(self):
        self.tracks = [
            {'id': 42, 'title': 'First', 'sample_rate': 44.1, 'bit_depth': 16},
            {'id': 43, 'title': 'Second', 'sample_rate': 96.0, 'bit_depth': 24},
            {'id': 44, 'title': 'Third', 'sample_rate': 48.0, 'bit_depth': 24},
        ]
        self.index = 0
        self.playback = {'track_id': 42, 'state': 'Playing', 'sample_rate': 44100,
                         'bit_depth': 16, 'position_secs': 12, 'duration_secs': 200,
                         'volume': 1.0}
        self.fail_load = False
        self.confirm_load = True
        self.load_timeout = None
        self.repeat = 'Off'
        self.decoded_rates = {42: 44100, 43: 96000, 44: 48000}

    async def get(self, base, path, timeout=2.0):
        if path == '/api/status':
            return {'logged_in': True, 'qconnect': True, 'state': 'ready'}
        if path == '/api/playback':
            return dict(self.playback)
        if path == '/api/queue':
            return {'current_track': dict(self.tracks[self.index]), 'current_index': self.index,
                    'total_tracks': 3, 'history': copy.deepcopy(self.tracks[:self.index]),
                    'repeat': self.repeat,
                    'upcoming': copy.deepcopy(self.tracks[self.index + 1:])}
        raise AssertionError(path)

    async def post(self, base, path, body=None, timeout=2.0):
        if path == '/api/queue/repeat':
            self.repeat = {'off': 'Off', 'one': 'One', 'all': 'All'}[body['mode']]
            return {'repeat': self.repeat}
        if path.endswith('/next') or path.endswith('/previous'):
            if path.endswith('/next') and self.repeat == 'One':
                return {'track': dict(self.tracks[self.index])}
            offset = 1 if path.endswith('/next') else -1
            index = self.index + offset
            if not 0 <= index < len(self.tracks):
                return {'track': None}
            self.index = index
            return {'track': dict(self.tracks[index])}
        if path.endswith('/play-track'):
            self.load_timeout = timeout
            if self.fail_load:
                return None
            track = next(t for t in self.tracks if t['id'] == body['track_id'])
            if self.confirm_load:
                self.playback.update(track_id=track['id'], state='Playing', position_secs=0,
                                      sample_rate=self.decoded_rates[track['id']],
                                     bit_depth=track['bit_depth'])
            return {'playing': True, 'track_id': track['id']}
        if path.endswith('/play'):
            if self.playback['track_id']:
                self.playback['state'] = 'Playing'
            return {'playing': bool(self.playback['track_id'])}
        raise AssertionError(path)


class QobuzQueueNavigationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.daemon = QueueOnlyDaemon()
        self.provider = QobuzProvider()
        self.patches = [patch.object(backend, 'get_json', side_effect=self.daemon.get),
                        patch.object(backend, 'post_json', side_effect=self.daemon.post),
                        patch.object(backend, 'qbzd_installed', return_value=True),
                        patch.object(backend, 'is_reachable', return_value=True)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def action_fixture(self):
        fixture = HandoffFixture('qobuz', playing=False, player_available=False)
        deps = fixture.runtime._deps
        deps.get_qobuz_ui_state = self.provider.status
        deps.qobuz_loaded_track_id = self.provider.loaded_track_id
        deps.qobuz_play = self.provider.play
        deps.qobuz_play_track = self.provider.play_track
        deps.qobuz_navigate = lambda action: getattr(self.provider, action)()
        original = streaming_api._deps()
        self.enterContext(patch.object(streaming_api._runtime, 'deps', replace(original,
            get_qobuz_ui_state=self.provider.status,
            qobuz_target_track_from_state=lambda s: {'id': s['trackId'], 'sample_rate_hz': s['sample_rate']},
            qobuz_target_rate=lambda s: s['sample_rate'], coordinator_rate_change=lambda rate: False,
            run_coordinated_transition=fixture.coordinator.execute,
            get_current_playback_owner=lambda: 'qobuz', broadcast_qobuz_state=self.provider.status,
            publish_committed_playback_owner=AsyncMock(), qobuz_pin_unity=AsyncMock(),
            capture_source_intent=lambda: None, ensure_source_intent_current=lambda token: None)))
        return fixture

    async def test_cold_play_resolves_decoded_rate_before_auto_graph_staging(self):
        self.daemon.tracks[0]['sample_rate'] = 96.0
        self.daemon.playback.update(track_id=0, sample_rate=0, state='Stopped')
        fixture = self.action_fixture()
        staged_rates = []

        async def effects(req):
            staged_rates.append(req.target_rate)
            return {'dsp_reinitialized': False}

        fixture.runtime._deps.coordinator_establish_effects_and_helper = effects
        req = TransitionRequest(operation='qobuz-play', source='qobuz', target_rate=96000,
                                target_url='42', target_track={'id': '42'},
                                sample_rate_policy={'mode': 'auto'})
        result = await fixture.coordinator.execute(req)
        self.assertTrue(result.committed)
        self.assertEqual(self.daemon.playback['sample_rate'], 44100)
        self.assertEqual(staged_rates, [44100])
        self.assertEqual(fixture.rate, 44100)

    async def test_finished_queue_with_cleared_current_marker_keeps_next_a_noop(self):
        self.daemon.index = 2
        self.daemon.playback.update(track_id=44, sample_rate=48000, state='Paused',
                                    position_secs=200, duration_secs=200)
        original_get = self.daemon.get

        async def get(base, path, timeout=2.0):
            value = await original_get(base, path, timeout)
            if path == '/api/queue':
                # The fork clears its current marker when automatic Next
                # reaches EOF, making upcoming look like the whole queue.
                value.update(current_track=None, current_index=None,
                             history=[dict(self.daemon.tracks[-1])],
                             upcoming=copy.deepcopy(self.daemon.tracks))
            return value

        self.action_fixture()
        with patch.object(backend, 'get_json', side_effect=get):
            state = await streaming_api._qobuz_ui_start_action('next')
        self.assertEqual(state['status'], 'Paused')
        self.assertEqual(state['trackId'], '44')
        self.assertEqual(state['position'], 200)
        self.assertIsNone(self.daemon.load_timeout)

    async def test_play_queued_behind_next_resumes_the_new_track(self):
        await self.check_queued_action('play')

    async def test_excess_next_queued_at_queue_end_does_not_latch_the_gate(self):
        self.daemon.index = 1
        self.daemon.playback.update(track_id=43, sample_rate=96000)
        await self.check_queued_action('next')

    async def check_queued_action(self, action):
        self.daemon.playback['state'] = 'Paused'
        fixture = self.action_fixture()
        navigating = asyncio.Event()
        release = asyncio.Event()
        queued = asyncio.Event()
        original_run = fixture.coordinator.execute

        async def navigate(direction):
            navigating.set()
            await release.wait()
            return await getattr(self.provider, direction)()

        async def run(req):
            if navigating.is_set():
                queued.set()
            return await original_run(replace(req, sample_rate_policy={'mode': 'auto'}))

        fixture.runtime._deps.qobuz_navigate = navigate
        streaming_api.configure_streaming_api(replace(streaming_api._deps(), run_coordinated_transition=run))
        first = asyncio.create_task(streaming_api._qobuz_ui_start_action('next'))
        second = None
        try:
            await asyncio.wait_for(navigating.wait(), 1)
            second = asyncio.create_task(streaming_api._qobuz_ui_start_action(action))
            await asyncio.wait_for(queued.wait(), 1)
        finally:
            release.set()
            outcomes = await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
        self.assertTrue(all(isinstance(state, dict) for state in outcomes), outcomes)
        expected_id = 44 if action == 'next' else 43
        self.assertEqual(self.daemon.playback['track_id'], expected_id)
        self.assertEqual(outcomes[-1]['trackId'], str(expected_id))
        self.assertEqual(self.daemon.playback['state'], 'Playing')
        self.assertFalse(fixture.muted)
        self.assertFalse(fixture.coordinator.transition_blocked)
        self.assertIsNone(fixture.coordinator.last_error)

    async def test_next_loads_selected_track_not_just_queue_metadata(self):
        state = await self.provider.next()
        self.assertEqual(self.daemon.playback['track_id'], 43)
        self.assertEqual(state['trackId'], '43')
        self.assertEqual(state['sample_rate'], 96000)
        self.assertGreaterEqual(self.daemon.load_timeout, 60)

    async def test_previous_loads_selected_track(self):
        self.daemon.index = 2
        self.daemon.playback.update(track_id=44, sample_rate=48000)
        state = await self.provider.previous()
        self.assertEqual(self.daemon.playback['track_id'], 43)
        self.assertEqual(state['trackId'], '43')

    async def test_manual_next_advances_with_repeat_one_and_preserves_repeat(self):
        self.daemon.repeat = 'One'
        state = await self.provider.next()
        self.assertEqual(state['trackId'], '43')
        self.assertEqual(self.daemon.repeat, 'One')

    async def test_failed_next_restores_repeat_one(self):
        self.daemon.repeat = 'One'
        self.daemon.fail_load = True
        with self.assertRaises(RuntimeError):
            await self.provider.next()
        self.assertEqual(self.daemon.repeat, 'One')

    async def test_status_does_not_attribute_old_player_to_selected_queue_track(self):
        self.daemon.index = 1
        state = await self.provider.status()
        self.assertEqual(state['trackId'], '42')
        self.assertEqual(state['title'], 'First')
        self.assertEqual(state['sample_rate'], 44100)

    async def test_decoded_rate_overrides_catalog_maximum_for_loaded_track(self):
        self.daemon.playback['sample_rate'] = 48000
        state = await self.provider.status()
        self.assertEqual(state['sample_rate'], 48000)

    async def test_failed_navigation_load_is_not_reported_as_success(self):
        self.daemon.fail_load = True
        with self.assertRaises(RuntimeError):
            await self.provider.next()

    async def test_navigation_waits_for_actual_player_identity(self):
        self.daemon.confirm_load = False
        with patch('streaming.qobuz.provider.PLAY_TRACK_CONFIRM_TIMEOUT_S', 0.05, create=True):
            with self.assertRaisesRegex(RuntimeError, 'confirmed'):
                await self.provider.next()

    async def test_navigation_rejects_playing_queue_metadata_without_raw_identity(self):
        self.daemon.confirm_load = False
        self.daemon.playback.update(track_id=0, state='Playing', sample_rate=0)
        with patch('streaming.qobuz.provider.PLAY_TRACK_CONFIRM_TIMEOUT_S', 0.05):
            with self.assertRaisesRegex(RuntimeError, 'confirmed'):
                await self.provider.next()

    async def test_cancelled_download_drains_before_gate_release_and_source_restore(self):
        fixture = HandoffFixture('spotify', target='qobuz')
        started = threading.Event()
        release = threading.Event()
        self.daemon.playback['state'] = 'Paused'

        def post(base, path, body=None, timeout=2.0):
            if path.endswith('/next'):
                self.daemon.index = 1
                return {'track': dict(self.daemon.tracks[1])}
            if path.endswith('/play-track'):
                started.set()
                if not release.wait(3):
                    raise RuntimeError('Test download was not released')
                self.daemon.playback.update(track_id=43, state='Playing', sample_rate=96000)
                fixture.states['qobuz'].update(status='Playing', trackId='43', sample_rate=96000)
                return {'playing': True, 'track_id': 43}
            raise AssertionError(path)

        fixture.runtime._deps.qobuz_navigate = lambda action: getattr(self.provider, action)()
        req = TransitionRequest(operation='qobuz-next', source='qobuz', target_rate=44100,
                                target_url='42', target_track={'id': '42'})
        # Exercise the real thread-backed HTTP wrapper, not an async-only fake.
        self.patches[1].stop()
        with patch.object(backend, '_post', side_effect=post), \
                patch('playback.runtime.source.spotify_play', lambda: fixture.resume('spotify')):
            task = asyncio.create_task(fixture.coordinator.execute(req))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                task.cancel()
                await asyncio.sleep(0.02)
                self.assertFalse(task.done(), 'the download must remain owned by the transition')
                self.assertTrue(fixture.coordinator.lock.locked())
                self.assertTrue(fixture.muted)
                self.assertEqual(fixture.states['spotify']['status'], 'Paused')
                task.cancel()
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual(fixture.states['qobuz']['status'], 'Paused')
            self.assertEqual(fixture.states['spotify']['status'], 'Playing')
            self.assertFalse(fixture.coordinator.lock.locked())
            self.assertFalse(fixture.muted)

    async def test_queue_end_does_not_restart_loaded_track(self):
        self.daemon.index = 2
        state = await self.provider.next()
        self.assertIsNone(self.daemon.load_timeout)
        self.assertEqual(state['trackId'], '42')
        self.assertIs(state['navigation_changed'], False)

    async def test_real_coordinator_navigates_and_resolves_rates_under_closed_gate(self):
        physical = {'active_rate': 44100, 'force_rate': 44100}
        gate = {'muted': False}
        staged_rates = []

        async def mute(value, transition_id):
            gate['muted'] = value

        async def read_mute():
            return gate['muted']

        async def navigate(action):
            self.assertTrue(gate['muted'], 'navigation must be gated')
            return await getattr(self.provider, action)()

        async def force(rate, reason, **kwargs):
            self.assertTrue(gate['muted'], 'rate mutation must be gated')
            physical.update(active_rate=rate, force_rate=rate)
            return True

        async def effects(req):
            self.assertTrue(gate['muted'])
            staged_rates.append(req.target_rate)
            return {'dsp_reinitialized': False}

        deps = replace(main.make_playback_runtime_deps(),
            player=lambda: None, player_is_running=lambda: False,
            dsp_manager=lambda: None, dsp_runtime=lambda: None,
            get_qobuz_ui_state=self.provider.status, qobuz_navigate=navigate,
            qobuz_play=self.provider.status, qobuz_loaded_track_id=self.provider.loaded_track_id,
            get_current_track_info=lambda: None, get_playback_intent_generation=lambda: 0,
            get_samplerate_status=lambda: dict(physical),
            ensure_playback_samplerate_force=force,
            get_audio_output_overview=lambda *args: {'output_mode': {'mode': 'stereo'}},
            get_spotify_ui_state=AsyncMock(return_value={'status': 'Stopped'}),
            coordinator_establish_effects_and_helper=effects,
            coordinator_reconcile_post_start_graph=AsyncMock(return_value={'graph_complete': True}),
            wait_for_qobuz_sink_input_samplerate=AsyncMock(return_value=44100),
            playback_graph_links_complete=AsyncMock(return_value=True))
        runtime = FxrouteTransitionRuntime(deps)
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
        with patch.object(runtime, 'read_hardware_mute', new=read_mute), \
                patch.object(runtime, 'set_hardware_mute', new=mute), \
                patch.object(runtime, 'read_sink_mute', new=AsyncMock(return_value=False)), \
                patch.object(runtime, 'stabilize_effects_after_rate_change',
                             new=AsyncMock(return_value={'stabilized': True})):
            for action, expected_id, expected_rate in [
                ('next', 43, 96000), ('next', 44, 48000),
                ('previous', 43, 96000), ('previous', 42, 44100),
            ]:
                current = await self.provider.status()
                req = TransitionRequest(operation=f'qobuz-{action}', source='qobuz',
                    target_rate=current['sample_rate'], target_url=current['trackId'],
                    target_track={'id': current['trackId'], 'sample_rate_hz': current['sample_rate']})
                result = await coordinator.execute(req)
                self.assertTrue(result.committed)
                self.assertEqual(self.daemon.playback['track_id'], expected_id)
                self.assertEqual(physical['active_rate'], expected_rate)
                self.assertFalse(gate['muted'])
                self.assertIsNone(coordinator.last_error)
        self.assertEqual(staged_rates, [96000, 48000, 96000, 44100])


def request(**kwargs):
    return TransitionRequest(operation='qobuz-play', source='qobuz', target_rate=96000,
        target_url='42', target_track={'id': '42', 'sample_rate_hz': 96000}, **kwargs)


class QobuzRendererRateTests(unittest.IsolatedAsyncioTestCase):
    async def test_commit_rejects_queue_only_identity_even_with_stable_renderer(self):
        state = {'status': 'Playing', 'trackId': '42', 'sample_rate': 96000}
        deps = replace(main.make_playback_runtime_deps(),
            player=lambda: SimpleNamespace(state={}), dsp_manager=lambda: None,
            get_qobuz_ui_state=AsyncMock(return_value=state),
            qobuz_loaded_track_id=AsyncMock(return_value=0),
            wait_for_qobuz_sink_input_samplerate=AsyncMock(return_value=44100),
            get_samplerate_status=lambda: {'active_rate': 96000, 'force_rate': 96000},
            get_audio_output_overview=lambda: {'output_mode': {'mode': 'stereo'}},
            playback_graph_links_complete=AsyncMock(return_value=True))
        with self.assertRaisesRegex(RuntimeError, 'track mismatch'):
            await FxrouteTransitionRuntime(deps).verify_transition_graph(request())

    async def test_cancelled_resume_waits_for_playing_before_restoring_spotify(self):
        fixture = HandoffFixture('spotify', target='qobuz')
        resumed = asyncio.Event()
        playing_edge = asyncio.Event()

        async def play():
            resumed.set()
            return dict(fixture.states['qobuz'])

        async def state():
            if resumed.is_set() and not playing_edge.is_set():
                await playing_edge.wait()
                fixture.states['qobuz']['status'] = 'Playing'
            return dict(fixture.states['qobuz'])

        fixture.runtime._deps.get_qobuz_ui_state = state
        fixture.runtime._deps.qobuz_loaded_track_id = AsyncMock(return_value=123)
        fixture.runtime._deps.qobuz_play = play
        req = TransitionRequest(operation='qobuz-play', source='qobuz', target_rate=44100,
                                target_url='123', target_track={'id': '123'})
        with patch('playback.runtime.source.spotify_play', lambda: fixture.resume('spotify')):
            task = asyncio.create_task(fixture.coordinator.execute(req))
            try:
                await asyncio.wait_for(resumed.wait(), 1)
                for _ in range(2):
                    task.cancel()
                    await asyncio.sleep(0.01)
                    self.assertFalse(task.done())
                    self.assertTrue(fixture.coordinator.lock.locked())
                    self.assertTrue(fixture.muted)
                    self.assertEqual(fixture.states['spotify']['status'], 'Paused')
            finally:
                playing_edge.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual(fixture.states['qobuz']['status'], 'Paused')
        self.assertEqual(fixture.states['spotify']['status'], 'Playing')
        self.assertFalse(fixture.muted)

    async def test_commit_rejects_missing_renderer_readback(self):
        state = {'status': 'Playing', 'trackId': '42', 'sample_rate': 96000}
        deps = replace(main.make_playback_runtime_deps(),
            player=lambda: SimpleNamespace(state={}), dsp_manager=lambda: None,
            get_qobuz_ui_state=AsyncMock(return_value=state),
            qobuz_loaded_track_id=AsyncMock(return_value=42),
            wait_for_qobuz_sink_input_samplerate=AsyncMock(return_value=None),
            get_samplerate_status=lambda: {'active_rate': 96000, 'force_rate': 96000},
            get_audio_output_overview=lambda: {'output_mode': {'mode': 'stereo'}},
            playback_graph_links_complete=AsyncMock(return_value=True))
        with self.assertRaisesRegex(RuntimeError, 'renderer rate'):
            await FxrouteTransitionRuntime(deps).verify_transition_graph(request())

    async def test_navigation_resolves_new_track_rate_before_graph_staging(self):
        state = {'available': True, 'status': 'Playing', 'trackId': '43', 'sample_rate': 48000}
        deps = SimpleNamespace(**{**vars(main.make_playback_runtime_deps()),
            'qobuz_navigate': AsyncMock(return_value=state)})
        runtime = FxrouteTransitionRuntime(deps)
        target = await runtime.resolve_target_rate(replace(request(), operation='qobuz-next'))
        self.assertEqual(target, 48000)

    async def test_rate_discovery_accepts_stable_renderer_without_catalog_expectation(self):
        entries = [{'id': 12, 'sample_rate': 44100, 'corked': False}]
        with patch.object(readiness, 'list_qobuz_sink_inputs', return_value=entries):
            self.assertEqual(await readiness.wait_for_qobuz_sink_input_samplerate(timeout_ms=150), 44100)

    async def test_rate_discovery_does_not_carry_stability_across_stream_gap(self):
        entries = [{'id': 12, 'sample_rate': 44100, 'corked': False}]
        with patch.object(readiness, 'list_qobuz_sink_inputs', side_effect=[entries, [], entries]):
            with self.assertRaisesRegex(RuntimeError, 'stable'):
                await readiness.wait_for_qobuz_sink_input_samplerate(timeout_ms=100)

    async def test_start_reasserts_graph_rate_even_when_decoded_rate_matches_target(self):
        physical = {'active_rate': 96000, 'force_rate': 96000}
        state = {'available': True, 'status': 'Playing', 'trackId': '42', 'sample_rate': 96000}

        async def play():
            physical.update(active_rate=44100, force_rate=44100)
            return state

        async def force(rate, reason, **kwargs):
            physical.update(active_rate=rate, force_rate=rate)
            return True

        deps = replace(main.make_playback_runtime_deps(),
            player=lambda: SimpleNamespace(state={}), dsp_manager=lambda: None,
            qobuz_play=play, get_qobuz_ui_state=AsyncMock(return_value=state),
            qobuz_loaded_track_id=AsyncMock(return_value=42),
            get_samplerate_status=lambda: dict(physical), ensure_playback_samplerate_force=force,
            wait_for_qobuz_sink_input_samplerate=AsyncMock(return_value=44100),
            get_audio_output_overview=lambda: {'output_mode': {'mode': 'stereo'}},
            playback_graph_links_complete=AsyncMock(return_value=True))
        runtime = FxrouteTransitionRuntime(deps)
        await runtime.start_target_source(request())
        self.assertEqual(physical['force_rate'], 96000)
        result = await runtime.verify_transition_graph(request())
        self.assertEqual(result['qobuz_stream_rate'], 44100)
        self.assertTrue(result['committed'])


class QobuzActionRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_next_at_queue_end_preserves_finished_paused_track_without_gate_work(self):
        await self.check_queue_end(shuffle=False)

    async def test_next_at_shuffled_queue_end_is_also_a_non_failing_noop(self):
        await self.check_queue_end(shuffle=True)

    async def check_queue_end(self, *, shuffle):
        fixture = HandoffFixture('qobuz', playing=False, player_available=False)
        fixture.states['qobuz'].update(trackId='44', position=200, duration=200,
                                      queue_len=3, queue_index=2, next_track=None, loop='none',
                                      shuffle=shuffle)
        fixture.runtime._deps.qobuz_navigate = AsyncMock(return_value={
            **fixture.states['qobuz'], 'navigation_changed': False})
        fixture.runtime._deps.qobuz_loaded_track_id = AsyncMock(return_value=44)
        fixture.runtime._deps.qobuz_play_track = lambda _track: fixture.resume('qobuz')
        gate_changes = []

        async def mute(value, transition_id):
            gate_changes.append(value)
            await fixture.set_mute(value, transition_id)

        fixture.runtime.set_hardware_mute = mute
        original = streaming_api._deps()
        deps = replace(original,
            get_qobuz_ui_state=lambda: fixture.state('qobuz'),
            qobuz_target_track_from_state=lambda s: {'id': s['trackId']},
            qobuz_target_rate=lambda s: s['sample_rate'], coordinator_rate_change=lambda rate: False,
            run_coordinated_transition=fixture.coordinator.execute,
            get_current_playback_owner=lambda: 'qobuz',
            broadcast_qobuz_state=lambda: fixture.state('qobuz'),
            publish_committed_playback_owner=AsyncMock(), qobuz_pin_unity=AsyncMock(),
            capture_source_intent=lambda: None, ensure_source_intent_current=lambda token: None)
        streaming_api.configure_streaming_api(deps)
        try:
            state = await streaming_api._qobuz_ui_start_action('next')
        finally:
            streaming_api.configure_streaming_api(original)
        self.assertEqual(state['status'], 'Paused')
        self.assertEqual(state['position'], 200)
        self.assertEqual(state['trackId'], '44')
        self.assertFalse(fixture.muted)
        self.assertFalse(fixture.coordinator.transition_blocked)
        self.assertIsNone(fixture.coordinator.last_error)
        self.assertEqual(fixture.resume_requests, [])
        self.assertEqual(gate_changes, [])

    async def test_provider_and_global_navigation_enter_same_coordinator_action(self):
        original = streaming_api._deps()
        actions = []

        async def run(req):
            actions.append(req.operation)
            return SimpleNamespace(committed=True, transition_id='tr-test')

        state = {'status': 'Playing', 'available': True, 'trackId': '42', 'sample_rate': 96000}
        deps = replace(original,
            get_qobuz_ui_state=AsyncMock(return_value=state),
            qobuz_target_track_from_state=lambda s: {'id': s['trackId']},
            qobuz_target_rate=lambda s: s['sample_rate'],
            coordinator_rate_change=lambda rate: False, run_coordinated_transition=run,
            publish_committed_playback_owner=AsyncMock(), qobuz_pin_unity=AsyncMock(),
            broadcast_qobuz_state=AsyncMock(return_value=state),
            capture_source_intent=lambda: None, ensure_source_intent_current=lambda token: None)
        streaming_api.configure_streaming_api(deps)
        try:
            with patch('streaming.get_provider', return_value=SimpleNamespace(
                    next=AsyncMock(side_effect=AssertionError('ungated navigation')),
                    previous=AsyncMock(side_effect=AssertionError('ungated navigation')))):
                await streaming_api.api_streaming_provider_action('qobuz', 'next', None)
                await main._qobuz_global_control('previous')
        finally:
            streaming_api.configure_streaming_api(original)
        self.assertEqual(actions, ['qobuz-next', 'qobuz-previous'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
