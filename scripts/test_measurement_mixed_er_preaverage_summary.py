#!/usr/bin/env python3
"""L/R repeat paired summary must not drop sweeps when one side was ER pre-averaged.

ER pre-averaging collapses a side to a single effective capture.  When only one
side pre-averages, the summarizer used to pair the collapsed side against the
first per-sweep capture only, silently dropping the remaining runs from the
magnitude average and the timing/delta computation.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from measurement.store import MeasurementStore


def _payload(measurement_id: str, channel: str, timing_ms: float, level_db: float, *, electrical: bool) -> dict:
    sample_rate = 48_000
    arrival_samples = round(timing_ms / 1000 * sample_rate)
    return {
        "id": measurement_id,
        "name": measurement_id,
        "channel": channel,
        "input_device": {"id": "test-input", "label": "Test input"},
        "input_channels": {"mic": 1, "electrical_reference": 2 if electrical else None},
        "traces": [{
            "kind": "sweep-response",
            "label": measurement_id,
            "role": "trusted",
            "points": [[20, level_db], [1000, level_db + 1], [20000, level_db + 2]],
        }],
        "review_traces": [{
            "kind": "sweep-response-review",
            "label": f"{measurement_id} review",
            "role": "raw-review",
            "points": [[10, level_db - 1], [1000, level_db + 1], [22000, level_db + 3]],
        }],
        "analysis": {
            "sample_rate": sample_rate,
            "reference_path": {
                "electrical_reference_used": electrical,
                "electrical_reference_input_channel": 2 if electrical else None,
                "capture_mode": "electrical-input" if electrical else "dual-channel",
                "acoustic_arrival_corrected_ms": timing_ms,
            },
            "impulse_response": {
                "arrival_ms": timing_ms,
                "arrival_seconds": timing_ms / 1000,
                "arrival_samples": arrival_samples,
                "direct_arrival_index": arrival_samples + 10,
                "reference_peak_index": 10,
            },
        },
    }


class MixedErPreaverageSummaryTests(unittest.TestCase):
    def _store(self, tempdir: str) -> MeasurementStore:
        return MeasurementStore(home=Path(tempdir))

    def test_single_preaveraged_side_pairs_against_all_per_sweep_runs(self):
        with tempfile.TemporaryDirectory() as tempdir, mock.patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            # Left collapsed to one ER pre-averaged effective capture; right
            # still holds three per-sweep captures with tightly clustered timing.
            left = [_payload("left-effective", "left", 1.00, 0.0, electrical=True)]
            right = [
                _payload("right-1", "right", 1.05, 1.0, electrical=True),
                _payload("right-2", "right", 1.10, 2.0, electrical=True),
                _payload("right-3", "right", 1.15, 3.0, electrical=True),
            ]

            l_summary, r_summary = store._repeat_runner.summarize_lr_repeat_paired(
                left, right, base_name="Sofa center", repeat_count=3
            )

            # The per-sweep right side must contribute all three runs.
            r_repeat = r_summary["analysis"]["lr_repeat"]
            self.assertEqual(r_repeat["accepted_runs"], 3)
            self.assertEqual(r_repeat["rejected_runs"], 0)
            self.assertEqual(r_repeat["accepted_run_numbers"], [1, 2, 3])
            self.assertEqual(r_summary["analysis"]["reference_path"]["acoustic_arrival_corrected_ms"], 1.10)
            # Magnitude average over right runs 1..3 (levels 1, 2, 3 dB).
            self.assertEqual(
                r_summary["traces"][0]["points"],
                [[20.0, 2.0], [1000.0, 3.0], [20000.0, 4.0]],
            )

            # The pre-averaged left side contributes its single effective run.
            l_repeat = l_summary["analysis"]["lr_repeat"]
            self.assertEqual(l_repeat["accepted_runs"], 1)
            self.assertEqual(l_repeat["rejected_runs"], 0)
            self.assertEqual(l_repeat["accepted_run_numbers"], [1])
            self.assertEqual(l_summary["analysis"]["reference_path"]["acoustic_arrival_corrected_ms"], 1.00)
            self.assertEqual(
                l_summary["traces"][0]["points"],
                [[20.0, 0.0], [1000.0, 1.0], [20000.0, 2.0]],
            )

            # Delta center = median of (right_i - left) over all three runs.
            self.assertEqual(l_repeat["delta_center_ms"], 0.1)
            self.assertEqual(r_repeat["delta_center_ms"], 0.1)
            self.assertEqual(r_repeat["delta_spread_ms"], 0.1)
            self.assertTrue(r_repeat["timing_stable"])
            self.assertEqual(r_summary["analysis"]["reference_path"]["timing_status"], "lr-repeat")

    def test_balanced_per_sweep_sides_still_reject_outlier_pairs(self):
        with tempfile.TemporaryDirectory() as tempdir, mock.patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            left = [
                _payload("left-1", "left", 1.00, 0.0, electrical=True),
                _payload("left-2", "left", 1.10, 2.0, electrical=True),
                _payload("left-outlier", "left", 3.00, 20.0, electrical=True),
            ]
            right = [
                _payload("right-1", "right", 1.05, 1.0, electrical=True),
                _payload("right-2", "right", 1.15, 3.0, electrical=True),
                _payload("right-outlier", "right", 1.10, 4.0, electrical=True),
            ]

            l_summary, r_summary = store._repeat_runner.summarize_lr_repeat_paired(
                left, right, base_name="Sofa center", repeat_count=3
            )

            # Pair 3 (left timing 3.00) is the outlier; both sides drop it.
            for summary in (l_summary, r_summary):
                repeat = summary["analysis"]["lr_repeat"]
                self.assertEqual(repeat["accepted_runs"], 2)
                self.assertEqual(repeat["rejected_runs"], 1)
                self.assertEqual(repeat["accepted_run_numbers"], [1, 2])
                self.assertEqual(repeat["delta_center_ms"], 0.05)
                self.assertEqual(repeat["delta_spread_ms"], 0.0)

            self.assertEqual(
                l_summary["traces"][0]["points"],
                [[20.0, 1.0], [1000.0, 2.0], [20000.0, 3.0]],
            )
            self.assertEqual(
                r_summary["traces"][0]["points"],
                [[20.0, 2.0], [1000.0, 3.0], [20000.0, 4.0]],
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
