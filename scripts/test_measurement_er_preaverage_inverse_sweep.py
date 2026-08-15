#!/usr/bin/env python3
"""ER pre-average regression: the inverse sweep is built from measurement_signal.

The L/R repeat ER pre-average path used to call the removed
``MeasurementStore._build_inverse_sweep``, raising AttributeError and silently
falling back to per-sweep analysis.  This test runs the real inverse-sweep
build and proves the pre-average path is reached without that fallback.
"""

import os
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

import measurement.repeat_runner as repeat_runner_module
from measurement.store import MeasurementStore
from measurement.signal import generate_log_sweep


def _write_stereo_wav(path: Path, sample_rate: int, data: np.ndarray) -> None:
    samples = np.clip(data, -1.0, 1.0)
    int16 = (samples * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(int16.tobytes())


class ErPreaverageInverseSweepTests(unittest.TestCase):
    def test_er_preaverage_builds_inverse_sweep_without_attribute_error(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = MeasurementStore(home=Path(tempdir))
            job_id = "measurement-repeat-job-inverse"
            store._jobs[job_id] = {
                "id": job_id,
                "status": "queued",
                "created_at": store._utc_now(),
                "updated_at": store._utc_now(),
                "message": "L/R repeat queued.",
                "result": None,
                "error": None,
                "job_kind": "lr-repeat",
                "repeat_count": 1,
                "base_name": "Sofa",
                "channel": "stereo",
            }

            sample_rate = 48_000
            sweep_seconds = 0.05
            lead_in_seconds = 0.01
            # Match the frequency defaults the pre-average path uses.
            sweep = generate_log_sweep(sample_rate, sweep_seconds, 10.0, 22_000.0)
            lead_in = np.zeros(int(round(sample_rate * lead_in_seconds)), dtype=np.float32)
            program = np.concatenate([lead_in, sweep]).astype(np.float32)
            stereo = np.column_stack([program, program])

            # Only the playback WAVs are read by the pre-average path; write a
            # real sweep so reference extraction and inverse build succeed.
            for side in ("left", "right"):
                playback = store.playbacks_dir / f"{job_id}-repeat1-{side}.wav"
                _write_stereo_wav(playback, sample_rate, stereo)

            def _executor(job):
                sweep_id = job["id"]
                return {
                    "measurement": {
                        "channel": job["channel"],
                        "analysis": {"reference_path": {}},
                        "traces": [
                            {
                                "role": "trusted",
                                "kind": "sweep-response",
                                "label": "trusted",
                                "points": [[20.0, 0.0], [1000.0, 1.0], [20000.0, 0.0]],
                            }
                        ],
                        "review_traces": [
                            {
                                "role": "raw-review",
                                "kind": "sweep-response-review",
                                "label": "review",
                                "points": [[20.0, 0.0], [20000.0, 0.0]],
                            }
                        ],
                    },
                    "_capture_path": str(store.captures_dir / f"{sweep_id}.wav"),
                    "_playback_path": str(store.playbacks_dir / f"{sweep_id}.wav"),
                    "_sample_rate": sample_rate,
                    "_mic_input_channel_index": 0,
                    "_electrical_reference_channel_index": 1,
                    "_use_electrical_reference": True,
                    "_calibration_curve": None,
                    "_sweep_seconds": sweep_seconds,
                    "_lead_in_seconds": lead_in_seconds,
                    "_tail_seconds": 0.0,
                    "_record_preroll_seconds": 0.0,
                    "_record_postroll_seconds": 0.0,
                    "_record_duration_seconds": sweep_seconds + lead_in_seconds,
                }

            store._execute_capture_job = _executor

            inverse_calls = []
            real_build_inverse = repeat_runner_module.build_inverse_sweep

            def _spy_build_inverse(sweep, *, sample_rate, duration_seconds, start_hz, end_hz):
                result = real_build_inverse(
                    sweep,
                    sample_rate=sample_rate,
                    duration_seconds=duration_seconds,
                    start_hz=start_hz,
                    end_hz=end_hz,
                )
                inverse_calls.append(
                    {
                        "sweep": sweep,
                        "sample_rate": sample_rate,
                        "duration_seconds": duration_seconds,
                        "start_hz": start_hz,
                        "end_hz": end_hz,
                        "result": result,
                    }
                )
                return result

            received = {}

            def _fake_pre_average(
                capture_paths,
                *,
                playback_path,
                sample_rate,
                mic_input_channel_index,
                electrical_reference_channel_index,
                calibration_curve,
                reference_sweep,
                inverse_sweep,
            ):
                received["inverse_sweep"] = inverse_sweep
                received["reference_sweep"] = reference_sweep
                return {"sample_rate": sample_rate, "trusted_points": [], "review_points": []}, {
                    "pre_average_applied": True,
                    "capture_count": len(capture_paths),
                }

            def _fake_summary(own, other, *, side, base_name, repeat_count, pre_avg_debug):
                return {"channel": side, "measurement_kind": "lr-repeat-paired-average-summary"}

            with patch.object(
                repeat_runner_module, "build_inverse_sweep", _spy_build_inverse
            ), patch.object(
                store._repeat_runner, "_pre_average_er_captures", _fake_pre_average
            ), patch.object(
                store._repeat_runner, "_build_pre_averaged_lr_summary", _fake_summary
            ):
                result = store._execute_lr_repeat_job(store._jobs[job_id])

            # The pre-average path ran for both sides: the stale Store call
            # would have raised AttributeError before reaching the fake.
            self.assertEqual(len(inverse_calls), 2)
            self.assertEqual(sorted(m["channel"] for m in result["measurements"]), ["left", "right"])

            self.assertIsInstance(received["inverse_sweep"], np.ndarray)
            self.assertGreater(received["inverse_sweep"].size, 0)
            self.assertTrue(np.all(np.isfinite(received["inverse_sweep"])))
            self.assertEqual(received["inverse_sweep"].shape, received["reference_sweep"].shape)

            for call in inverse_calls:
                self.assertEqual(call["sample_rate"], sample_rate)
                self.assertEqual(call["duration_seconds"], sweep_seconds)
                self.assertEqual(call["start_hz"], 10.0)
                self.assertEqual(call["end_hz"], 22_000.0)
                self.assertIsInstance(call["result"], np.ndarray)
                self.assertGreater(call["result"].size, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
