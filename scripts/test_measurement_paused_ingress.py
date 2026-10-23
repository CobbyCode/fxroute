#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""A retained paused programme must not own the measurement ingress."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import measurement.session as measurement_session
from audio.src_latency import SrcLatencyReader
from dsp.runtime import DSPRuntimeConfig
from playback.orchestration import PlaybackOrchestrator
from playback.runtime import FxrouteTransitionRuntime
from playback.transition import TransitionRequest
from test_av_sync_src import link, node
from test_measurement_external_ingress import IngressFixture


MPV = (("mpv:output_FL", "fxroute_dsp:input_1"),
       ("mpv:output_FR", "fxroute_dsp:input_2"))


class PausedIngressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = IngressFixture()
        self.native = self.fixture.native
        self.native._process.pid = 123
        self.native._config = DSPRuntimeConfig(
            'surround', 'hardware', 48000, ('playback_AUX0', 'playback_AUX1'),
            ({'name': 'main_l'}, {'name': 'main_r'}))
        self.state = 'init'
        self.output_state = 'active'
        self.engine_ready = True
        self.acknowledge_input = True
        self.frames = 0
        self.input_commands = []
        self.session = measurement_session.MeasurementSampleRateSession()
        self.player = SimpleNamespace(state={'current_file': 'retained', 'paused': True, 'playing': False,
                                            'audio_output_params': {'channel-count': 2}})
        def pause(value):
            self.player.state.update(paused=value, playing=not value)
        self.player.set_pause = pause
        self.committed_entries = 0
        async def context():
            return {'source': 'radio', 'target_url': 'retained', 'target_track': {
                'source': 'radio', 'url': 'retained', 'sample_rate_hz': 48000}}
        async def transition(request):
            self.assertEqual(request.operation, 'measurement-entry')
            _, _, adapter = self.transport()
            await adapter.quiet_old_source(request)
            result = await adapter.verify_measurement_entry(request)
            self.committed_entries += 1
            return SimpleNamespace(committed=result['committed'])
        services = replace(main._make_measurement_services(),
            get_samplerate_status=lambda: {'active_rate': 48000, 'force_rate': 48000},
            coordinator_current_playback_context=context, run_coordinated_transition=transition,
            get_current_track_info=lambda: None, get_player=lambda: self.player,
            get_player_audio_samplerate=lambda: 48000,
            stdin_measurement_acquire=None, stdin_measurement_release=None, input_entry_guard=None)
        for target, name, value in (
            (main.runtime, 'dsp_runtime', self.native),
            (main.runtime, 'player_instance', self.player),
            (main, 'measurement_sr_session', self.session),
            (measurement_session, '_services', services),
            (measurement_session, '_playback_state_before_measurement', None),
            (main, '_src_latency_reader', SrcLatencyReader()),
            (self.native, '_control_unlocked', self.control),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        dump = patch.object(main._src_latency_reader, '_dump', self.graph)
        dump.start()
        self.addCleanup(dump.stop)
        pause = patch('audio.src_latency.SETTLE_PAUSE_SECONDS', 0)
        pause.start()
        self.addCleanup(pause.stop)
        self.native.configure_latency_tracking(input_latency_provider=main._electronic_input_latency_ms)
        await self.native.set_mapped_ingress(MPV)
        self.original_links = set(self.fixture.links)

    def graph(self):
        value = [node(1, 'mpv', 'Stream/Output/Audio', 750000000, state='idle'),
                 node(2, 'fxroute_dsp_sink', 'Audio/Sink'), node(3, 'fxroute_dsp'),
                 node(4, 'hardware', 'Audio/Sink', 250000000),
                 link(1, 2, 'paused' if self.player.state['paused'] else 'active')]
        for source, target in self.fixture.links:
            if source.startswith('mpv:') and target.startswith('fxroute_dsp:'):
                value.append(link(1, 3, self.state))
            elif source.startswith('fxroute_dsp_sink:'):
                value.append(link(2, 3))
            elif source.startswith('fxroute_dsp:output_'):
                value.append(link(3, 4, self.output_state))
        return value

    async def control(self, command, *, reply=False):
        if command.startswith('input latency '):
            self.input_commands.append(command)
            if self.acknowledge_input:
                self.frames = round(float(command.split()[-1]) * 48)
            return 'ok'
        if command == 'latency get':
            return json.dumps({
                'ready': self.engine_ready, 'rate': 48000, 'input': self.frames,
                'target': 0, 'minimum': self.frames, 'maximum': 960000,
                'common': 0, 'actual': self.frames, 'bypassed': int(self.native._effect_bypass),
                'outputs': [{'dsp': 0, 'intentional': 0, 'align': 0, 'balance': 0,
                             'sync': 0, 'total': self.frames} for _ in range(2)]})
        return await self.fixture.control(command, reply=reply)

    async def test_paused_mpv_enters_active_and_raw_measurement_on_the_real_measurement_path(self):
        await self.session.start(48000)
        session_links = set(self.fixture.links)
        for raw in (False, True):
            with self.subTest(raw=raw):
                enter = self.native.enter_raw_measurement if raw else self.native.enter_active_measurement
                leave = self.native.exit_raw_measurement if raw else self.native.exit_active_measurement
                previous = await enter()
                try:
                    value = await self.native.settled_latency()
                    self.assertTrue(value['ready'])
                    self.assertEqual(value['input'], 12000)  # Hardware SRC only: 250 ms at 48 kHz.
                    self.assertEqual(bool(value['bypassed']), raw)
                    self.assertFalse(set(MPV) & self.fixture.links)
                    self.assertIn(('fxroute_dsp_sink:monitor_FL', 'fxroute_dsp:input_1'), self.fixture.links)
                    captured = await self.native.finish_measurement_latency({'id': 'silent-probe'})
                    self.assertTrue(captured['verified'])
                    self.assertNotIn('ingress_restore', captured)
                finally:
                    await leave(previous)
                self.assertEqual(self.fixture.links, session_links)
                self.assertFalse(self.native._measurement_scope_lock.locked())

    async def test_session_without_a_child_scope_has_the_spl_and_commit_readback_path(self):
        await self.session.start(48000)
        value = await self.native.settled_latency()
        self.assertEqual(value['input'], 12000)
        self.assertEqual(self.native._mapped_ingress, ())
        self.assertFalse(self.native._measurement_active)
        self.assertTrue(await self.native.verify())

    async def test_session_ownership_survives_reconciliation_and_rejects_mpv_reentry(self):
        await self.session.start(48000)
        session_links = set(self.fixture.links)
        with self.assertRaisesRegex(RuntimeError, 'owned by the measurement session'):
            await self.native.set_mapped_ingress(MPV)
        await self.native._reconcile_ingress_links()
        self.assertEqual(self.fixture.links, session_links)
        self.assertTrue(self.native.snapshot()['programme_input_suspended'])
        self.assertEqual((await self.native.settled_latency())['input'], 12000)

    async def test_new_job_reclaims_the_ingress_after_idle_session_playback(self):
        await self.session.start(48000)
        self.native.release_programme_input()
        await self.native.set_mapped_ingress(MPV)
        self.player.set_pause(False)
        self.assertTrue(set(MPV).issubset(self.fixture.links))
        await self.session.register_spl_job('next-silent-job')
        self.assertTrue(self.player.state['paused'])
        self.assertFalse(set(MPV) & self.fixture.links)
        self.assertEqual((await self.native.settled_latency())['input'], 12000)

    async def test_cancelled_session_takeover_restores_the_old_graph_and_releases_locks(self):
        started = asyncio.Event()
        command = self.native._run
        disconnects = 0
        async def held(args):
            nonlocal disconnects
            if args[:2] == ('pw-link', '-d'):
                disconnects += 1
                if disconnects == 2:
                    started.set()
                    await asyncio.Event().wait()
            return await command(args)
        self.native._run = held
        task = asyncio.create_task(self.session.start(48000))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        self.assertEqual(self.fixture.links, self.original_links)
        self.assertFalse(self.native.snapshot()['programme_input_suspended'])
        self.assertFalse(self.session.owns_audio_graph)
        self.assertEqual(self.committed_entries, 0)
        self.assertFalse(self.session.lock.locked())
        self.assertFalse(self.native._lock.locked())

    async def test_failed_monitor_link_takeover_does_not_claim_a_measurement_session(self):
        from dsp.runtime import CommandResult
        command = self.native._run
        async def broken(args):
            if len(args) == 3 and args[1].startswith('fxroute_dsp_sink:'):
                return CommandResult(1, stderr='Injected monitor link failure')
            return await command(args)
        self.native._run = broken
        with self.assertRaisesRegex(RuntimeError, 'Could not establish guarded measurement entry'):
            await self.session.start(48000)
        self.assertEqual(self.fixture.links, self.original_links)
        self.assertFalse(self.native.snapshot()['programme_input_suspended'])
        self.assertFalse(self.session.owns_audio_graph)

    async def test_failed_reclaim_keeps_the_existing_session_ownership(self):
        from dsp.runtime import CommandResult
        await self.session.start(48000)
        session_links = set(self.fixture.links)
        command = self.native._run
        failed = False
        async def broken(args):
            nonlocal failed
            if args == ('pw-link', '-l') and not failed:
                failed = True
                return CommandResult(1, stderr='Injected reclaim readback failure')
            return await command(args)
        self.native._run = broken
        with self.assertRaisesRegex(RuntimeError, 'Injected reclaim readback failure'):
            await self.native.suspend_programme_input()
        self.assertTrue(self.native.snapshot()['programme_input_suspended'])
        self.assertEqual(self.fixture.links, session_links)
        self.assertFalse(self.session.lock.locked())

    async def test_acknowledged_but_not_rendered_native_input_still_rejects_measurement(self):
        await self.session.start(48000)
        self.acknowledge_input = False
        with self.assertRaisesRegex(RuntimeError, 'not ready'):
            await self.native.enter_active_measurement()
        self.assertFalse(self.native._measurement_scope_lock.locked())
        self.assertFalse(self.native._measurement_latency_snapshot.get('ready', False))

    def transport(self, *, center_only=False):
        player = self.player
        player.state['audio_output_params'] = {'channel-count': 1 if center_only else 2}
        async def inactive():
            return {'status': 'Stopped'}
        async def ports(argument):
            if argument == '-l':
                return self.fixture.listing()
            self.assertEqual(argument, '-io')
            sources = ['mpv:output_FC'] if center_only else ['mpv:output_FL', 'mpv:output_FR']
            return '\n'.join(sources + [f'fxroute_dsp:input_{i}' for i in range(1, 9)])
        orchestration = PlaybackOrchestrator(replace(main._make_playback_orchestration_deps(),
            get_runtime_player=lambda: player, get_dsp_runtime=lambda: self.native,
            run_pw_link_command=ports))
        adapter = FxrouteTransitionRuntime(replace(main.make_playback_runtime_deps(),
            player=lambda: player, dsp_runtime=lambda: self.native, player_is_running=lambda: True,
            ensure_mpv_to_dsp_links=orchestration.ensure_mpv_to_dsp_links,
            get_spotify_ui_state=inactive, get_qobuz_ui_state=inactive,
            get_samplerate_status=lambda: {'active_rate': 48000, 'force_rate': 48000},
            playback_graph_diagnosis=self.fixture.orchestrator.playback_graph_diagnosis,
            log_playback_graph_diagnosis=lambda *args, **kwargs: None))
        return player, orchestration, adapter

    async def test_play_cannot_release_ingress_while_a_raw_scope_owns_it(self):
        await self.session.start(48000)
        _, _, adapter = self.transport()
        previous = await self.native.enter_raw_measurement()
        try:
            with self.assertRaisesRegex(RuntimeError, 'Measurement.*active'):
                await adapter.prepare_target_source(TransitionRequest(operation='play', source='radio'))
            self.assertTrue(self.native.snapshot()['programme_input_suspended'])
            self.assertFalse(set(MPV) & self.fixture.links)
        finally:
            await self.native.exit_raw_measurement(previous)

    async def test_idle_paused_output_mode_restore_keeps_measurement_isolation(self):
        await self.session.start(48000)
        player, _, adapter = self.transport()
        player.set_surround_channels = lambda enabled: True
        await adapter.restore_output_mode_transport(
            TransitionRequest(operation='output-mode-switch', source='radio'),
            {'player': dict(player.state)}, 'silent-mode-probe')
        self.assertTrue(player.state['paused'])
        self.assertTrue(self.native.snapshot()['programme_input_suspended'])
        self.assertFalse(set(MPV) & self.fixture.links)

    async def test_native_queue_selection_cannot_use_transport_only_resume_when_suspended(self):
        await self.session.start(48000)
        self.player._running = True
        self.player.get_property = lambda name: 2
        self.assertFalse(main._native_mpv_direct_selection_ready(['first', 'second']))

    def capture_services(self, intent, restores):
        self.player._running = True
        track = {'source': 'radio', 'url': 'retained', 'id': 'station', 'sample_rate_hz': 48000}
        async def matches(**kwargs):
            return kwargs.get('intent_generation') == intent[0]
        async def restore(**kwargs):
            restores.append(kwargs)
            _, _, adapter = self.transport()
            await adapter.prepare_target_source(TransitionRequest(
                operation='measurement-restore', source=kwargs['source'], target_rate=48000,
                target_url=kwargs['target_url'], should_play=False, reload_source=False))
            self.player.set_pause(not kwargs['should_play'])
            return SimpleNamespace(committed=True)
        async def sync(rate):
            return None
        return replace(measurement_session._services,
            get_current_track_info=lambda: track, get_playback_intent_generation=lambda: intent[0],
            measurement_restore_intent_matches_live_state=matches,
            get_playback_transition_coordinator=lambda: SimpleNamespace(restore_measurement=restore),
            get_dsp_orchestrator=lambda: SimpleNamespace(sync_runtime_at_rate=sync),
            begin_playback_transition_attempt=lambda: 0, end_playback_transition_attempt=lambda: None,
            post_release_input_resync=None, post_release_policy_rate_restore=None)

    async def test_reclaim_refreshes_playback_intent_and_close_restores_the_current_source(self):
        intent, restores = [0], []
        services = self.capture_services(intent, restores)
        with patch.object(measurement_session, '_services', services):
            await self.session.start(48000)
            self.assertEqual(measurement_session._playback_state_before_measurement['intent_generation'], 0)
            self.native.release_programme_input()
            await self.native.set_mapped_ingress(MPV)
            self.player.set_pause(False)
            intent[0] = 1
            await self.session.register_spl_job('reclaimed')
            self.assertEqual(measurement_session._playback_state_before_measurement['intent_generation'], 1)
            await self.session.unregister_spl_job('reclaimed')
            await self.session.request_close()
        self.assertEqual(len(restores), 1)
        self.assertFalse(self.player.state['paused'])
        self.assertTrue(set(MPV).issubset(self.fixture.links))
        self.assertFalse(self.native.snapshot()['programme_input_suspended'])

    async def test_stale_close_preserves_the_guarded_reconnect_requirement(self):
        intent, restores = [0], []
        services = self.capture_services(intent, restores)
        with patch.object(measurement_session, '_services', services):
            await self.session.start(48000)
            intent[0] = 1
            await self.session.request_close()
        self.assertEqual(restores, [])
        self.assertFalse(self.session.active)
        self.assertTrue(self.native.snapshot()['programme_input_suspended'])
        self.assertFalse(main._native_mpv_direct_selection_ready(['retained']))
        _, _, adapter = self.transport()
        await adapter.prepare_target_source(TransitionRequest(
            operation='resume', source='radio', target_rate=48000,
            target_url='retained', should_play=False, reload_source=False))
        self.assertTrue(set(MPV).issubset(self.fixture.links))
        self.assertFalse(self.native.snapshot()['programme_input_suspended'])

    async def test_guarded_paused_restore_uses_live_ports_not_saved_mpv_edges(self):
        await self.session.start(48000)
        player, orchestration, adapter = self.transport(center_only=True)
        self.assertFalse(await orchestration.ensure_mpv_to_dsp_links(timeout_ms=0))
        self.assertFalse(set(MPV) & self.fixture.links)
        await adapter.prepare_target_source(TransitionRequest(
            operation='measurement-restore', source='radio', target_url='retained',
            target_rate=48000, should_play=False, reload_source=False))
        self.assertEqual(self.native._mapped_ingress, (('mpv:output_FC', 'fxroute_dsp:input_3'),))
        self.assertFalse(set(MPV) & self.fixture.links)
        self.assertTrue(player.state['paused'])
        self.assertFalse(self.native.snapshot()['programme_input_suspended'])

    async def test_source_switch_from_paused_radio_does_not_restore_its_old_mapping(self):
        await self.session.start(48000)
        _, _, adapter = self.transport()
        await adapter.prepare_target_source(TransitionRequest(operation='spotify-play', source='spotify'))
        self.assertFalse(self.native.snapshot()['programme_input_suspended'])
        self.assertEqual(self.native._mapped_ingress, ())
        self.assertFalse(set(MPV) & self.fixture.links)

    async def test_paused_readback_fails_closed_then_resume_confirms_actual_source_src(self):
        with self.assertRaisesRegex(RuntimeError, 'not ready'):
            await self.native.settled_latency()
        self.assertEqual(self.input_commands, [])
        self.assertIsNone(self.native._last_input_latency_ms)
        self.state = 'active'
        value = await self.native.settled_latency()
        self.assertEqual(value['input'], 48000)  # 750 ms programme + 250 ms hardware.
        self.assertFalse(self.native._ingress_latency_pending)
        self.assertEqual(self.fixture.links, self.original_links)

    async def test_pause_stop_then_measurement_has_no_stale_mpv_latency(self):
        await self.native.set_mapped_ingress(())
        previous = await self.native.enter_active_measurement()
        try:
            self.assertEqual((await self.native.settled_latency())['input'], 12000)
        finally:
            await self.native.exit_active_measurement(previous)
        self.assertFalse(set(MPV) & self.fixture.links)

    async def test_unconfirmed_native_latency_still_rejects_entry_and_restores_graph(self):
        await self.session.start(48000)
        session_links = set(self.fixture.links)
        self.engine_ready = False
        with self.assertRaisesRegex(RuntimeError, 'not ready'):
            await self.native.enter_raw_measurement()
        self.assertEqual(self.fixture.links, session_links)
        self.assertFalse(self.native._effect_bypass)
        self.assertFalse(self.native._measurement_scope_lock.locked())

    async def test_unsettled_hardware_latency_still_rejects_entry(self):
        await self.session.start(48000)
        session_links = set(self.fixture.links)
        self.output_state = 'init'
        with self.assertRaisesRegex(RuntimeError, 'output path is settling'):
            await self.native.enter_active_measurement()
        self.assertEqual(self.fixture.links, session_links)
        self.assertEqual(self.input_commands, [])
        self.assertFalse(self.native._measurement_scope_lock.locked())


if __name__ == '__main__':
    unittest.main()
