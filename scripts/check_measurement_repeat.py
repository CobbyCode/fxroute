#!/usr/bin/env python3
"""Regression check for same-position L/R repeat summaries."""

from __future__ import annotations

import tempfile
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from measurement import MeasurementStore


def measurement_payload(measurement_id: str, channel: str, timing_ms: float, level_db: float, *, electrical: bool) -> dict:
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


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fxroute-measurement-repeat-") as temp_dir:
        root = Path(temp_dir)
        old_config_home = os.environ.get("XDG_CONFIG_HOME")
        old_state_home = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(root / "config")
        os.environ["XDG_STATE_HOME"] = str(root / "state")
        try:
            store = MeasurementStore(home=root)
            left = [
            measurement_payload("left-1", "left", 1.00, 0.0, electrical=True),
            measurement_payload("left-2", "left", 1.10, 2.0, electrical=True),
            measurement_payload("left-outlier", "left", 3.00, 20.0, electrical=True),
        ]
            summary = store.summarize_repeat_measurements(left, base_name="Sofa", channel="left", repeat_count=3)
            repeat = summary["analysis"]["lr_repeat"]
            assert summary["name"] == "Sofa · L"
            assert summary["measurement_kind"] == "lr-repeat-summary"
            assert repeat["repeat_count"] == 3
            assert repeat["accepted_runs"] == 2
            assert repeat["rejected_runs"] == 1
            assert repeat["accepted_run_numbers"] == [1, 2]
            assert repeat["timing_method"] == "electrical-reference-cluster-median"
            assert repeat["timing_spread_ms"] == 0.1
            assert summary["analysis"]["reference_path"]["acoustic_arrival_corrected_ms"] == 1.05
            assert summary["traces"][0]["points"] == [[20.0, 1.0], [1000.0, 2.0], [20000.0, 3.0]]

            right = [
                measurement_payload("right-1", "right", 1.00, 0.0, electrical=False),
                measurement_payload("right-2", "right", 3.00, 2.0, electrical=False),
            ]
            unstable = store.summarize_repeat_measurements(right, base_name="Sofa", channel="right", repeat_count=2)
            unstable_repeat = unstable["analysis"]["lr_repeat"]
            assert unstable["name"] == "Sofa · R"
            assert unstable_repeat["accepted_runs"] == 0
            assert unstable_repeat["rejected_runs"] == 2
            assert unstable_repeat["timing_stable"] is False
            assert unstable["analysis"]["reference_path"]["timing_status"] == "lr-repeat-unstable"
            assert "arrival_ms" not in unstable["analysis"]["impulse_response"]

            saved_left = store.save_measurement(summary)
            saved_right = store.save_measurement(unstable)
            listed = store.list_measurements()["measurements"]
            assert {item["id"] for item in listed} == {saved_left["id"], saved_right["id"]}
        finally:
            if old_config_home is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_config_home
            if old_state_home is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = old_state_home

    print("Measurement L/R repeat regression check passed.")


if __name__ == "__main__":
    main()
