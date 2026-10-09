#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""A successful worker is not a successful capture when its timing drifted."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_measurement_job_lifecycle import _make_store


class ValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_store_validates_before_marking_worker_result_completed(self):
        with tempfile.TemporaryDirectory(dir='/tmp/opencode') as directory, patch.dict(
                'os.environ', {'XDG_CONFIG_HOME': directory, 'XDG_STATE_HOME': directory}):
            store = _make_store(directory)
            async def enter():
                return False
            async def exit_scope(previous):
                pass
            async def validate(job):
                raise RuntimeError('DSP/input latency changed during measurement')
            store.active_scope_enter = enter
            store.active_scope_exit = exit_scope
            store.latency_capture_finish = validate
            job = await store.start_measurement(input_id='mic', channel='left')
            await store._job_tasks[job['id']]
            result = store.get_job(job['id'])
            self.assertEqual(result['status'], 'failed')
            self.assertIn('latency changed', result['error']['detail'])
            self.assertIsNone(result['result'])


if __name__ == '__main__':
    unittest.main()
