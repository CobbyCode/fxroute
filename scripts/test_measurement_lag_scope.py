#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Lag-continuity scoping for measurement alignment.

A successfully determined lag/align value is only a tie-break within the
measurement run that determined it. An independent later run or a competing
reference candidate must never blindly adopt a stale lag.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from measurement.host_capture import HostCaptureRunner
from measurement.store import MeasurementStore


def _ambiguous_anchor_peaks():
    """Two lag clusters whose base scores differ by less than the bonus.

    Cluster A (lag 1000, mean 0.90) wins on its own evidence; cluster B
    (lag 5000, mean 0.88) only wins with a stale continuity bonus.
    """
    anchors = []
    for name, offset in (("start", 0), ("mid", 48000), ("end", 96000)):
        anchors.append({
            "name": name,
            "offset_samples": offset,
            "expected_start": offset,
            "search_start": 0,
            "top_peaks": [
                {"index": offset + 1000, "score": 0.90, "raw_score": 0.90, "polarity": 1.0},
                {"index": offset + 5000, "score": 0.88, "raw_score": 0.88, "polarity": 1.0},
                {"index": offset + 20000, "score": 0.30, "raw_score": 0.30, "polarity": 1.0},
                {"index": offset - 15000, "score": 0.25, "raw_score": 0.25, "polarity": 1.0},
                {"index": offset + 40000, "score": 0.20, "raw_score": 0.20, "polarity": 1.0},
            ],
        })
    return anchors


class LagScopeRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_run_discards_previous_run_lag(self):
        with tempfile.TemporaryDirectory(prefix="lag-scope-") as home:
            store = MeasurementStore(home=Path(home))
            store._last_successful_lag = 5000  # determined by previous run A
            job = {"id": "measurement-job-lagscope", "status": "queued",
                   "measurement_scope": "active_chain"}
            store._register_measurement_job(job, lambda current: {"message": "done"})
            # Independent run B starts: A's lag must be gone before any analysis.
            self.assertIsNone(store._last_successful_lag)
            await store._job_runner.tasks[job["id"]]
            self.assertEqual(store.get_job(job["id"])["status"], "completed")
            self.assertIsNone(store._last_successful_lag)


class ReferenceCandidateLagIsolationTests(unittest.TestCase):
    def test_candidates_start_unverified_and_winner_continues_scope(self):
        with tempfile.TemporaryDirectory(prefix="lag-candidate-") as home:
            store = MeasurementStore(home=Path(home))
            store._last_successful_lag = 111  # run scope lag from an earlier sweep
            observed = []

            def analyze(index, label):
                observed.append(store._last_successful_lag)
                store._last_successful_lag = 5000 + index
                return {"channel_index": index, "clock": {"selected_lag": 5000 + index}}

            def select(candidates):
                usable = [item for item in candidates if isinstance(item.get("analysis"), dict)]
                return max(usable, key=lambda item: item["analysis"]["clock"]["selected_lag"])

            chosen = HostCaptureRunner._select_reference_candidate_isolated(
                store, analyze, [0, 1], select)
            self.assertEqual(chosen["channel_index"], 1)
            # No candidate saw the run lag or another candidate's lag.
            self.assertEqual(observed, [None, None])
            # Only the winner's lag continues the run's scope.
            self.assertEqual(store._last_successful_lag, 5001)

    def test_failed_selection_restores_run_lag(self):
        with tempfile.TemporaryDirectory(prefix="lag-candidate-fail-") as home:
            store = MeasurementStore(home=Path(home))
            store._last_successful_lag = 111

            def analyze(index, label):
                raise RuntimeError("candidate carries no sweep")

            with self.assertRaises(RuntimeError):
                HostCaptureRunner._select_reference_candidate_isolated(
                    store, analyze, [0, 1], lambda candidates: candidates[0])
            self.assertEqual(store._last_successful_lag, 111)


class LagContinuityWithinScopeTests(unittest.TestCase):
    def test_explicit_previous_lag_still_breaks_ties_within_scope(self):
        # The continuity mechanism itself is kept: within one scope an
        # explicitly passed previous lag still decides ambiguous layouts.
        with tempfile.TemporaryDirectory(prefix="lag-continuity-") as home:
            store = MeasurementStore(home=Path(home))
            analyzer = store._analyzer
            lag_none, _, _ = analyzer._select_global_lag_from_peaks(
                _ambiguous_anchor_peaks(), cluster_threshold=480,
                previous_successful_lag=None)
            self.assertEqual(lag_none, 1000)
            lag_prev, _, _ = analyzer._select_global_lag_from_peaks(
                _ambiguous_anchor_peaks(), cluster_threshold=480,
                previous_successful_lag=5000)
            self.assertEqual(lag_prev, 5000)


if __name__ == "__main__":
    unittest.main()
