#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Fixed graph rates and paused external owners retain valid graph contracts."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import playback.orchestration as orchestration
from playback.runtime import FxrouteTransitionRuntime
from playback.transition import TransitionRequest
from test_post_start_graph_reconcile import _stereo_diagnosis_without_source_links


def _request(source, *, playing=False, target_rate=44100):
    return TransitionRequest(operation='sample-rate-policy', source=source,
        target_rate=target_rate, target_url='42',
        target_track={'id': '42', 'source': source, 'sample_rate_hz': 44100},
        should_play=playing, reload_source=True, rate_change=True,
        sample_rate_policy={'mode': 'auto', 'rate': None})


class ExternalRatePolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_qobuz_policy_context_uses_live_transport_not_stale_watcher_cache(self):
        orchestrator = orchestration.configured()
        for cached, actual, playing in (('Playing', 'Paused', False), ('Paused', 'Playing', True)):
            with self.subTest(cached=cached, actual=actual):
                playback = SimpleNamespace(current_playback_owner='qobuz', current_track_info=None,
                    latest_qobuz_state={'available': True, 'status': cached,
                                       'trackId': '41', 'sample_rate': 96000})
                live = {'available': True, 'status': actual, 'trackId': '42',
                        'sample_rate': 44100, 'title': 'Track', 'artist': 'Artist'}
                getter = AsyncMock(return_value=live)
                deps = SimpleNamespace(**{**vars(orchestrator._deps),
                    'get_playback_state': lambda: playback,
                    'get_runtime_player': lambda: None,
                    'get_spotify_ui_state': AsyncMock(return_value={'status': 'Stopped'}),
                    'is_spotify_playback_active': lambda state: False,
                    'is_local_playback_active': lambda state: False,
                    'get_qobuz_ui_state': getter})
                with patch.object(orchestrator, '_deps', deps), patch.object(
                        orchestrator, 'coordinator_target_rate', return_value=44100):
                    context = await orchestrator.current_playback_context()
                self.assertEqual(context['should_play'], playing)
                self.assertEqual(context['target_track']['id'], '42')
                self.assertEqual(context['target_track']['sample_rate_hz'], 44100)
                getter.assert_awaited_once()

    async def test_qobuz_start_restores_fixed_graph_rate_after_native_daemon_pin(self):
        physical = {'active_rate': 48000, 'force_rate': 48000}
        source = {'available': True, 'status': 'Playing', 'trackId': '42',
                  'position': 12, 'duration': 200, 'sample_rate': 44100}

        async def play():
            physical.update(active_rate=44100, force_rate=44100)
            return source

        async def force(rate, reason, **kwargs):
            physical.update(active_rate=rate, force_rate=rate)
            return True

        deps = replace(main.make_playback_runtime_deps(),
            player=lambda: SimpleNamespace(state={}), dsp_manager=lambda: None,
            qobuz_play=play, get_qobuz_ui_state=AsyncMock(return_value=source),
            qobuz_loaded_track_id=AsyncMock(return_value=42),
            get_samplerate_status=lambda: dict(physical),
            ensure_playback_samplerate_force=force,
            wait_for_qobuz_sink_input_samplerate=AsyncMock(return_value=44100),
            get_audio_output_overview=lambda: {'output_mode': {'mode': 'stereo'}},
            playback_graph_links_complete=AsyncMock(return_value=True))
        runtime = FxrouteTransitionRuntime(deps)
        request = replace(_request('qobuz', playing=True, target_rate=48000),
                          operation='qobuz-play', sample_rate_policy={'mode': 'fixed', 'rate': 48000})
        await runtime.start_target_source(request)
        self.assertEqual(physical, {'active_rate': 48000, 'force_rate': 48000})
        result = await runtime.verify_transition_graph(request)
        self.assertTrue(result['committed'])
        self.assertEqual(result['active_rate'], 48000)

    async def test_post_start_accepts_paused_external_owner_without_producer(self):
        orchestrator = orchestration.configured()

        async def diagnosis(overview, *, require_source=False, **kwargs):
            base = _stereo_diagnosis_without_source_links()
            if require_source:
                return {**base, 'source_links_complete': False, 'links_complete': False,
                        'signature': 'missing-producer'}
            return base

        with patch.object(orchestrator, '_deps', replace(orchestrator._deps,
                source_port_readiness_timeout_ms=0)), patch.object(orchestrator,
                'playback_graph_diagnosis', new=diagnosis):
            for source in ('spotify', 'qobuz'):
                with self.subTest(source=source):
                    result = await orchestrator.reconcile_post_start_graph(_request(source))
                    self.assertTrue(result['graph_complete'])

    async def test_commit_accepts_paused_external_owner_without_producer(self):
        async def links_complete(**kwargs):
            return not kwargs['require_source']

        deps = replace(main.make_playback_runtime_deps(),
            player=lambda: SimpleNamespace(state={}), dsp_manager=lambda: None,
            get_samplerate_status=lambda: {'active_rate': 44100, 'force_rate': 44100},
            get_audio_output_overview=lambda: {'output_mode': {'mode': 'stereo'}},
            get_spotify_ui_state=AsyncMock(return_value={'status': 'Paused'}),
            get_qobuz_ui_state=AsyncMock(return_value={'status': 'Paused'}),
            playback_graph_links_complete=links_complete)
        runtime = FxrouteTransitionRuntime(deps)
        for source in ('spotify', 'qobuz'):
            with self.subTest(source=source):
                result = await runtime.verify_transition_graph(_request(source))
                self.assertTrue(result['committed'])

    async def test_missing_producer_still_rejects_playing_external_and_loaded_native(self):
        orchestrator = orchestration.configured()

        async def diagnosis(overview, *, require_source=False, **kwargs):
            base = _stereo_diagnosis_without_source_links()
            if require_source:
                return {**base, 'source_links_complete': False, 'links_complete': False,
                        'signature': 'missing-producer'}
            return base

        with patch.object(orchestrator, '_deps', replace(orchestrator._deps,
                source_port_readiness_timeout_ms=0)), patch.object(orchestrator,
                'playback_graph_diagnosis', new=diagnosis):
            for source, playing in (('spotify', True), ('qobuz', True), ('local', False)):
                with self.subTest(source=source):
                    with self.assertRaises(RuntimeError):
                        await orchestrator.reconcile_post_start_graph(_request(source, playing=playing))

    async def test_same_rate_policy_commit_accepts_paused_external_owner_without_producer(self):
        async def diagnosis(overview, *, require_source=False, **kwargs):
            base = _stereo_diagnosis_without_source_links()
            if require_source:
                return {**base, 'source_links_complete': False, 'links_complete': False,
                        'signature': 'missing-producer'}
            return base

        deps = replace(main.make_playback_runtime_deps(),
            player=lambda: SimpleNamespace(state={}), dsp_manager=lambda: None,
            get_samplerate_status=lambda: {'active_rate': 44100, 'force_rate': 44100},
            get_spotify_ui_state=AsyncMock(return_value={'status': 'Paused'}),
            get_qobuz_ui_state=AsyncMock(return_value={'status': 'Paused'}),
            playback_graph_diagnosis=diagnosis)
        runtime = FxrouteTransitionRuntime(deps)
        for source in ('spotify', 'qobuz'):
            with self.subTest(source=source):
                request = replace(_request(source), reload_source=False, rate_change=False,
                    output_mode_target={'output_mode': {'mode': 'stereo'}})
                result = await runtime.verify_output_mode_runtime(request)
                self.assertTrue(result['committed'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
