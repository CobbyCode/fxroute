#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""SRC credit follows linked adapters, not propagated port latency or policy."""
import importlib
from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    src = importlib.import_module('audio.src_latency')
except ModuleNotFoundError as exc:
    if exc.name != 'audio.src_latency':
        raise
    src = None


def node(index, name, media_class='', latency=None, bypass=False, state='running'):
    params = {} if latency is None else {'fxroute.src.mode': 'high', 'fxroute.src.nominal-bypass': bypass,
        'fxroute.src.error': 0, 'fxroute.src.latency-ns': latency}
    return {'id': index, 'type': 'PipeWire:Interface:Node', 'info': {'state': state,
        'props': {'node.name': name, 'media.class': media_class}, 'params': {'Props': [{'params': params}]}}}


def link(source, target, state='active'):
    return {'type': 'PipeWire:Interface:Link', 'info': {'state': state,
        'output-node-id': source, 'input-node-id': target}}


def graph():
    return [node(1, 'mpv', 'Stream/Output/Audio', 750000000),
            node(2, 'fxroute_dsp_sink', 'Audio/Sink'), node(3, 'fxroute_dsp'),
            node(4, 'hardware', 'Audio/Sink', 0, bypass=True),
            link(1, 2), link(1, 2), link(2, 3), link(2, 3), link(3, 4), link(3, 4)]


class SrcLatencyTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(src, 'Linked SRC latency accounting is not implemented')

    def test_stereo_channels_do_not_double_count_shared_adapter(self):
        self.assertEqual(src.graph_src_latency(graph()), 750)

    def test_equal_rate_bypass_has_no_quality_dependent_credit(self):
        value = graph()
        value[0] = node(1, 'mpv', 'Stream/Output/Audio', 0, bypass=True)
        self.assertEqual(src.graph_src_latency(value), 0)

    def test_paused_connected_player_keeps_its_resume_latency(self):
        value = graph()
        value[0]['info']['state'] = 'idle'
        for row in value[4:6]:
            row['info']['state'] = 'paused'
        self.assertEqual(src.graph_src_latency(value), 750)

    def test_unrelated_converter_and_recording_tap_are_not_counted(self):
        value = graph() + [node(5, 'other-player', 'Stream/Output/Audio', 900000000),
            node(6, 'recorder', 'Stream/Input/Audio', 750000000), link(3, 6)]
        self.assertEqual(src.graph_src_latency(value), 750)

    def test_serial_capture_and_playback_adapters_are_added_once_each(self):
        value = graph()
        value[0] = node(1, 'capture', 'Audio/Source', 810000000)
        value[3] = node(4, 'hardware', 'Audio/Sink', 750000000)
        self.assertEqual(src.graph_src_latency(value), 1560)

    def test_measurement_excludes_paused_program_ingress_but_not_hardware_src(self):
        value = graph()
        value[3] = node(4, 'hardware', 'Audio/Sink', 750000000)
        self.assertEqual(src.graph_src_latency(value, include_ingress=False), 750)

    def test_propagated_port_latency_is_not_an_additional_converter(self):
        value = graph()
        value[1]['info']['params']['Latency'] = [{'maxNs': 750000000}]
        value.append({'id': 10, 'type': 'PipeWire:Interface:Port',
            'info': {'props': {'node.id': 3}, 'params': {'Latency': [{'maxNs': 750000000}]}}})
        self.assertEqual(src.graph_src_latency(value), 750)

    def test_startup_extension_is_used_instead_of_hardcoded_minimum(self):
        value = graph()
        value[0] = node(1, 'mpv', 'Stream/Output/Audio', 1056000000)
        self.assertEqual(src.graph_src_latency(value), 1056)

    def test_unequal_simultaneous_ingress_cannot_claim_one_precise_total(self):
        value = graph() + [node(5, 'capture', 'Audio/Source', 0, bypass=True), link(5, 3)]
        with self.assertRaisesRegex(RuntimeError, 'different'):
            src.graph_src_latency(value)

    def test_external_owner_ignores_the_preserved_paused_app_monitor(self):
        value = graph() + [node(5, 'digital-helper'), link(5, 3)]
        self.assertEqual(src.graph_src_latency(value, ingress_nodes=('digital-helper',)), 0)

    def test_missing_selected_ingress_or_hardware_path_is_unavailable(self):
        for value, kwargs in ((graph(), {'ingress_nodes': ('missing-helper',)}),
                              (graph()[:8], {}), ([node(3, 'fxroute_dsp')], {})):
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, 'path|ingress|output'):
                src.graph_src_latency(value, **kwargs)

    def test_failed_or_missing_converter_evidence_is_not_guessed_zero(self):
        for key, val in (('fxroute.src.error', 32), ('fxroute.src.latency-ns', None),
                         ('fxroute.src.latency-ns', -1), ('fxroute.src.latency-ns', True)):
            value = graph()
            value[0]['info']['params']['Props'][0]['params'][key] = val
            with self.subTest(key=key, value=val), self.assertRaises(RuntimeError):
                src.graph_src_latency(value)

    def test_reader_uses_fresh_graph_after_cache_expires_and_never_hides_failure(self):
        equal = graph(); equal[0] = node(1, 'mpv', 'Stream/Output/Audio', 0, bypass=True)
        outputs = [subprocess.CompletedProcess([], 0, json.dumps(graph()), ''),
                   subprocess.CompletedProcess([], 0, json.dumps(equal), '')]
        with patch.object(src.subprocess, 'run', side_effect=outputs), patch.object(src.time, 'monotonic', side_effect=[0, .1, .6]):
            reader = src.SrcLatencyReader()
            self.assertEqual(reader.read(), 750)
            self.assertEqual(reader.read(), 750)
            self.assertEqual(reader.read(), 0)
        with patch.object(src.subprocess, 'run', side_effect=subprocess.TimeoutExpired('pw-dump', 5)), \
                patch.object(src.time, 'monotonic', return_value=1.2), self.assertRaises(RuntimeError):
            reader.read()

    def test_new_ingress_selection_does_not_reuse_a_pre_link_graph(self):
        app_graph = graph()
        external_graph = graph() + [node(5, 'digital-helper'), link(5, 3)]
        outputs = [subprocess.CompletedProcess([], 0, json.dumps(app_graph), ''),
                   subprocess.CompletedProcess([], 0, json.dumps(external_graph), '')]
        with patch.object(src.subprocess, 'run', side_effect=outputs), \
                patch.object(src.time, 'monotonic', side_effect=[0, .1]):
            reader = src.SrcLatencyReader()
            self.assertEqual(reader.read(ingress_nodes=('fxroute_dsp_sink',)), 750)
            self.assertEqual(reader.read(ingress_nodes=('digital-helper',)), 0)

    def test_replacement_rendering_gets_fresh_graph_even_within_cache_window(self):
        bypass = graph(); bypass[0] = node(1, 'mpv', 'Stream/Output/Audio', 0, bypass=True)
        outputs = [subprocess.CompletedProcess([], 0, json.dumps(graph()), ''),
                   subprocess.CompletedProcess([], 0, json.dumps(bypass), '')]
        with patch.object(src.subprocess, 'run', side_effect=outputs), \
                patch.object(src.time, 'monotonic', side_effect=[0, .1]):
            reader = src.SrcLatencyReader()
            self.assertEqual(reader.read(owner=(123, 'conversion')), 750)
            self.assertEqual(reader.read(owner=(123, 'bypass')), 0)

    def test_overlapping_reads_cannot_pair_old_graph_with_new_owner_metadata(self):
        captured, resume = threading.Event(), threading.Event()
        class PausedCacheReader(src.SrcLatencyReader):
            def __getattribute__(self, name):
                value = super().__getattribute__(name)
                if (name in {'_graph', '_cache'} and value is not None
                        and threading.current_thread().name.startswith('paused-cache-read')):
                    captured.set()
                    if not resume.wait(5):
                        raise RuntimeError('Concurrent cache test did not resume')
                return value
        bypass = graph(); bypass[0] = node(1, 'mpv', 'Stream/Output/Audio', 0, bypass=True)
        outputs = [subprocess.CompletedProcess([], 0, json.dumps(graph()), '')] + [
            subprocess.CompletedProcess([], 0, json.dumps(bypass), '') for _ in range(2)]
        with patch.object(src.subprocess, 'run', side_effect=outputs), \
                patch.object(src.time, 'monotonic', return_value=0), \
                ThreadPoolExecutor(max_workers=1, thread_name_prefix='paused-cache-read') as pool:
            reader = PausedCacheReader()
            self.assertEqual(reader.read(owner='previous'), 750)
            pending = pool.submit(reader.read, owner='current')
            try:
                self.assertTrue(captured.wait(5), 'Concurrent reader did not capture the old cache')
                self.assertEqual(reader.read(owner='current'), 0)
            finally:
                resume.set()
            self.assertEqual(pending.result(timeout=5), 0)


if __name__ == '__main__':
    unittest.main()
