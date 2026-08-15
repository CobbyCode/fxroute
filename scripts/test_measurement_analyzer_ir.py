#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from measurement.analyzer import MeasurementAnalyzer


class MeasurementAnalyzerIrTests(unittest.TestCase):
    def test_direct_arrival_promotes_stronger_candidate_by_energy(self):
        impulse = np.zeros(6000, dtype=np.float64)
        impulse[4300] = 0.06
        impulse[4495:4506] = [0.055, 0.057, 0.059, 0.060, 0.061, 0.062, 0.061, 0.060, 0.059, 0.057, 0.055]
        impulse[5000] = 1.0
        reference = np.zeros_like(impulse)
        reference[5000] = 1.0

        analyzer = MeasurementAnalyzer(None, None)
        result = analyzer._estimate_impulse_direct_arrival(impulse, reference, 48_000)

        self.assertEqual(result["direct_arrival_index"], 4500)
        self.assertEqual(result["selection_rule"], "skipped_weak_threshold_edge_for_stronger_impulse_region")
        self.assertTrue(result["promotion_applied"])


if __name__ == "__main__":
    unittest.main()
