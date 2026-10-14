#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Signal-driven ingress and decoded routing across output modes."""
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.external_input import ExternalInputRouting, ExternalInputRoutingDependencies
from dsp.runtime import CommandResult, DSPRuntime, DSPRuntimeConfig, _contains_link
from playback.orchestration import PlaybackOrchestrator


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_unlabelled_pair_uses_signal_detection_in_every_mode(self):
        overview = {'mode': 'external-input', 'selected_input': {
            'key': 'capture::pair:3-4', 'source_key': 'capture',
            'left_channel': 'AUX2', 'right_channel': 'AUX3'}}
        for mode in ('stereo', 'stereo-sub', 'surround'):
            with self.subTest(mode=mode):
                routing = ExternalInputRouting(ExternalInputRoutingDependencies(
                    get_audio_source_overview=lambda: overview, get_output_mode=lambda: mode,
                    sync_digital_input=AsyncMock(), stop_digital_input=AsyncMock()))
                with patch('audio.pw_link.connect_ports', side_effect=AssertionError('raw carrier bypass')):
                    await routing.sync()
                self.assertEqual(routing.digital_selection_key, 'capture::pair:3-4')
                self.assertEqual(routing.digital_source_name, 'capture')
                self.assertIsNone(routing.loopback_source_name)

    async def test_split_capture_pair_uses_signal_detection_without_mono_duplication(self):
        calls = []
        async def sync(source, left, right, card, right_source=None):
            calls.append((source, left, right, card, right_source))
        overview = {'mode': 'external-input', 'selected_input': {
            'key': 'left', 'source_key': 'left', 'right_source': 'right',
            'left_channel': 'MONO', 'right_channel': 'MONO'}}
        for mode in ('stereo', 'stereo-sub', 'surround'):
            routing = ExternalInputRouting(ExternalInputRoutingDependencies(
                get_audio_source_overview=lambda: overview, get_output_mode=lambda: mode,
                sync_digital_input=sync, stop_digital_input=AsyncMock()))
            with patch('audio.pw_link.connect_ports', side_effect=AssertionError('raw carrier bypass')):
                await routing.sync()
            self.assertEqual(routing.active_right_source, 'right')
        self.assertEqual(calls, [('left', 'MONO', 'MONO', None, 'right')] * 3)


class GraphTests(unittest.IsolatedAsyncioTestCase):
    async def test_stereo_modes_link_downmix_and_surround_links_discrete_positions(self):
        for mode in ('stereo', 'stereo-sub', 'surround'):
            with self.subTest(mode=mode):
                links = set()
                async def run(args):
                    if args == ('pw-link', '-l'):
                        return CommandResult(0, '\n'.join(f'{a} -> {b}' for a, b in links))
                    if args[1] == '-d':
                        links.discard(tuple(args[2:]))
                    else:
                        links.add(tuple(args[1:]))
                    return CommandResult(0)
                native = DSPRuntime(SimpleNamespace(), command_runner=run)
                native._process = SimpleNamespace(returncode=None)
                native._config = DSPRuntimeConfig(mode, 'A', 48000, (), ())
                await native.set_surround_input(True)
                ports = ('FL', 'FR', 'FC', 'LFE', 'SL', 'SR', 'BL', 'BR') if mode == 'surround' else ('stereo_FL', 'stereo_FR')
                expected = {(f'fxroute_surround_input:output_{port}', f'fxroute_dsp:input_{i + 1}')
                            for i, port in enumerate(ports)}
                self.assertEqual({edge for edge in links if edge[0].startswith('fxroute_surround_input:')}, expected)
                await native.set_surround_input(False)
                self.assertFalse(any(a.startswith('fxroute_surround_input:') for a, _ in links))

    async def test_diagnosis_requires_the_downmix_links_in_stereo_modes(self):
        for mode in ('stereo', 'stereo-sub'):
            with self.subTest(mode=mode):
                links = [('fxroute_dsp_sink:monitor_FL', 'fxroute_dsp:input_1'),
                         ('fxroute_dsp_sink:monitor_FR', 'fxroute_dsp:input_2'),
                         ('fxroute_surround_input:output_stereo_FL', 'fxroute_dsp:input_1'),
                         ('fxroute_surround_input:output_stereo_FR', 'fxroute_dsp:input_2'),
                         ('fxroute_dsp:output_1', 'alsa_output.A:playback_FL'),
                         ('fxroute_dsp:output_2', 'alsa_output.A:playback_FR')]
                async def read(*args):
                    return '\n'.join(f'{a} -> {b}' for a, b in links)
                overview = {'output_mode': {'mode': 'stereo', 'effective_output_key': 'alsa_output.A',
                            'hardware_playback_ports': ['playback_FL', 'playback_FR']}}
                deps = SimpleNamespace(run_pw_link_command=read, output_mode_subwoofer_modes=frozenset(),
                    get_dsp_snapshot=lambda: {'active': True, 'surround_input': True,
                        'config': {'sample_rate': 48000, 'output_mode': mode,
                                   'output_routes': [[1, 'playback_FL'], [2, 'playback_FR']]}},
                    contains_link=_contains_link, helper_argument_sample_rate=lambda snapshot: 48000)
                orchestrator = PlaybackOrchestrator(deps)
                self.assertTrue((await orchestrator.playback_graph_diagnosis(overview, target_rate=48000))['links_complete'])
                links.remove(('fxroute_surround_input:output_stereo_FR', 'fxroute_dsp:input_2'))
                self.assertFalse((await orchestrator.playback_graph_diagnosis(overview, target_rate=48000))['links_complete'])


if __name__ == '__main__':
    unittest.main()
