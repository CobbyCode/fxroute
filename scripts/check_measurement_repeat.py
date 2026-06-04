#!/usr/bin/env python3
"""Regression check for same-position L/R repeat summaries."""

from __future__ import annotations

import asyncio
import tempfile
import sys
import os
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from measurement import (
    HOST_SWEEP_RECORD_POSTROLL_SECONDS,
    HOST_SWEEP_RECORD_PREROLL_SECONDS,
    LR_REPEAT_LEAD_IN_SECONDS,
    LR_REPEAT_SWEEP_SECONDS,
    LR_REPEAT_TAIL_SECONDS,
    MeasurementStore,
    SWEEP_V2_LEAD_IN_SECONDS,
    SWEEP_V2_SECONDS,
    SWEEP_V2_TAIL_SECONDS,
)


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


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fxroute-measurement-repeat-") as temp_dir:
        root = Path(temp_dir)
        old_config_home = os.environ.get("XDG_CONFIG_HOME")
        old_state_home = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(root / "config")
        os.environ["XDG_STATE_HOME"] = str(root / "state")
        try:
            store = MeasurementStore(home=root)
            sweep_profile = store._default_measurement_sweep_profile()
            assert sweep_profile["sweep_seconds"] == SWEEP_V2_SECONDS
            assert sweep_profile["lead_in_seconds"] == SWEEP_V2_LEAD_IN_SECONDS
            assert sweep_profile["tail_seconds"] == SWEEP_V2_TAIL_SECONDS
            assert sweep_profile["record_preroll_seconds"] == HOST_SWEEP_RECORD_PREROLL_SECONDS
            assert sweep_profile["record_postroll_seconds"] == HOST_SWEEP_RECORD_POSTROLL_SECONDS
            assert sweep_profile["sweep_seconds"] != LR_REPEAT_SWEEP_SECONDS
            assert sweep_profile["lead_in_seconds"] != LR_REPEAT_LEAD_IN_SECONDS
            assert sweep_profile["tail_seconds"] != LR_REPEAT_TAIL_SECONDS

            public_result = store._public_job_result({
                "measurement": {"id": "single"},
                "_calibration_curve": (np.array([20.0]), np.array([0.0])),
                "nested": {"_capture_path": "/tmp/capture.wav", "visible": True},
            })
            assert public_result == {
                "measurement": {"id": "single"},
                "nested": {"visible": True},
            }

            async def check_public_job_result() -> None:
                job_id = "test-calibrated-single"
                store._jobs[job_id] = {
                    "id": job_id,
                    "status": "queued",
                    "message": "queued",
                    "result": None,
                    "error": None,
                }
                store._execute_capture_job = lambda _job: {
                    "message": "Measurement finished.",
                    "measurement": {"id": "single"},
                    "_calibration_curve": (np.array([20.0]), np.array([0.0])),
                }
                await store._run_measurement_job(job_id)
                job = store.get_job(job_id)
                assert job["status"] == "completed"
                assert job["result"] == {
                    "message": "Measurement finished.",
                    "measurement": {"id": "single"},
                }

            asyncio.run(check_public_job_result())

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
            assert repeat["reference_source"] == "electrical-input-channel-2"
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

            capture_index = {"left": 0, "right": 0}

            def fake_capture(job: dict) -> dict:
                channel = job["channel"]
                capture_index[channel] += 1
                return {
                    "measurement": measurement_payload(
                        f"{channel}-{capture_index[channel]}",
                        channel,
                        1.0 + (capture_index[channel] * 0.05),
                        float(capture_index[channel]),
                        electrical=True,
                    ),
                }

            store._execute_capture_job = fake_capture
            repeat_result = store._execute_lr_repeat_job({
                "id": "test-repeat-job",
                "repeat_count": 3,
                "base_name": "Sofa center",
            })
            assert repeat_result["base_name"] == "Sofa center"
            assert [measurement["name"] for measurement in repeat_result["measurements"]] == ["Sofa center · L", "Sofa center · R"]
            assert len(store.list_measurements()["measurements"]) == 2

            for measurement in repeat_result["measurements"]:
                measurement["name"] = f"Edited name · {'R' if measurement['channel'] == 'right' else 'L'}"
            saved_repeat = store.save_measurements(repeat_result["measurements"])
            assert [measurement["name"] for measurement in saved_repeat] == ["Edited name · L", "Edited name · R"]
            assert len(store.list_measurements()["measurements"]) == 4

            async def check_fixed_repeat_start() -> None:
                store._discover_capture_inputs = lambda: [{
                    "id": "test-input",
                    "label": "Test input",
                    "available": True,
                    "channels": 2,
                }]

                async def no_capture(_job_id: str) -> None:
                    return None

                store._run_measurement_job = no_capture
                job = await store.start_lr_repeat_measurement(input_id="test-input", base_name="Sofa center")
                assert job["repeat_count"] == 3
                assert job["base_name"] == "Sofa center"
                await asyncio.sleep(0)

            asyncio.run(check_fixed_repeat_start())
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
