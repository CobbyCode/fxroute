#!/usr/bin/env python3
"""Temporary WAV lifecycle: captures/playbacks are removed on success,
failure and cancel, and jobs never touch another job's files."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from measurement import MeasurementStore, SWEEP_V2_SECONDS, SWEEP_V2_TAIL_SECONDS


def _repeat_meta_result(store, sweep_id):
    return {
        "measurement": {"channel": "left", "analysis": {}},
        "_capture_path": str(store.captures_dir / f"{sweep_id}.wav"),
        "_playback_path": str(store.playbacks_dir / f"{sweep_id}.wav"),
        "_sample_rate": 48_000,
        "_mic_input_channel_index": 0,
        "_electrical_reference_channel_index": None,
        "_use_electrical_reference": False,
        "_calibration_curve": None,
        "_sweep_seconds": 10.0,
        "_lead_in_seconds": 1.0,
        "_tail_seconds": 1.0,
        "_record_preroll_seconds": 0.5,
        "_record_postroll_seconds": 0.5,
        "_record_duration_seconds": 13.0,
    }


class MeasurementWavLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def _store(self, tempdir):
        return MeasurementStore(home=Path(tempdir))

    def _register_job(self, store, job_id, **overrides):
        job = {
            "id": job_id,
            "status": "queued",
            "created_at": store._utc_now(),
            "updated_at": store._utc_now(),
            "message": "Sweep queued.",
            "result": None,
            "error": None,
            "input_channels": {"mic": 1, "electrical_reference": None, "reference_disabled_reason": ""},
            "calibration": {"filename": "", "applied": False},
        }
        job.update(overrides)
        store._jobs[job_id] = job
        return job

    def _write_job_wavs(self, store, job_id):
        capture = store.captures_dir / f"{job_id}.wav"
        playback = store.playbacks_dir / f"{job_id}.wav"
        capture.write_bytes(b"RIFF-test")
        playback.write_bytes(b"RIFF-test")
        return capture, playback

    async def test_single_sweep_wavs_removed_on_success(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            job_id = "measurement-job-success"
            self._register_job(store, job_id)
            capture, playback = self._write_job_wavs(store, job_id)

            def _executor(_job):
                return {"message": "Measurement finished."}

            store._execute_capture_job = _executor
            await store._run_measurement_job(job_id)

            self.assertEqual(store.get_job(job_id)["status"], "completed")
            self.assertFalse(capture.exists())
            self.assertFalse(playback.exists())

    async def test_single_sweep_wavs_removed_on_failure(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            job_id = "measurement-job-failure"
            self._register_job(store, job_id)
            capture, playback = self._write_job_wavs(store, job_id)

            def _executor(_job):
                raise RuntimeError("boom")

            store._execute_capture_job = _executor
            await store._run_measurement_job(job_id)

            self.assertEqual(store.get_job(job_id)["status"], "failed")
            self.assertFalse(capture.exists())
            self.assertFalse(playback.exists())

    async def test_single_sweep_wavs_removed_on_cancel(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            job_id = "measurement-job-cancel"
            self._register_job(store, job_id)
            capture, playback = self._write_job_wavs(store, job_id)

            def _executor(_job):
                # Simulate a cancel that lands while the worker is running:
                # the worker observes it and aborts.
                store._cancelled_jobs.add(job_id)
                raise RuntimeError("Measurement cancelled.")

            store._execute_capture_job = _executor
            await store._run_measurement_job(job_id)

            self.assertEqual(store.get_job(job_id)["status"], "cancelled")
            self.assertFalse(capture.exists())
            self.assertFalse(playback.exists())

    async def test_lr_repeat_sweep_wavs_removed_on_success(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            job_id = "measurement-repeat-job-repeat-ok"
            self._register_job(store, job_id, job_kind="lr-repeat", repeat_count=1, base_name="R")
            created = []

            def _executor(job):
                sweep_id = job["id"]
                store.captures_dir.mkdir(parents=True, exist_ok=True)
                store.playbacks_dir.mkdir(parents=True, exist_ok=True)
                capture = store.captures_dir / f"{sweep_id}.wav"
                playback = store.playbacks_dir / f"{sweep_id}.wav"
                capture.write_bytes(b"RIFF-test")
                playback.write_bytes(b"RIFF-test")
                created.extend([capture, playback])
                return _repeat_meta_result(store, sweep_id)

            store._execute_capture_job = _executor
            store.summarize_lr_repeat_paired = lambda _a, _b, base_name="", repeat_count=1: [
                {"channel": "left"}, {"channel": "right"}
            ]
            result = store._execute_lr_repeat_job(store._jobs[job_id])

            self.assertIn("measurements", result)
            self.assertEqual(len(created), 4)
            for path in created:
                self.assertFalse(path.exists())

    async def test_lr_repeat_sweep_wavs_removed_on_failure(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            job_id = "measurement-repeat-job-repeat-fail"
            self._register_job(store, job_id, job_kind="lr-repeat", repeat_count=1, base_name="R")
            created = []

            def _executor(job):
                sweep_id = job["id"]
                store.captures_dir.mkdir(parents=True, exist_ok=True)
                store.playbacks_dir.mkdir(parents=True, exist_ok=True)
                capture = store.captures_dir / f"{sweep_id}.wav"
                playback = store.playbacks_dir / f"{sweep_id}.wav"
                capture.write_bytes(b"RIFF-test")
                playback.write_bytes(b"RIFF-test")
                created.extend([capture, playback])
                if str(sweep_id).endswith("-right"):
                    raise RuntimeError("sweep failed")
                return _repeat_meta_result(store, sweep_id)

            store._execute_capture_job = _executor
            with self.assertRaisesRegex(RuntimeError, "sweep failed"):
                store._execute_lr_repeat_job(store._jobs[job_id])

            self.assertTrue(created)
            for path in created:
                self.assertFalse(path.exists())

    async def test_lr_repeat_sweep_wavs_removed_on_cancel(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            job_id = "measurement-repeat-job-repeat-cancel"
            self._register_job(store, job_id, job_kind="lr-repeat", repeat_count=1, base_name="R")
            created = []

            def _executor(job):
                sweep_id = job["id"]
                store.captures_dir.mkdir(parents=True, exist_ok=True)
                store.playbacks_dir.mkdir(parents=True, exist_ok=True)
                capture = store.captures_dir / f"{sweep_id}.wav"
                playback = store.playbacks_dir / f"{sweep_id}.wav"
                capture.write_bytes(b"RIFF-test")
                playback.write_bytes(b"RIFF-test")
                created.extend([capture, playback])
                if str(sweep_id).endswith("-right"):
                    store._cancelled_jobs.add(job_id)
                    raise RuntimeError("Measurement cancelled.")
                return _repeat_meta_result(store, sweep_id)

            store._execute_capture_job = _executor
            with self.assertRaisesRegex(RuntimeError, "Measurement cancelled."):
                store._execute_lr_repeat_job(store._jobs[job_id])

            self.assertTrue(created)
            for path in created:
                self.assertFalse(path.exists())

    async def test_repeat_cleanup_never_touches_other_job_files(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            other_id = "measurement-job-other"
            other_capture = store.captures_dir / f"{other_id}.wav"
            other_playback = store.playbacks_dir / f"{other_id}.wav"
            other_capture.write_bytes(b"RIFF-test")
            other_playback.write_bytes(b"RIFF-test")

            store._cleanup_job_wav_files("measurement-job-unrelated")
            store._cleanup_lr_repeat_sweep_wavs("measurement-repeat-job-unrelated")

            self.assertTrue(other_capture.exists())
            self.assertTrue(other_playback.exists())

    async def test_preaverage_temp_wav_removed_when_analysis_fails(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            sweep = np.zeros((int(48_000 * (SWEEP_V2_SECONDS + SWEEP_V2_TAIL_SECONDS)), 2), dtype=np.float32)

            def _fake_load(_path):
                return 48_000, sweep

            def _fake_write_wav(path, _sr, _data):
                path.write_bytes(b"RIFF-test")

            def _fake_analyze(*_args, **_kwargs):
                raise RuntimeError("pre-average QC failed")

            with patch.object(store, "_load_wav_array", _fake_load), \
                    patch.object(store, "_find_sweep_start", lambda *_a, **_k: 0), \
                    patch.object(store, "_estimate_sweep_timing", lambda *_a, **_k: {
                        "aligned_start": 0,
                        "observed_sweep_samples": sweep.shape[0],
                        "start_score": 1.0,
                        "end_score": 1.0,
                        "estimated_ppm": 0.0,
                        "drift_ppm": 0.0,
                        "estimated_total_drift_samples": 0,
                        "total_drift_samples": 0,
                        "anchor_seconds": 0.0,
                    }), \
                    patch.object(store, "_compute_alignment_shift", lambda *_a, **_k: 0), \
                    patch.object(store, "_shift_signal", lambda signal, _shift: signal), \
                    patch.object(store, "_write_stereo_wav", _fake_write_wav), \
                    patch.object(store, "_analyze_sweep_capture", _fake_analyze):
                with self.assertRaisesRegex(RuntimeError, "pre-average QC failed"):
                    store._pre_average_er_captures(
                        capture_paths=["c1.wav", "c2.wav"],
                        playback_path="p.wav",
                        sample_rate=48_000,
                        mic_input_channel_index=0,
                        electrical_reference_channel_index=1,
                        calibration_curve=None,
                        reference_sweep=np.zeros(1024, dtype=np.float32),
                        inverse_sweep=np.zeros(1024, dtype=np.float32),
                    )

            self.assertEqual(list(store.captures_dir.glob("preavg-*.wav")), [])

    async def test_preaverage_temp_wav_removed_when_write_fails(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            sweep = np.zeros((int(48_000 * (SWEEP_V2_SECONDS + SWEEP_V2_TAIL_SECONDS)), 2), dtype=np.float32)

            def _fake_load(_path):
                return 48_000, sweep

            def _failing_write_wav(path, _sr, _data):
                # Simulate a partial write followed by an error.
                path.write_bytes(b"partial")
                raise OSError("disk full")

            with patch.object(store, "_load_wav_array", _fake_load), \
                    patch.object(store, "_find_sweep_start", lambda *_a, **_k: 0), \
                    patch.object(store, "_estimate_sweep_timing", lambda *_a, **_k: {
                        "aligned_start": 0,
                        "observed_sweep_samples": sweep.shape[0],
                        "start_score": 1.0,
                        "end_score": 1.0,
                        "estimated_ppm": 0.0,
                        "drift_ppm": 0.0,
                        "estimated_total_drift_samples": 0,
                        "total_drift_samples": 0,
                        "anchor_seconds": 0.0,
                    }), \
                    patch.object(store, "_compute_alignment_shift", lambda *_a, **_k: 0), \
                    patch.object(store, "_shift_signal", lambda signal, _shift: signal), \
                    patch.object(store, "_write_stereo_wav", _failing_write_wav):
                with self.assertRaisesRegex(OSError, "disk full"):
                    store._pre_average_er_captures(
                        capture_paths=["c1.wav", "c2.wav"],
                        playback_path="p.wav",
                        sample_rate=48_000,
                        mic_input_channel_index=0,
                        electrical_reference_channel_index=1,
                        calibration_curve=None,
                        reference_sweep=np.zeros(1024, dtype=np.float32),
                        inverse_sweep=np.zeros(1024, dtype=np.float32),
                    )

            self.assertEqual(list(store.captures_dir.glob("preavg-*.wav")), [])

    async def test_terminal_result_has_no_dangling_temp_wav_paths(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            job_id = "measurement-job-public-result"
            self._register_job(store, job_id)
            capture, playback = self._write_job_wavs(store, job_id)

            def _executor(_job):
                return {
                    "message": "Measurement finished.",
                    "capture": {
                        "path": str(capture),
                        "reference_path": str(capture),
                        "duration_seconds": 12.0,
                        "sample_rate": 48_000,
                        "channels": 2,
                        "input_node": "mic",
                        "microphone_node": "mic",
                        "mic_input_channel": 1,
                        "electrical_reference_input_channel": None,
                        "reference_node": "",
                        "reference_channel": "reference",
                        "record_node": "rec",
                        "routing_diagnostics": {},
                    },
                    "playback": {
                        "path": str(playback),
                        "duration_seconds": 12.0,
                        "sweep_seconds": 11.0,
                        "lead_in_seconds": 0.5,
                        "tail_seconds": 1.25,
                        "play_node": "play",
                        "target_name": "t",
                        "target_label": "l",
                        "timed_out": False,
                        "routing_diagnostics": {},
                    },
                    "analysis": {
                        "method": "sweep",
                        "reference_path": {"usable": True, "timing_label": "Electrical reference active"},
                    },
                    "calibration": {"filename": "cal.txt", "applied": True},
                }

            store._execute_capture_job = _executor
            await store._run_measurement_job(job_id)

            job = store.get_job(job_id)
            self.assertEqual(job["status"], "completed")
            self.assertFalse(capture.exists())
            self.assertFalse(playback.exists())
            result = job["result"]
            self.assertNotIn("path", result["capture"])
            self.assertNotIn("reference_path", result["capture"])
            self.assertNotIn("path", result["playback"])
            self.assertEqual(result["capture"]["duration_seconds"], 12.0)
            self.assertEqual(result["capture"]["channels"], 2)
            self.assertEqual(result["playback"]["sweep_seconds"], 11.0)
            self.assertTrue(result["analysis"]["reference_path"]["usable"])
            persisted = json.loads(
                (store.job_records_dir / f"{job_id}.json").read_text(encoding="utf-8")
            )
            self.assertNotIn("path", persisted["result"]["capture"])
            self.assertNotIn("path", persisted["result"]["playback"])

    async def test_persist_failure_does_not_skip_wav_cleanup(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            job_id = "measurement-job-persist-fail"
            self._register_job(store, job_id)
            capture, playback = self._write_job_wavs(store, job_id)

            original_persist = MeasurementStore._persist_job

            def _executor(_job):
                return {"message": "Measurement finished."}

            def _flaky_persist(job):
                if str(job.get("status") or "") in {"completed", "failed", "cancelled"}:
                    raise OSError("disk full")
                original_persist(store, job)

            store._execute_capture_job = _executor
            store._persist_job = _flaky_persist
            await store._run_measurement_job(job_id)

            self.assertEqual(store.get_job(job_id)["status"], "completed")
            self.assertFalse(capture.exists())
            self.assertFalse(playback.exists())

    async def test_two_jobs_with_separate_resources_do_not_interfere(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            ok_id = "measurement-job-ok"
            bad_id = "measurement-job-bad"
            self._register_job(store, ok_id)
            self._register_job(store, bad_id)
            ok_capture, ok_playback = self._write_job_wavs(store, ok_id)
            bad_capture, bad_playback = self._write_job_wavs(store, bad_id)

            def _executor_ok(_job):
                return {"message": "ok"}

            def _executor_bad(_job):
                raise RuntimeError("bad")

            store._execute_capture_job = _executor_ok
            await store._run_measurement_job(ok_id)
            store._execute_capture_job = _executor_bad
            await store._run_measurement_job(bad_id)

            self.assertFalse(ok_capture.exists())
            self.assertFalse(ok_playback.exists())
            self.assertFalse(bad_capture.exists())
            self.assertFalse(bad_playback.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
