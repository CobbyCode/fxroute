#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""The measurement scope note states which DSP processing a sweep includes."""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from measurement.constants import MEASUREMENT_SCOPE_NOTE  # noqa: E402


class MeasurementScopeNoteTests(unittest.TestCase):
    def test_note_does_not_claim_independence_from_the_active_processing(self):
        self.assertNotIn('independent of the active DSP preset', MEASUREMENT_SCOPE_NOTE)
        self.assertNotRegex(MEASUREMENT_SCOPE_NOTE, r'(?i)\bindependent\b')

    def test_note_names_the_processing_a_sweep_runs_through(self):
        for part in ('complete active chain', 'Global preset', 'output extras', 'crossover',
                     "the measured bank's preset", 'output trims'):
            self.assertIn(part, MEASUREMENT_SCOPE_NOTE)
        self.assertRegex(MEASUREMENT_SCOPE_NOTE,
                         r'Auto Sub Optimize and Speaker Align bypass the presets and output extras')

    def test_demo_serves_the_same_note(self):
        source = (ROOT / 'demo/routes.js').read_text(encoding='utf-8')
        match = re.search(r"const DEMO_MEASUREMENT_SCOPE_NOTE = '((?:[^'\\]|\\.)*)';", source)
        self.assertIsNotNone(match, 'demo scope note constant not found')
        self.assertEqual(match.group(1).replace("\\'", "'").replace('\\\\', '\\'), MEASUREMENT_SCOPE_NOTE)


if __name__ == '__main__':
    unittest.main()
