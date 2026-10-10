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
from test_output_state_api import audio_context, FakeRequest, make_manager, make_service


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_target_mutation_is_revision_checked_and_mode_specific(self):
        with tempfile.TemporaryDirectory(dir='/tmp/opencode') as directory:
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
        with tempfile.TemporaryDirectory(dir='/tmp/opencode') as directory:
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

    def test_helper_snapshot_exposes_worker_reserve_and_losses(self):
        helper = DigitalInput()
        helper.process = SimpleNamespace(returncode=None, pid=123)
        metrics = {'worker_margin_frames': 1024, 'worker_quantum_frames': 1024,
                   'worker_delay_max_us': 3900, 'worker_reserve_min_us': 17433,
                   'input_queue_peak_frames': 1024, 'late_frames': 7,
                   'underrun_frames': 7, 'queued_muted_frames': 351, 'transport_faults': 0}
        with tempfile.TemporaryDirectory(dir='/tmp/opencode') as directory:
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
