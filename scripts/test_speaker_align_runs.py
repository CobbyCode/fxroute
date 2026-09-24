#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Saveable Speaker Align runs: time-domain Before/After without re-measuring."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from measurement.speaker_runs import (  # noqa: E402
    SPEAKER_ALIGN_RUN_KIND,
    SPEAKER_ALIGN_RUN_SCHEMA,
    build_speaker_align_run,
    measurement_to_run,
    run_to_measurement,
    time_domain_view,
    validate_speaker_align_run,
)
from measurement.store import MeasurementStore  # noqa: E402


def proposal(**overrides):
    base = {
        "start_revision": 7,
        "processing_fingerprint": "frozen-plan",
        "arrival_ms": {"left_low": 0.0, "left_high": 3.0},
        "added_delay_ms": {"left_low": 3.0, "left_high": 0.0},
        "added_gain_db": {"left_low": 2.0, "left_high": -2.0},
        "way_levels_db": {"left_low": -12.0, "left_high": -8.0},
        "planning_isolation_db": {"left_low": 18.5, "left_high": 21.0},
        "reference_role": "left_high",
        "arrival_source": "shared-planning-take",
    }
    base.update(overrides)
    return base


def check(**overrides):
    base = {
        "confirmed": True,
        "reasons": [],
        "warnings": [],
        "max_residual_ms": 0.021,
        "before_spread_ms": 3.0,
        "after_arrival_ms": {"left_low": 5.0, "left_high": 5.021},
        "tolerance_ms": 0.25,
        "pairs": [{"roles": ["left_low", "left_high"], "residual_within_pair_ms": 0.021}],
        "gain_spread_db": 0.2,
        "before_gain_spread_db": 4.0,
        "gain_tolerance_db": 2.0,
        "after_way_levels_db": {"left_low": -10.0, "left_high": -10.2},
        "way_isolation_db": {"left_low": 19.0, "left_high": None},
        "isolation_margin_db": 19.0,
    }
    base.update(overrides)
    return base


def frequency():
    points = [[20.0, -12.0], [100.0, -11.5], [1000.0, -10.0]]
    return {
        "left_low": {"trusted_points": [list(p) for p in points]},
        "left_high": {"review_points": [[20.0, -8.0], [100.0, -8.2], [1000.0, -9.0]]},
    }


class BuildRunTests(unittest.TestCase):
    def test_before_after_sources_and_dataset_preserved(self):
        run = build_speaker_align_run(
            side="left", proposal=proposal(), check=check(),
            provenance={"microphone_node": "mic"},
            params={"input_id": "mic-1"},
            sample_rate_hz=48000, job_id="job-1",
            committed_revision=8, frequency=frequency())
        self.assertEqual(run["schema"], SPEAKER_ALIGN_RUN_SCHEMA)
        self.assertEqual(run["before"]["source"], "planning-take")
        self.assertEqual(run["after"]["source"], "verification-take")
        self.assertEqual(run["ways"], ["left_high", "left_low"])
        self.assertEqual(run["before"]["arrival_ms"], {"left_high": 3.0, "left_low": 0.0})
        self.assertEqual(run["after"]["arrival_ms"], {"left_high": 5.021, "left_low": 5.0})
        self.assertEqual(run["corrections"]["added_delay_ms"], {"left_high": 0.0, "left_low": 3.0})
        self.assertEqual(run["corrections"]["added_gain_db"], {"left_high": -2.0, "left_low": 2.0})
        self.assertEqual(run["qc"]["max_residual_ms"], 0.021)
        self.assertEqual(run["qc"]["before_spread_ms"], 3.0)
        self.assertEqual(run["before"]["way_isolation_db"]["left_low"], 18.5)
        self.assertIsNone(run["after"]["way_isolation_db"]["left_high"])
        self.assertEqual(run["metadata"]["start_revision"], 7)
        self.assertEqual(run["metadata"]["processing_fingerprint"], "frozen-plan")
        self.assertEqual(run["frequency"]["left_low"]["trusted_points"][0], [20.0, -12.0])

    def test_frequency_is_optional_passthrough(self):
        run = build_speaker_align_run(side="left", proposal=proposal(), check=check())
        self.assertNotIn("frequency", run)

    def test_mismatched_ways_fail_closed(self):
        bad = check(after_arrival_ms={"left_low": 5.0, "left_other": 5.0})
        with self.assertRaises(ValueError):
            build_speaker_align_run(side="left", proposal=proposal(), check=bad)

    def test_run_sources_are_gated_on_load(self):
        run = build_speaker_align_run(side="left", proposal=proposal(), check=check())
        run["before"]["source"] = "verification-take"
        with self.assertRaisesRegex(ValueError, "planning take"):
            validate_speaker_align_run(run)
        run = build_speaker_align_run(side="left", proposal=proposal(), check=check())
        run["after"]["source"] = "planning-take"
        with self.assertRaisesRegex(ValueError, "verification take"):
            validate_speaker_align_run(run)


class TimeDomainViewTests(unittest.TestCase):
    def test_shared_window_shows_relative_offset(self):
        run = build_speaker_align_run(side="left", proposal=proposal(), check=check())
        view = time_domain_view(run)
        self.assertEqual(view["lanes"]["before"]["source"], "planning-take")
        self.assertEqual(view["lanes"]["after"]["source"], "verification-take")
        self.assertEqual(view["ways"], ["left_high", "left_low"])
        self.assertAlmostEqual(view["lanes"]["before"]["spread_ms"], 3.0)
        self.assertAlmostEqual(view["lanes"]["after"]["spread_ms"], 0.021)
        low, high = view["window_ms"]
        self.assertLessEqual(low, 0.0)
        self.assertGreaterEqual(high, 5.021)
        # Both lanes on one axis: the same ms value maps to the same position.
        self.assertEqual(view["lanes"]["before"]["arrival_ms"]["left_low"], 0.0)
        self.assertEqual(view["lanes"]["after"]["arrival_ms"]["left_low"], 5.0)

    def test_view_is_pure_derivation_without_measurement(self):
        run = build_speaker_align_run(side="left", proposal=proposal(), check=check())
        first = time_domain_view(run)
        run["before"]["arrival_ms"]["left_low"] = 99.0
        second = time_domain_view(run)
        self.assertNotEqual(first["lanes"]["before"]["arrival_ms"]["left_low"],
                            second["lanes"]["before"]["arrival_ms"]["left_low"])


class PersistenceRoundtripTests(unittest.TestCase):
    def store(self, root):
        return MeasurementStore(home=Path(root))

    def test_save_reopen_keeps_run_and_frequency(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(
                os.environ, {"XDG_CONFIG_HOME": str(Path(root) / "config"),
                             "XDG_STATE_HOME": str(Path(root) / "state")}):
            store = self.store(root)
            run = build_speaker_align_run(
                side="left", proposal=proposal(), check=check(),
                provenance={"microphone_node": "mic"},
                params={"input_id": "mic-1"}, sample_rate_hz=48000,
                job_id="job-1", committed_revision=8, frequency=frequency())
            payload = run_to_measurement(run, name="Left align")
            self.assertEqual(payload["measurement_kind"], SPEAKER_ALIGN_RUN_KIND)
            self.assertTrue(payload["traces"])
            saved = store.save_measurement(payload)
            self.assertEqual(saved["speaker_align"]["ways"], ["left_high", "left_low"])
            reloaded = store.list_measurements()["measurements"][0]
            reopened = measurement_to_run(reloaded)
            self.assertEqual(reopened, run)
            view_before = time_domain_view(run)
            view_after = time_domain_view(reopened)
            self.assertEqual(view_before, view_after)

    def test_timing_only_run_saves_without_traces(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(
                os.environ, {"XDG_CONFIG_HOME": str(Path(root) / "config"),
                             "XDG_STATE_HOME": str(Path(root) / "state")}):
            store = self.store(root)
            run = build_speaker_align_run(side="left", proposal=proposal(), check=check())
            payload = run_to_measurement(run)
            self.assertEqual(payload["traces"], [])
            saved = store.save_measurement(payload)
            self.assertEqual(saved["measurement_kind"], SPEAKER_ALIGN_RUN_KIND)
            reopened = measurement_to_run(store.get_measurement(saved["id"]))
            self.assertEqual(reopened["ways"], run["ways"])
            self.assertEqual(time_domain_view(reopened)["lanes"], time_domain_view(run)["lanes"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
