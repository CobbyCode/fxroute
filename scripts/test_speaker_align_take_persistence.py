#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Saved Speaker Align takes stay normal measurements tagged Before/After."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from measurement.store import MeasurementStore  # noqa: E402


def take_payload(measurement_id: str, take: object) -> dict:
    return {
        "id": measurement_id,
        "name": f"Speaker Align Right · {measurement_id}",
        "channel": "right",
        "measurement_kind": "sweep-response-v3",
        "traces": [{"kind": "sweep-response", "label": "trusted", "role": "trusted",
                    "points": [[20, -3.0], [1000, -1.0], [20000, -6.0]]}],
        "analysis": {"impulse_response": {"preview": {
            "schema": "fxroute.ir-preview.v1", "points": [[-2.0, 0.0], [0.0, 1.0], [30.0, 0.01]]}}},
        "speaker_align_take": take,
    }


class SpeakerAlignTakePersistenceTests(unittest.TestCase):
    def store(self, root: str) -> MeasurementStore:
        return MeasurementStore(home=Path(root))

    def test_save_reload_keeps_the_take_tag_and_ir_preview(self) -> None:
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {
                "XDG_CONFIG_HOME": str(Path(root) / "config"),
                "XDG_STATE_HOME": str(Path(root) / "state")}):
            store = self.store(root)
            saved = store.save_measurements([
                take_payload("before", {"side": "right", "take": "before", "extra": 1}),
                take_payload("after", {"side": "right", "take": "after"}),
            ])
            reloaded = {item["id"]: item for item in store.list_measurements()["measurements"]}
            for measurement in (*saved, *reloaded.values()):
                take = "before" if measurement["id"] == "before" else "after"
                self.assertEqual(measurement["speaker_align_take"], {"side": "right", "take": take})
                self.assertEqual(measurement["measurement_kind"], "sweep-response-v3")
                self.assertEqual(
                    measurement["analysis"]["impulse_response"]["preview"]["points"][1], [0.0, 1.0])

    def test_save_reload_keeps_the_timing_timeline(self) -> None:
        timeline = {
            "schema": "fxroute.speaker-align-timeline.v1", "time_origin": "reference-way-arrival",
            "reference_role": "right_low", "sample_rate": 48000,
            "arrival_ms": {"right_low": 0.0, "right_high": -0.458333},
            "full_band": [[-0.5, 0.1], [0.0, 1.0]],
            "ways": {"right_low": [[0.0, -1.0]], "right_high": [[-0.4583, 1.0]]},
        }
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {
                "XDG_CONFIG_HOME": str(Path(root) / "config"),
                "XDG_STATE_HOME": str(Path(root) / "state")}):
            store = self.store(root)
            payload = take_payload("before", {"side": "right", "take": "before"})
            payload["analysis"]["speaker_align_timeline"] = timeline
            store.save_measurements([payload])
            reloaded = store.list_measurements()["measurements"][0]
            self.assertEqual(reloaded["analysis"]["speaker_align_timeline"], timeline)

    def test_invalid_take_tags_are_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {
                "XDG_CONFIG_HOME": str(Path(root) / "config"),
                "XDG_STATE_HOME": str(Path(root) / "state")}):
            store = self.store(root)
            for index, take in enumerate((
                    {"side": "center", "take": "before"},
                    {"side": "left", "take": "during"},
                    "before",
                    None)):
                saved = store.save_measurement(take_payload(f"take-{index}", take))
                self.assertNotIn("speaker_align_take", saved)

    def test_trace_less_payload_is_rejected(self) -> None:
        # The retired speaker-align-run-v1 exemption is gone: a Speaker Align
        # save is a normal sweep and needs its traces.
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {
                "XDG_CONFIG_HOME": str(Path(root) / "config"),
                "XDG_STATE_HOME": str(Path(root) / "state")}):
            payload = take_payload("run", {"side": "left", "take": "after"})
            payload["traces"] = []
            payload["measurement_kind"] = "speaker-align-run-v1"
            with self.assertRaisesRegex(ValueError, "at least one trace"):
                self.store(root).save_measurement(payload)


if __name__ == "__main__":
    unittest.main()
