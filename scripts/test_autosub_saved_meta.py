#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""AutoSub saved-measurement metadata tests.

Verifies that AutoSub runners embed ``autosub_meta`` (Target Curve + final
sub gains) and ``measurement_kind="auto_sub"`` into the baseline and
confirmation measurements they return, and that the metadata builder:
- takes the complete Target Curve from the job's own target_curve snapshot
  (never from a later-selected UI curve),
- omits the target part when no curve was captured,
- emits ``sub`` for 2.1 and ``sub1``/``sub2`` for 2.2 modes,
- rounds gains to two decimals.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from measurement.autosub.scoring import (  # noqa: E402
    _auto_sub_display_offset_db,
    _auto_sub_measurement_from_sweep,
    _auto_sub_result_meta,
)
from measurement.store import MeasurementStore  # noqa: E402

TARGET = {
    "key": "harman",
    "label": "Harman-style",
    "provenance": "built_in",
    "points": [[20, 0], [20000, -5]],
}


def sweep(points_left: list, points_right: list | None = None) -> dict:
    result = {"points_left": points_left}
    if points_right is not None:
        result["points_right"] = points_right
    return result


class AutoSubResultMetaTests(unittest.TestCase):
    def test_target_label_comes_from_job_snapshot(self) -> None:
        job = {"target_curve": TARGET}
        meta = _auto_sub_result_meta(job, "subwoofer-2.1", {"sub": 1.141})
        self.assertEqual(meta["target"], TARGET)

    def test_target_omitted_when_no_curve(self) -> None:
        meta = _auto_sub_result_meta({"target_curve": None}, "subwoofer-2.1", {"sub": 0.5})
        self.assertIsNone(meta["target"])
        self.assertEqual(meta["final_gains_db"], {"sub": 0.5})

    def test_21_uses_sub_key(self) -> None:
        meta = _auto_sub_result_meta({"target_curve": TARGET}, "subwoofer-2.1", {"sub": -0.94})
        self.assertEqual(meta["final_gains_db"], {"sub": -0.94})

    def test_22_uses_sub1_sub2_keys(self) -> None:
        meta = _auto_sub_result_meta(
            {"target_curve": TARGET}, "subwoofer-2.2", {"sub1": 1.141, "sub2": -0.626}
        )
        self.assertEqual(meta["final_gains_db"], {"sub1": 1.14, "sub2": -0.63})

    def test_22_stereo_same_keys(self) -> None:
        meta = _auto_sub_result_meta(
            {"target_curve": TARGET}, "subwoofer-2.2-stereo", {"sub1": 0.5, "sub2": -4.0}
        )
        self.assertEqual(meta["final_gains_db"], {"sub1": 0.5, "sub2": -4.0})

    def test_empty_gains_omit_final_gains_db(self) -> None:
        meta = _auto_sub_result_meta({"target_curve": TARGET}, "subwoofer-2.2", {})
        self.assertNotIn("final_gains_db", meta)

    def test_meta_is_json_serializable(self) -> None:
        meta = _auto_sub_result_meta({"target_curve": TARGET}, "subwoofer-2.2", {"sub1": 1.1, "sub2": -0.6})
        json.dumps(meta)


class AutoSubMeasurementEmbedTests(unittest.TestCase):
    def test_display_offset_transform_applies_anchor_shift_with_inverse_sign(self) -> None:
        self.assertEqual(_auto_sub_display_offset_db(12.0, 1.5, 3.0), 13.5)
        self.assertEqual(_auto_sub_display_offset_db(-20.0, -1.25, -4.0), -22.75)

    def test_measurement_from_sweep_without_meta_has_no_kind(self) -> None:
        measurement = _auto_sub_measurement_from_sweep(
            sweep([[20, 0], [100, 1], [20000, -2]]), "Before", "AutoSub Baseline (0.0 ms)"
        )
        self.assertNotIn("measurement_kind", measurement)
        self.assertNotIn("autosub_meta", measurement)

    def test_measurement_from_sweep_with_meta_embeds_meta(self) -> None:
        # The sweep helper embeds autosub_meta; the RUNNERS add
        # measurement_kind="auto_sub" on top (see runner tests).
        meta = _auto_sub_result_meta({"target_curve": TARGET}, "subwoofer-2.1", {"sub": 1.1})
        measurement = _auto_sub_measurement_from_sweep(
            sweep([[20, 0], [100, 1], [20000, -2]]),
            "After",
            "AutoSub After (1.0 ms)",
            meta=meta,
        )
        self.assertNotIn("measurement_kind", measurement)
        self.assertEqual(measurement["autosub_meta"]["target"], TARGET)
        self.assertEqual(measurement["autosub_meta"]["final_gains_db"], {"sub": 1.1})

    def test_display_offset_undoes_positive_anchor_shift(self) -> None:
        measurement = _auto_sub_measurement_from_sweep(
            {
                "points_left": [[20, 11.5], [100, 12.5], [20000, 9.5]],
                "normalized_by_db_left": 12.0,
                "display_anchor_shift_db_left": 1.5,
            },
            "Before",
            "AutoSub Before",
            offset_db=3.0,
        )
        trace = measurement["traces"][0]
        self.assertEqual(trace["points"][0][1], 8.5)
        self.assertEqual(trace["display_offset_db"], 13.5)
        calibrated_db = 10.0 + 12.0
        self.assertEqual(calibrated_db - trace["display_offset_db"], trace["points"][0][1])


class AutoSubMeasurementPersistenceTests(unittest.TestCase):
    def test_save_reload_preserves_exact_target_display_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(Path(root) / "config"),
                "XDG_STATE_HOME": str(Path(root) / "state"),
            },
        ):
            store = MeasurementStore(home=Path(root))
            payload = {
                "id": "autosub-before-left",
                "name": "AutoSub Before L",
                "channel": "left",
                "measurement_kind": "auto_sub",
                "traces": [{
                    "kind": "measured",
                    "label": "Before L",
                    "role": "left",
                    "display_offset_db": 13.5,
                    "points": [[20, 8.5], [100, 9.5], [20000, 6.5]],
                }],
                "autosub_meta": {
                    "target": TARGET,
                    "target_vertical_offset_db": -2.25,
                    "final_gains_db": {"sub": 1.1},
                },
            }

            saved = store.save_measurement(payload)
            reloaded = store.list_measurements()["measurements"][0]

            for measurement in (saved, reloaded):
                self.assertEqual(measurement["traces"][0]["display_offset_db"], 13.5)
                self.assertEqual(measurement["autosub_meta"]["target"], TARGET)
                self.assertEqual(measurement["autosub_meta"]["target_vertical_offset_db"], -2.25)


if __name__ == "__main__":
    unittest.main()
