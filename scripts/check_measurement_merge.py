#!/usr/bin/env python3
"""Regression check for saved-measurement merge persistence and averaging."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from measurement import MeasurementStore


def measurement_payload(
    measurement_id: str,
    *,
    channel: str,
    trusted_points: list[list[float]],
    review_points: list[list[float]] | None = None,
) -> dict:
    payload = {
        "id": measurement_id,
        "name": measurement_id,
        "channel": channel,
        "calibration": {"filename": "mic.txt", "applied": True},
        "traces": [
            {
                "kind": "sweep-response",
                "role": "trusted",
                "points": trusted_points,
            }
        ],
    }
    if review_points:
        payload["review_traces"] = [
            {
                "kind": "sweep-response-review",
                "role": "raw-review",
                "points": review_points,
            }
        ]
    return payload


def expect_value_error(callback, expected_text: str) -> None:
    try:
        callback()
    except ValueError as exc:
        assert expected_text in str(exc), str(exc)
    else:
        raise AssertionError(f"Expected ValueError containing: {expected_text}")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fxroute-measurement-merge-") as temp_dir:
        root = Path(temp_dir)
        old_config_home = os.environ.get("XDG_CONFIG_HOME")
        old_state_home = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(root / "config")
        os.environ["XDG_STATE_HOME"] = str(root / "state")
        try:
            store = MeasurementStore(home=root)
            store.save_measurement(measurement_payload(
                "left-a",
                channel="left",
                trusted_points=[[20, 0], [100, 10], [1000, 20]],
                review_points=[[10, -2], [100, 10], [2000, 24]],
            ))
            store.save_measurement(measurement_payload(
                "left-b",
                channel="left",
                trusted_points=[[20, 2], [200, 14], [1000, 22]],
                review_points=[[10, 0], [200, 14], [2000, 26]],
            ))
            store.save_measurement(measurement_payload(
                "right-no-review",
                channel="right",
                trusted_points=[[20, 4], [100, 12], [1000, 24]],
            ))
            store.save_measurement(measurement_payload(
                "no-overlap",
                channel="left",
                trusted_points=[[2000, 1], [4000, 2]],
            ))

            merged = store.merge_measurements(["left-a", "left-b"], "Merged left")
            assert merged["measurement_kind"] == "merged-measurement"
            assert merged["channel"] == "left"
            assert merged["analysis"]["source_measurement_ids"] == ["left-a", "left-b"]
            assert merged["traces"][0]["role"] == "trusted"
            assert merged["review_traces"][0]["role"] == "raw-review"
            assert merged["traces"][0]["points"] == [
                [20.0, 1.0],
                [100.0, 8.667],
                [200.0, 12.556],
                [1000.0, 21.0],
            ]
            json.dumps(merged)
            persisted = json.loads((store.measurements_dir / f"{merged['id']}.json").read_text())
            assert persisted["traces"] == merged["traces"]

            mixed = store.merge_measurements(["left-a", "right-no-review"], "Mixed channels")
            assert mixed["channel"] == "stereo"
            assert "review_traces" not in mixed

            expect_value_error(lambda: store.merge_measurements(["left-a"]), "at least two")
            expect_value_error(lambda: store.merge_measurements(["left-a", "left-a"]), "distinct")
            expect_value_error(lambda: store.merge_measurements(["left-a", "no-overlap"]), "usable frequency range")
        finally:
            if old_config_home is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_config_home
            if old_state_home is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = old_state_home

    print("Measurement merge regression check passed.")


if __name__ == "__main__":
    main()
