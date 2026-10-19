#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Per-mode target API and deterministic live helper ingress latency."""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from audio.digital_input import DigitalInput
from audio.src_latency import SrcLatencyReader, graph_src_latency
from dsp.runtime import CommandResult, DSPRuntime, DSPRuntimeConfig
from test_av_sync_src import link, node
from test_output_state_api import audio_context, FakeRequest, make_manager, make_service


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_implicit_output_observation_follows_runtime_not_unrelated_default(self):
        native = SimpleNamespace(snapshot=lambda: {'config': {'output_key': 'selected-hardware'}})
        with patch.object(main.runtime, 'dsp_runtime', native), \
                patch.object(main.samplerate, '_load_audio_output_selection', return_value={'selected_key': None}):
            self.assertEqual(main._observed_output_key(), 'selected-hardware')

    async def test_migrated_selection_cannot_promote_unactivated_replacement_defaults(self):
        native = SimpleNamespace(snapshot=lambda: {'config': {'output_key': 'old-profile'}})
        with patch.object(main.runtime, 'dsp_runtime', native), \
                patch.object(main.samplerate, '_load_audio_output_selection', return_value={'selected_key': 'new-profile'}):
            self.assertEqual(main._observed_output_key(), 'old-profile')

    async def test_target_mutation_is_revision_checked_and_mode_specific(self):
        with tempfile.TemporaryDirectory() as directory:
            service = make_service(directory, make_manager(directory))
            with audio_context(service):
                await main.apply_audio_output_state(FakeRequest({'expected_revision': 0,
                    'mutation': {'kind': 'set_av_sync', 'mode': 'surround', 'target_ms': 350}}))
                catalog = await main.get_audio_output_state()
                self.assertEqual(catalog['modes']['surround']['av_sync']['target_ms'], 350)
                self.assertEqual(catalog['modes']['stereo']['av_sync']['target_ms'], 0)
                with self.assertRaises(main.HTTPException) as raised:
                    await main.apply_audio_output_state(FakeRequest({'expected_revision': 0,
                        'mutation': {'kind': 'set_av_sync', 'mode': 'surround', 'target_ms': 400}}))
                self.assertEqual(raised.exception.status_code, 409)

    async def test_target_api_rejects_forged_basis_and_nonfinite_values(self):
        with tempfile.TemporaryDirectory() as directory:
            service = make_service(directory, make_manager(directory))
            with audio_context(service):
                for payload in ({'target_ms': True}, {'target_ms': float('nan')},
                                {'target_ms': 100, 'basis': {'FL': 0}}):
                    with self.subTest(payload=payload), self.assertRaises(main.HTTPException) as raised:
                        await main.apply_audio_output_state(FakeRequest({'expected_revision': 0,
                            'mutation': {'kind': 'set_av_sync', 'mode': 'stereo', **payload}}))
                    self.assertEqual(raised.exception.status_code, 400)

    def test_live_helper_pcm_ac3_dts_have_the_same_electronic_offset(self):
        helper = DigitalInput()
        helper.process = SimpleNamespace(returncode=None, pid=123)
        for codec in ('pcm', 'ac3', 'dts'):
            helper._metadata = {'codec': codec, 'lookahead_frames': 2050}
            with self.subTest(codec=codec):
                self.assertAlmostEqual(helper.deterministic_latency_ms(), 2818 / 48)

    def test_input_credit_follows_the_routed_path_not_the_selected_source(self):
        helper = DigitalInput()
        helper.process = SimpleNamespace(returncode=None, pid=123)
        engine = SimpleNamespace(snapshot=lambda: {'surround_input': True})
        with patch.object(main.runtime, 'dsp_runtime', engine), patch.object(main, '_surround_input', helper):
            self.assertAlmostEqual(main._deterministic_input_latency_ms(), 2818 / 48)
            engine.snapshot = lambda: {'surround_input': False}
            self.assertEqual(main._deterministic_input_latency_ms(), 0)

    async def test_src_and_digital_helper_credits_are_added_without_replacing_each_other(self):
        helper = DigitalInput()
        helper.process = SimpleNamespace(returncode=None, pid=123)
        engine = SimpleNamespace(snapshot=lambda: {'active': True, 'helper_pid': 123, 'surround_input': True, 'config': {'sample_rate': 48000}})
        reader = SimpleNamespace(read=lambda **kwargs: 750)
        with patch.object(main.runtime, 'dsp_runtime', engine), patch.object(main, '_surround_input', helper), \
                patch.object(main, '_src_latency_reader', reader, create=True):
            provider = getattr(main, '_electronic_input_latency_ms', None)
            self.assertIsNotNone(provider, 'SRC credit is not wired into the native input latency provider')
            self.assertAlmostEqual(await provider(), 808.7083333333334)

    async def test_running_engine_without_links_cannot_certify_zero_src_credit(self):
        native = DSPRuntime(object())
        native._process = SimpleNamespace(returncode=None, pid=123)
        native._config = DSPRuntimeConfig('stereo', 'test', 48000, ('FL', 'FR'),
                                          ({'name': 'main_l'}, {'name': 'main_r'}))
        reader = SimpleNamespace(read=lambda owner=None, **options: graph_src_latency([node(3, 'fxroute_dsp')], **options))
        with patch.object(main.runtime, 'dsp_runtime', native), \
                patch.object(main, '_src_latency_reader', reader), \
                patch.object(main, '_measurement_session_owns_live_rate', return_value=False):
            self.assertFalse(native.snapshot()['active'])
            with self.assertRaisesRegex(RuntimeError, 'path|unavailable'):
                await main._electronic_input_latency_ms()

    async def test_graph_reconciliation_invalidates_src_cache_for_the_same_engine_and_plan(self):
        from test_av_sync_src import graph
        reader = SrcLatencyReader()
        snapshot = {'helper_pid': 123, 'graph_revision': 1,
                    'config': {'output_key': 'hardware', 'plan_fingerprint': 'same'}}
        native = SimpleNamespace(snapshot=lambda: snapshot)
        first, second = graph(), graph()
        second[3] = node(4, 'hardware', 'Audio/Sink', 250000000)
        with patch.object(main.runtime, 'dsp_runtime', native), \
                patch.object(main, '_src_latency_reader', reader), \
                patch.object(reader, '_dump', side_effect=[first, second]), \
                patch.object(main, '_measurement_session_owns_live_rate', return_value=False):
            self.assertEqual(await main._electronic_input_latency_ms(), 750)
            snapshot['graph_revision'] = 2
            self.assertEqual(await main._electronic_input_latency_ms(), 1000)

    async def test_reconciled_helper_ingress_survives_a_background_latency_failure(self):
        links = set()
        async def run(args):
            if args == ('pw-link', '-l'):
                return CommandResult(0, '\n'.join(f'{a} -> {b}' for a, b in links))
            links.add(tuple(args[1:]))
            return CommandResult(0)
        def dump():
            value = [node(3, 'fxroute_dsp'), node(4, 'hardware', 'Audio/Sink'),
                     node(5, 'fxroute_surround_input'), link(3, 4)]
            if ('fxroute_surround_input:output_stereo_FL', 'fxroute_dsp:input_1') in links:
                value.append(link(5, 3))
            return value
        helper = DigitalInput()
        helper.process = SimpleNamespace(returncode=None, pid=456)
        native = DSPRuntime(object(), command_runner=run)
        native._process = SimpleNamespace(returncode=None, pid=123)
        native._config = DSPRuntimeConfig('stereo', 'test', 48000, (), ())
        native._surround_input = True
        native.configure_latency_tracking(input_latency_provider=main._electronic_input_latency_ms)
        reader = SrcLatencyReader()
        with patch.object(main.runtime, 'dsp_runtime', native), patch.object(main, '_surround_input', helper), \
                patch.object(main, '_src_latency_reader', reader), patch.object(reader, '_dump', side_effect=dump), \
                patch.object(main, '_measurement_session_owns_live_rate', return_value=False), \
                patch('audio.src_latency.time', SimpleNamespace(monotonic=lambda: 0)), \
                patch.object(native, '_control_unlocked', return_value='OK'):
            with self.assertRaisesRegex(RuntimeError, 'ingress path is unavailable'):
                await native.update_input_latency()
            await native.set_surround_input(True)
            self.assertEqual({edge for edge in links if edge[0].startswith('fxroute_surround_input:')}, {
                ('fxroute_surround_input:output_stereo_FL', 'fxroute_dsp:input_1'),
                ('fxroute_surround_input:output_stereo_FR', 'fxroute_dsp:input_2')})
            self.assertAlmostEqual(native._last_input_latency_ms, 2818 / 48)

    async def test_stopped_engine_with_retained_config_can_bootstrap_without_graph(self):
        native = DSPRuntime(object())
        native._config = DSPRuntimeConfig('stereo', 'test', 48000, ('FL', 'FR'),
                                          ({'name': 'main_l'}, {'name': 'main_r'}))
        reader = SimpleNamespace(read=lambda owner=None, **options: graph_src_latency([], **options))
        with patch.object(main.runtime, 'dsp_runtime', native), patch.object(main, '_src_latency_reader', reader):
            self.assertEqual(await main._electronic_input_latency_ms(), 0)

    def test_helper_snapshot_exposes_worker_reserve_and_losses(self):
        helper = DigitalInput()
        helper.process = SimpleNamespace(returncode=None, pid=123)
        metrics = {'worker_margin_frames': 1024, 'worker_quantum_frames': 1024,
                   'worker_delay_max_us': 3900, 'worker_reserve_min_us': 17433,
                   'input_queue_peak_frames': 1024, 'late_frames': 7,
                   'underrun_frames': 7, 'queued_muted_frames': 351, 'transport_faults': 0}
        with tempfile.TemporaryDirectory() as directory:
            helper._status_path = Path(directory) / 'status.json'
            helper._status_path.write_text(json.dumps({**metrics, 'codec': 'pcm',
                'lookahead_frames': 2050, 'live_latency_frames': 2818, 'untrusted_field': 'ignored'}))
            status = helper.snapshot()
            for key, value in metrics.items():
                self.assertEqual(status.get(key), value, key)
            self.assertEqual(status['live_latency_frames'], 2818)
            self.assertNotIn('untrusted_field', status)


if __name__ == '__main__':
    unittest.main()
