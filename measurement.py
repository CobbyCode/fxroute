"""Separate measurement capture, analysis, and persistence for FXRoute."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import subprocess
import time
import wave
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np

from samplerate import get_audio_output_overview, get_samplerate_status
from system_volume import SystemVolumeError, get_node_volume, get_output_volume, set_node_volume, set_output_volume

DISPLAY_DEFAULTS = {
    "normalize": True,
    "smoothing": "1/6-oct",
    "target_db": 0,
    "x_range_hz": [20, 20000],
}

TRACE_COLORS = [
    "#6ee7b7",
    "#a78bfa",
    "#f59e0b",
    "#60a5fa",
    "#f472b6",
    "#f87171",
]

SWEEP_V2_SECONDS = 7.0
SWEEP_V2_LEAD_IN_SECONDS = 0.5
SWEEP_V2_TAIL_SECONDS = 1.25
SWEEP_START_HZ = 10.0
SWEEP_END_HZ = 22_000.0
HOST_SWEEP_PEAK_SCALE = 0.8
TRUSTED_MIN_HZ = 20.0
TRUSTED_MAX_HZ = 20_000.0
DISPLAY_POINT_COUNT = 96
EDGE_STABILITY_WINDOW_POINTS = 4
EDGE_STABILITY_MAX_DELTA_DB = 6.0
EDGE_STABILITY_MAX_SPAN_DB = 9.0
MIN_TRUSTED_POINTS = 24
BROWSER_UPPER_EDGE_GUARD_START_HZ = 17_500.0
BROWSER_UPPER_EDGE_REJECT_MAX_POINTS = 2
BROWSER_UPPER_EDGE_SINGLE_POINT_MAX_DEVIATION_DB = 4.5
BROWSER_UPPER_EDGE_SINGLE_POINT_MAX_DELTA_DB = 4.0
RESPONSE_OUTLIER_NEIGHBOR_RADIUS = 2
RESPONSE_OUTLIER_WARN_DB = 8.0
RESPONSE_OUTLIER_FAIL_DB = 12.0
RESPONSE_OUTLIER_MIN_HZ = 250.0
SWEEP_TIMING_ANCHOR_SECONDS = 0.35
SWEEP_TIMING_MULTI_ANCHOR_SECONDS = 0.18
SWEEP_TIMING_EDGE_INSET_SECONDS = 0.08
SWEEP_TIMING_SEARCH_SECONDS = 0.35
BROWSER_SWEEP_TIMING_START_SEARCH_MULTIPLIER = 2.25
SWEEP_TIMING_MAX_ABS_PPM = 12_000.0
SWEEP_TIMING_MIN_COMPENSATION_PPM = 75.0
SWEEP_TIMING_RESIDUAL_TOLERANCE_SECONDS = 0.04
SWEEP_TIMING_ANCHOR_LAYOUT = (
    ("start-inner", 0.06),
    ("start-body", 0.18),
    ("mid-low", 0.38),
    ("mid-high", 0.62),
    ("end-body", 0.82),
    ("end-inner", 0.94),
)
BROWSER_TIMING_WEIGHT_MULTIPLIERS = {
    "start-inner": 0.8,
    "start-body": 0.85,
    "mid-low": 1.35,
    "mid-high": 1.35,
    "end-body": 0.85,
    "end-inner": 0.8,
}
IR_WINDOW_PRE_SECONDS = 0.004
IR_WINDOW_POST_SECONDS = 0.35
IR_WINDOW_FADE_SECONDS = 0.012
BROWSER_SWEEP_START_DELAY_SECONDS = 1.5
BROWSER_SWEEP_END_PADDING_SECONDS = 1.5
HOST_SWEEP_RECORD_PREROLL_SECONDS = 0.75
HOST_SWEEP_RECORD_POSTROLL_SECONDS = 0.75
HOST_SWEEP_MAX_ATTEMPTS = 3
HOST_SWEEP_RETRY_DELAY_SECONDS = 0.4
HOST_SWEEP_AUTO_GAIN_RETRY_ATTEMPT = 1
HOST_SWEEP_AUTO_GAIN_TARGET_PERCENT = 100
BROWSER_MEASUREMENT_SAMPLE_RATE = 48_000
BROWSER_REFERENCE_MODE_V2 = "browser-hybrid-reference-v2"
BROWSER_MEASUREMENT_EXPERIMENT_ENABLED = False
BROWSER_REFERENCE_PRE_ROLL_SECONDS = 0.3
BROWSER_REFERENCE_SYNC_BURST_SECONDS = 0.22
BROWSER_REFERENCE_SYNC_GAP_SECONDS = 0.05
BROWSER_REFERENCE_SYNC_GUARD_SECONDS = 0.25
BROWSER_REFERENCE_TAIL_SECONDS = 1.5
BROWSER_REFERENCE_STOP_MARGIN_SECONDS = 0.2
BROWSER_REFERENCE_SYNC_RAMP_SECONDS = 0.025
BROWSER_REFERENCE_SYNC_START_HZ = 2_000.0
BROWSER_REFERENCE_SYNC_END_HZ = 10_000.0
BROWSER_REFERENCE_SYNC_SEEDS = (101, 211, 307, 401, 503, 601)
BROWSER_REFERENCE_PROGRAM_PEAK = 10 ** (-6.0 / 20.0)
BROWSER_REFERENCE_SYNC_PEAK_SCALE = 0.9
BROWSER_REFERENCE_SWEEP_PEAK_SCALE = 0.6
BROWSER_SYNC_MIN_TOTAL_BURSTS = 4
BROWSER_SYNC_MIN_CLUSTER_BURSTS = 2
BROWSER_SYNC_ACCEPT_WINDOW_SECONDS = 0.05
BROWSER_SYNC_REFINED_WINDOW_SECONDS = 0.03
BROWSER_SYNC_SCORE_WARN_THRESHOLD = 0.7
BROWSER_SYNC_RATIO_WARN_THRESHOLD = 1.25
BROWSER_SYNC_RESIDUAL_WARN_MS = 0.5
BROWSER_SYNC_RESIDUAL_FAIL_MS = 1.0
BROWSER_SYNC_MAX_RESIDUAL_WARN_MS = 1.0
BROWSER_SYNC_MAX_RESIDUAL_FAIL_MS = 2.0
BROWSER_DRIFT_WARN_PPM = 1_500.0
BROWSER_DRIFT_FAIL_PPM = 6_000.0
BROWSER_CORRECTED_SWEEP_SCORE_FAIL = 0.86
BROWSER_CORRECTED_SWEEP_SCORE_WARN = 0.92
ALIGNMENT_SCORE_FAIL_THRESHOLD = 0.90
ALIGNMENT_SCORE_WARN_THRESHOLD = 0.94
HOST_ALIGNMENT_SCORE_FAIL_THRESHOLD = 0.84
HOST_ALIGNMENT_SCORE_WARN_THRESHOLD = 0.90
CAPTURE_CLIP_FAIL_DBFS = -0.2
CAPTURE_CLIP_WARN_DBFS = -1.0
CLOCK_DRIFT_WARN_PPM = 3_000.0
CHANNEL_CORRELATION_WARN_THRESHOLD = 0.985
BROWSER_CAPTURE_LEVEL_WARN_PEAK_DBFS = -45.0
BROWSER_CAPTURE_LEVEL_WARN_RMS_DBFS = -60.0

MEASUREMENT_SCOPE_NOTE = (
    "Real sweep capture v3 generates a deterministic host-local log sweep, plays it over the active output, "
    "records the selected PipeWire input in parallel, and derives a normalized response trace from inverse-sweep deconvolution with "
    "basic timing compensation and IR windowing. The sweep now measures beyond the visible range for better edge behavior, while the normal view stays focused on 20 Hz .. 20 kHz. "
    "It stays separate from EasyEffects presets and active PEQ state, and it is intentionally not a full REW replacement."
)


class CaptureQualityError(RuntimeError):
    def __init__(self, capture_label: str, items: list[dict[str, Any]], analysis: dict[str, Any] | None = None):
        self.capture_label = capture_label
        self.items = [dict(item) for item in items]
        self.analysis = deepcopy(analysis) if isinstance(analysis, dict) else None
        hard_failures = [str(item.get("message") or "Capture QC failed") for item in self.items if item.get("level") == "error"]
        super().__init__(f"{capture_label} QC failed: " + "; ".join(hard_failures))


class MeasurementStore:
    """Persist measurement JSON and run conservative real sweep measurement jobs."""

    def __init__(self, home: Path | None = None):
        self.home = Path(home or Path.home())
        self.config_root = Path(os.environ.get("XDG_CONFIG_HOME") or (self.home / ".config"))
        self.state_root = Path(os.environ.get("XDG_STATE_HOME") or (self.home / ".local" / "state"))
        self.measurements_dir = self.config_root / "fxroute" / "measurements"
        self.jobs_dir = self.state_root / "fxroute" / "measurements"
        self.captures_dir = self.jobs_dir / "captures"
        self.calibrations_dir = self.jobs_dir / "calibrations"
        self.settings_path = self.jobs_dir / "settings.json"
        self.job_records_dir = self.jobs_dir / "jobs"
        self.playbacks_dir = self.jobs_dir / "playbacks"
        for directory in [
            self.measurements_dir,
            self.jobs_dir,
            self.captures_dir,
            self.calibrations_dir,
            self.job_records_dir,
            self.playbacks_dir,
        ]:
            directory.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, dict[str, Any]] = {}
        self._job_tasks: dict[str, asyncio.Task[Any]] = {}
        self._job_processes: dict[str, list[subprocess.Popen[str]]] = {}
        self._cancelled_jobs: set[str] = set()

    def list_measurements(self) -> dict[str, Any]:
        measurements = []
        for path in sorted(self.measurements_dir.glob("*.json"), reverse=True):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                measurements.append(self._normalize_measurement(payload, source_path=path))
            except Exception:
                continue
        measurements.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        return {
            "status": "ok",
            "storage": {
                "directory": str(self.measurements_dir),
                "jobs_directory": str(self.jobs_dir),
            },
            "calibrations": self._list_calibration_files(),
            "active_calibration_file_id": self.get_active_calibration_file_id(),
            "scope_note": MEASUREMENT_SCOPE_NOTE,
            "measurements": measurements,
        }

    def list_inputs(self) -> dict[str, Any]:
        inputs = self._discover_capture_inputs()
        return {
            "status": "ok",
            "scope_note": MEASUREMENT_SCOPE_NOTE,
            "modes": [
                {
                    "id": "host-local",
                    "label": "Host-local capture",
                    "primary": True,
                    "available": any(item.get("available") for item in inputs),
                    "note": "Primary path: FXRoute plays and records on the host via PipeWire.",
                },
                {
                    "id": "browser-microphone",
                    "label": "Browser / client microphone",
                    "primary": False,
                    "available": False,
                    "note": "Currently disabled for real room measurement. Keep as an archival experiment path only.",
                },
            ],
            "inputs": inputs,
            "capture_available": any(item.get("available") for item in inputs),
            "discovery": {
                "method": "wpctl status -n + pactl list short sources",
                "source_count": len(inputs),
            },
        }

    async def start_measurement(
        self,
        *,
        input_id: str,
        channel: str,
        calibration_filename: str | None = None,
        calibration_bytes: bytes | None = None,
        calibration_ref: str | None = None,
    ) -> dict[str, Any]:
        inputs = self._discover_capture_inputs()
        selected_input = next((item for item in inputs if item["id"] == input_id), None)
        if not selected_input:
            raise ValueError("Selected capture input is no longer available")
        if not selected_input.get("available"):
            raise ValueError("Selected capture input is not available")

        normalized_channel = str(channel or "left").strip().lower()
        if normalized_channel not in {"left", "right", "stereo"}:
            raise ValueError("channel must be left, right, or stereo")

        calibration_meta = self._resolve_calibration_meta(
            calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes,
            calibration_ref=calibration_ref,
        )

        job_id = f"measurement-job-{uuid4().hex[:12]}"
        now = self._utc_now()
        job = {
            "id": job_id,
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "input": {
                "id": selected_input["id"],
                "label": selected_input["label"],
                "node_serial": selected_input.get("node_serial"),
                "node_name": selected_input.get("node_name"),
                "channels": selected_input.get("channels"),
                "sample_rate": selected_input.get("sample_rate"),
            },
            "channel": normalized_channel,
            "calibration": calibration_meta or {"filename": "", "applied": False},
            "message": "Sweep queued.",
            "scope_note": MEASUREMENT_SCOPE_NOTE,
            "result": None,
            "error": None,
        }
        self._jobs[job_id] = job
        self._persist_job(job)
        task = asyncio.create_task(self._run_measurement_job(job_id))
        self._job_tasks[job_id] = task
        return self.get_job(job_id)

    async def start_browser_measurement(
        self,
        *,
        channel: str,
        calibration_filename: str | None = None,
        calibration_bytes: bytes | None = None,
        calibration_ref: str | None = None,
    ) -> dict[str, Any]:
        if not BROWSER_MEASUREMENT_EXPERIMENT_ENABLED:
            raise RuntimeError("Browser/client microphone measurement is currently disabled while FXRoute is held to the host-local path.")

        normalized_channel = str(channel or "left").strip().lower()
        if normalized_channel not in {"left", "right", "stereo"}:
            raise ValueError("channel must be left, right, or stereo")

        calibration_meta = self._resolve_calibration_meta(
            calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes,
            calibration_ref=calibration_ref,
        )

        sample_rate = BROWSER_MEASUREMENT_SAMPLE_RATE
        job_id = f"browser-measurement-job-{uuid4().hex[:12]}"
        now = self._utc_now()

        playback_target = self._resolve_playback_target()
        playback_path = self.playbacks_dir / f"{job_id}.wav"
        sweep_meta = self._write_browser_reference_file(
            playback_path,
            sample_rate=sample_rate,
            channel=normalized_channel,
        )
        playback_duration_seconds = float(sweep_meta["playback_duration_seconds"])
        record_seconds = BROWSER_SWEEP_START_DELAY_SECONDS + playback_duration_seconds + BROWSER_REFERENCE_STOP_MARGIN_SECONDS

        job = {
            "id": job_id,
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "mode": "browser-microphone",
            "channel": normalized_channel,
            "calibration": calibration_meta or {"filename": "", "applied": False},
            "message": "Browser microphone ready. Start recording and keep the browser open until upload finishes.",
            "scope_note": MEASUREMENT_SCOPE_NOTE,
            "result": None,
            "error": None,
            "browser_capture": {
                "sample_rate": sample_rate,
                "preferred_channels": 2,
                "start_after_ms": int(round(BROWSER_SWEEP_START_DELAY_SECONDS * 1000)),
                "record_duration_ms": int(round(record_seconds * 1000)),
                "record_seconds": round(record_seconds, 3),
                "playback_duration_seconds": round(playback_duration_seconds, 3),
                "stop_margin_seconds": round(BROWSER_REFERENCE_STOP_MARGIN_SECONDS, 3),
            },
            "playback": {
                "path": str(playback_path),
                "duration_seconds": round(playback_duration_seconds, 3),
                "sweep_seconds": round(float(sweep_meta["sweep_seconds"]), 3),
                "lead_in_seconds": round(float(sweep_meta["pre_roll_seconds"]), 3),
                "tail_seconds": round(float(sweep_meta["tail_seconds"]), 3),
                "target_name": playback_target["target_name"],
                "target_label": playback_target["target_label"],
            },
            "analysis_reference": {
                "mode": BROWSER_REFERENCE_MODE_V2,
                "sample_rate": sample_rate,
                "analysis_sweep": sweep_meta["analysis_sweep"].tolist(),
                "inverse_sweep": sweep_meta["inverse_sweep"].tolist(),
                "sync_bursts": [
                    {
                        "name": str(burst["name"]),
                        "cluster": str(burst["cluster"]),
                        "seed": int(burst["seed"]),
                        "start_emit": int(burst["start_emit"]),
                        "end_emit": int(burst["end_emit"]),
                        "template": burst["template"].tolist(),
                    }
                    for burst in sweep_meta["sync_bursts"]
                ],
                "emit": sweep_meta["emit"],
            },
        }
        self._jobs[job_id] = job
        self._persist_job(job)
        task = asyncio.create_task(self._run_browser_playback_job(job_id))
        self._job_tasks[job_id] = task
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any]:
        job = self._jobs.get(job_id)
        if job is None:
            path = self.job_records_dir / f"{job_id}.json"
            if not path.exists():
                raise KeyError(job_id)
            job = json.loads(path.read_text(encoding="utf-8"))
            self._jobs[job_id] = job
        return deepcopy(job)

    def save_measurement(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize_measurement(payload)
        measurement_id = normalized["id"]
        path = self.measurements_dir / f"{measurement_id}.json"
        if path.exists():
            raise ValueError(f"Measurement already exists: {measurement_id}")
        path.write_text(json.dumps(normalized, indent=2) + "\n", encoding="utf-8")
        return normalized

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        job = self.get_job(job_id)
        status = str(job.get("status") or "")
        if status in {"completed", "failed", "cancelled"}:
            return job
        self._cancelled_jobs.add(job_id)
        for process in self._job_processes.get(job_id, []):
            try:
                if process.poll() is None:
                    process.terminate()
            except Exception:
                pass
        task = self._job_tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
        live_job = self._jobs.get(job_id)
        if live_job is not None:
            live_job["status"] = "cancelled"
            live_job["updated_at"] = self._utc_now()
            live_job["message"] = "Measurement cancelled."
            live_job["error"] = None
            self._persist_job(live_job)
        return self.get_job(job_id)

    def delete_measurement(self, measurement_id: str) -> None:
        measurement_id = str(measurement_id or "").strip()
        if not measurement_id:
            raise ValueError("Measurement id is required")
        path = self.measurements_dir / f"{measurement_id}.json"
        if not path.exists():
            raise KeyError(measurement_id)
        path.unlink()

    def has_active_measurement_job(self) -> bool:
        return any(str(job.get("status") or "") in {"queued", "running"} for job in self._jobs.values())

    def upload_calibration_file(self, filename: str, data: bytes) -> dict[str, Any]:
        if not data:
            raise ValueError("Calibration file is empty")
        meta = self._store_calibration_file(filename or "calibration.txt", data)
        self.set_active_calibration_file_id(str(meta.get("id") or ""))
        return self.get_calibration_state()

    def get_calibration_state(self) -> dict[str, Any]:
        files = self._list_calibration_files()
        active_id = self.get_active_calibration_file_id(files)
        return {
            "status": "ok",
            "calibrations": files,
            "active_calibration_file_id": active_id,
        }

    def set_active_calibration_file_id(self, calibration_ref: str | None) -> dict[str, Any]:
        ref = Path(str(calibration_ref or "")).name.strip()
        if ref and not self._lookup_calibration_file(ref):
            ref = ""
        settings = self._read_settings()
        measure_settings = settings.setdefault("measure", {})
        measure_settings["activeCalibrationFileId"] = ref
        self._write_settings(settings)
        return self.get_calibration_state()

    def get_active_calibration_file_id(self, files: list[dict[str, Any]] | None = None) -> str:
        settings = self._read_settings()
        ref = Path(str(settings.get("measure", {}).get("activeCalibrationFileId") or "")).name.strip()
        if not ref:
            return ""
        available = files if files is not None else self._list_calibration_files()
        if any(item.get("id") == ref for item in available):
            return ref
        # Stale setting: clear it so setup never crashes or keeps selecting a missing file.
        self.set_active_calibration_file_id("")
        return ""

    def delete_calibration_file(self, calibration_ref: str) -> dict[str, Any]:
        if self.has_active_measurement_job():
            raise RuntimeError("Cannot delete calibration files while a measurement is active")
        ref = Path(str(calibration_ref or "")).name.strip()
        if not ref:
            raise ValueError("Calibration file id is required")
        path = self.calibrations_dir / ref
        if not path.exists() or not path.is_file():
            raise KeyError(ref)
        path.unlink()
        if self.get_active_calibration_file_id() == ref:
            self.set_active_calibration_file_id("")
        return self.get_calibration_state()

    async def _run_measurement_job(self, job_id: str) -> None:
        job = self._jobs[job_id]
        job["status"] = "running"
        job["updated_at"] = self._utc_now()
        job["message"] = "Running sweep…"
        self._persist_job(job)
        try:
            result = await asyncio.to_thread(self._execute_capture_job, deepcopy(job))
            if job_id in self._cancelled_jobs:
                job["status"] = "cancelled"
                job["updated_at"] = self._utc_now()
                job["message"] = "Measurement cancelled."
                job["result"] = None
                job["error"] = None
            else:
                job["status"] = "completed"
                job["updated_at"] = self._utc_now()
                job["message"] = result.get("message") or "Measurement finished."
                job["result"] = result
                if isinstance(result.get("calibration"), dict):
                    job["calibration"] = deepcopy(result["calibration"])
                job["error"] = None
        except asyncio.CancelledError:
            job["status"] = "cancelled"
            job["updated_at"] = self._utc_now()
            job["message"] = "Measurement cancelled."
            job["result"] = None
            job["error"] = None
        except Exception as exc:
            if job_id in self._cancelled_jobs:
                job["status"] = "cancelled"
                job["updated_at"] = self._utc_now()
                job["message"] = "Measurement cancelled."
                job["result"] = None
                job["error"] = None
            else:
                job["status"] = "failed"
                job["updated_at"] = self._utc_now()
                job["message"] = str(exc) or "Measurement failed"
                job["result"] = None
                job["error"] = {"detail": str(exc)}
        finally:
            self._job_processes.pop(job_id, None)
            self._persist_job(job)

    def _execute_capture_job(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = str(job["id"])
        selected_input = job.get("input") or {}
        channel = str(job.get("channel") or "left")
        calibration_meta = job.get("calibration") if isinstance(job.get("calibration"), dict) else {"filename": "", "applied": False}

        sample_rate = self._resolve_measurement_sample_rate()
        sweep_seconds = SWEEP_V2_SECONDS
        lead_in_seconds = SWEEP_V2_LEAD_IN_SECONDS
        tail_seconds = SWEEP_V2_TAIL_SECONDS
        duration_seconds = lead_in_seconds + sweep_seconds + tail_seconds
        record_preroll_seconds = HOST_SWEEP_RECORD_PREROLL_SECONDS
        record_postroll_seconds = HOST_SWEEP_RECORD_POSTROLL_SECONDS
        record_duration_seconds = duration_seconds + record_preroll_seconds + record_postroll_seconds
        capture_channels = 2
        capture_path = self.captures_dir / f"{job_id}.wav"
        playback_path = self.playbacks_dir / f"{job_id}.wav"
        source_node_name = str(selected_input.get("node_name") or "").strip()
        if not source_node_name:
            raise RuntimeError("Selected capture input has no usable PipeWire source node")
        if source_node_name == "easyeffects_source" or source_node_name.endswith(".monitor"):
            raise RuntimeError("Refusing to measure through a non-microphone source; select a real PipeWire input")
        if channel == "stereo":
            raise RuntimeError("Host-reference capture currently requires a left or right speaker measurement")

        playback_target = self._resolve_playback_target()
        host_reference = self._resolve_host_reference_capture(
            playback_target=playback_target,
            mic_source_node_name=source_node_name,
            requested_channel=channel,
        )
        sweep_meta = self._write_sweep_file(
            playback_path,
            sample_rate=sample_rate,
            sweep_seconds=sweep_seconds,
            lead_in_seconds=lead_in_seconds,
            tail_seconds=tail_seconds,
            channel=channel,
        )

        calibration_curve = None
        calibration_applied = False
        if calibration_meta.get("path"):
            calibration_curve = self._parse_calibration_file(Path(calibration_meta["path"]))
            calibration_applied = calibration_curve is not None and len(calibration_curve[0]) >= 2
        calibration_result = {
            "filename": str(calibration_meta.get("filename") or ""),
            "path": str(calibration_meta.get("path") or ""),
            "applied": calibration_applied,
        }

        analysis = None
        capture_info = None
        playback_info = None
        attempts_used = 0
        final_capture_level_low = False
        mic_auto_boosted = False
        for attempt_index in range(HOST_SWEEP_MAX_ATTEMPTS):
            attempts_used = attempt_index + 1
            try:
                if capture_path.exists():
                    capture_path.unlink()
                analysis, capture_info, playback_info = self._run_host_capture_attempt(
                    job_id=job_id,
                    mic_source_node_name=source_node_name,
                    reference_capture=host_reference,
                    channel=channel,
                    capture_channels=capture_channels,
                    capture_path=capture_path,
                    playback_path=playback_path,
                    playback_target=playback_target,
                    sweep_meta=sweep_meta,
                    sample_rate=sample_rate,
                    duration_seconds=duration_seconds,
                    sweep_seconds=sweep_seconds,
                    lead_in_seconds=lead_in_seconds,
                    tail_seconds=tail_seconds,
                    record_preroll_seconds=record_preroll_seconds,
                    record_postroll_seconds=record_postroll_seconds,
                    record_duration_seconds=record_duration_seconds,
                    calibration_curve=calibration_curve,
                )
                capture_level_low = self._analysis_has_warning_code(analysis, "capture-level-low")
                final_capture_level_low = capture_level_low
                if (
                    capture_level_low
                    and not mic_auto_boosted
                    and attempt_index == HOST_SWEEP_AUTO_GAIN_RETRY_ATTEMPT - 1
                ):
                    mic_target = str(selected_input.get("node_serial") or source_node_name).strip()
                    if mic_target:
                        try:
                            current_mic_volume = get_node_volume(mic_target)
                        except SystemVolumeError:
                            current_mic_volume = None
                        if isinstance(current_mic_volume, int) and current_mic_volume < HOST_SWEEP_AUTO_GAIN_TARGET_PERCENT:
                            try:
                                set_node_volume(mic_target, HOST_SWEEP_AUTO_GAIN_TARGET_PERCENT)
                            except SystemVolumeError:
                                pass
                            else:
                                mic_auto_boosted = True
                                time.sleep(HOST_SWEEP_RETRY_DELAY_SECONDS)
                                continue
                break
            except Exception as exc:
                if attempt_index >= HOST_SWEEP_MAX_ATTEMPTS - 1 or not self._should_retry_host_capture(exc):
                    raise
                time.sleep(HOST_SWEEP_RETRY_DELAY_SECONDS)
        if analysis is None or capture_info is None or playback_info is None:
            raise RuntimeError("Host-local capture did not produce an analysis result")

        measurement = self._build_measurement_from_analysis(
            analysis,
            input_device={
                "id": str(selected_input.get("id") or "capture-input"),
                "label": str(selected_input.get("label") or "Capture input"),
            },
            channel=channel,
            calibration=calibration_result,
        )
        if mic_auto_boosted and isinstance(capture_info, dict):
            capture_info["mic_auto_boosted"] = True
            capture_info["mic_auto_boost_target_percent"] = HOST_SWEEP_AUTO_GAIN_TARGET_PERCENT

        completion_message = "Measurement finished. Trusted trace is ready."
        if final_capture_level_low:
            completion_message += " Volume was low."

        return {
            "measurement": measurement,
            "calibration": calibration_result,
            "capture": {
                **capture_info,
                "attempts_used": attempts_used,
                "max_attempts": HOST_SWEEP_MAX_ATTEMPTS,
            },
            "playback": playback_info,
            "analysis": {
                "method": analysis["method"],
                "rms_dbfs": analysis["rms_dbfs"],
                "peak_dbfs": analysis["peak_dbfs"],
                "normalized_by_db": analysis["normalized_by_db"],
                "alignment_samples": analysis["alignment_samples"],
                "alignment_seconds": analysis["alignment_seconds"],
                "window_count": analysis["window_count"],
                "trusted_min_hz": analysis["trusted_min_hz"],
                "trusted_max_hz": analysis["trusted_max_hz"],
                "raw_point_count": analysis["raw_point_count"],
                "review_point_count": analysis["review_point_count"],
                "display_point_count": analysis["display_point_count"],
                "trusted_band_meta": analysis["trusted_band_meta"],
                "review_band_meta": analysis["review_band_meta"],
                "quality_checks": analysis["quality_checks"],
                "capture_audit": analysis["capture_audit"],
                "clock": analysis["clock"],
                "reference_path": analysis["reference_path"],
                "impulse_response": analysis["impulse_response"],
            },
            "limitations": [
                "This path is a real host-local sweep playback and capture flow, but it is still a conservative sweep-v3 implementation.",
                "Host-local timing now follows the separate sink-monitor reference capture and applies that offset/drift correction to the mic path; it is still not a full REW feature set.",
                "The displayed trace is intentionally trimmed to the conservative trusted band when the low or high edges remain unstable.",
                "Measurements stay separate from EasyEffects presets and active PEQ state. No Auto-PEQ or Copy-to-PEQ is included here.",
            ],
            "message": completion_message,
            "scope_note": MEASUREMENT_SCOPE_NOTE,
        }

    def _run_host_capture_attempt(
        self,
        *,
        job_id: str,
        mic_source_node_name: str,
        reference_capture: dict[str, Any],
        channel: str,
        capture_channels: int,
        capture_path: Path,
        playback_path: Path,
        playback_target: dict[str, Any],
        sweep_meta: dict[str, Any],
        sample_rate: int,
        duration_seconds: float,
        sweep_seconds: float,
        lead_in_seconds: float,
        tail_seconds: float,
        record_preroll_seconds: float,
        record_postroll_seconds: float,
        record_duration_seconds: float,
        calibration_curve: tuple[np.ndarray, np.ndarray] | None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        record_node_name = f"fxroute-measure-record-{job_id}"
        play_node_name = f"fxroute-measure-play-{job_id}"
        sample_count = int(round(sample_rate * record_duration_seconds))
        record_command = [
            "pw-record",
            "-P",
            "node.autoconnect=false",
            "-P",
            f"node.name={record_node_name}",
            "--target",
            "0",
            "--rate",
            str(sample_rate),
            "--channels",
            str(capture_channels),
            "--format",
            "s16",
        ]
        if self._pw_record_supports_option("--container"):
            record_command.extend(["--container", "wav"])
        if self._pw_record_supports_option("--sample-count"):
            record_command.extend(["--sample-count", str(sample_count)])
        record_command.append(str(capture_path))
        play_command = [
            "pw-play",
            "-P",
            f"node.name={play_node_name}",
            "--target",
            playback_target["target_name"],
            str(playback_path),
        ]

        record_process = subprocess.Popen(record_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self._job_processes[job_id] = [record_process]
        play_process: subprocess.Popen[str] | None = None
        play_stdout = ""
        play_stderr = ""
        play_timed_out = False
        record_stdout = ""
        record_stderr = ""
        try:
            self._link_host_reference_capture(
                reference_source_node_name=str(reference_capture["source_node_name"]),
                mic_source_node_name=mic_source_node_name,
                record_node_name=record_node_name,
                requested_channel=channel,
            )
            time.sleep(record_preroll_seconds)

            play_process = subprocess.Popen(play_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self._job_processes[job_id] = [record_process, play_process]
            try:
                play_stdout, play_stderr = play_process.communicate(timeout=duration_seconds + 8)
            except subprocess.TimeoutExpired:
                play_timed_out = True
                if play_process.poll() is None:
                    play_process.terminate()
                try:
                    play_stdout, play_stderr = play_process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    if play_process.poll() is None:
                        play_process.kill()
                    play_stdout, play_stderr = play_process.communicate(timeout=2)

            if self._pw_record_supports_option("--sample-count"):
                record_stdout, record_stderr = record_process.communicate(timeout=record_duration_seconds + 8)
            else:
                time.sleep(max(0.0, record_postroll_seconds))
                if record_process.poll() is None:
                    record_process.terminate()
                try:
                    record_stdout, record_stderr = record_process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    record_process.kill()
                    record_stdout, record_stderr = record_process.communicate(timeout=2)
        except Exception:
            if play_process is not None and play_process.poll() is None:
                play_process.kill()
                try:
                    play_process.communicate(timeout=2)
                except Exception:
                    pass
            if record_process.poll() is None:
                record_process.kill()
            try:
                record_process.communicate(timeout=2)
            except Exception:
                pass
            raise

        if job_id in self._cancelled_jobs:
            raise RuntimeError("Measurement cancelled.")

        capture_usable = capture_path.exists() and capture_path.stat().st_size > 44
        if play_process is None:
            raise RuntimeError("Sweep playback did not start")
        if play_process.returncode != 0 and not play_timed_out:
            detail = (play_stderr or play_stdout or f"pw-play exited with {play_process.returncode}").strip()
            raise RuntimeError(f"Sweep playback failed: {detail}")
        if record_process.returncode != 0 and not capture_usable:
            detail = (record_stderr or record_stdout or f"pw-record exited with {record_process.returncode}").strip()
            raise RuntimeError(f"Capture failed: {detail}")
        if not capture_usable:
            raise RuntimeError("Capture finished but no usable host-reference WAV data was produced")

        reference_channel_label = str(reference_capture.get("channel_label") or "reference")
        analysis = self._analyze_sweep_capture(
            capture_path,
            expected_sample_rate=sample_rate,
            channel=channel,
            reference_sweep=sweep_meta["analysis_sweep"],
            inverse_sweep=sweep_meta["inverse_sweep"],
            calibration_curve=calibration_curve,
            capture_label="Host-local capture",
            reference_channel_index=0,
            analysis_channel_index=1,
            reference_channel_label=reference_channel_label,
        )
        analysis["method"] = "inverse log-sweep deconvolution with host-reference dual-channel capture"
        analysis_clock = analysis.get("clock") if isinstance(analysis.get("clock"), dict) else {}
        analysis_clock.update(
            {
                "timing_channel": reference_channel_label,
                "reference_capture_mode": "dual-channel",
                "reference_channel": reference_channel_label,
            }
        )
        analysis["clock"] = analysis_clock
        reference_path = analysis.get("reference_path") if isinstance(analysis.get("reference_path"), dict) else {}
        reference_path.update(
            {
                "timing_applied_to_mic": True,
                "capture_mode": "dual-channel",
            }
        )
        analysis["reference_path"] = reference_path
        return (
            analysis,
            {
                "path": str(capture_path),
                "duration_seconds": round(duration_seconds, 3),
                "sample_rate": sample_rate,
                "channels": capture_channels,
                "input_node": mic_source_node_name,
                "microphone_node": mic_source_node_name,
                "reference_node": str(reference_capture.get("source_node_name") or ""),
                "reference_channel": str(reference_capture.get("channel_label") or "reference"),
                "reference_path": str(capture_path),
                "record_node": record_node_name,
            },
            {
                "path": str(playback_path),
                "duration_seconds": round(duration_seconds, 3),
                "sweep_seconds": round(sweep_seconds, 3),
                "lead_in_seconds": round(lead_in_seconds, 3),
                "tail_seconds": round(tail_seconds, 3),
                "play_node": play_node_name,
                "target_name": playback_target["target_name"],
                "target_label": playback_target["target_label"],
                "timed_out": bool(play_timed_out),
            },
        )

    def _capture_quality_error_codes(self, exc: Exception) -> set[str]:
        if not isinstance(exc, CaptureQualityError):
            return set()
        error_codes = {str(item.get("code") or "").strip() for item in exc.items if item.get("level") == "error"}
        error_codes.discard("")
        return error_codes

    def _should_retry_host_capture(self, exc: Exception) -> bool:
        error_codes = self._capture_quality_error_codes(exc)
        return bool(error_codes) and error_codes.issubset({"weak-start-alignment", "weak-end-alignment"})

    def _analysis_has_warning_code(self, analysis: dict[str, Any] | None, code: str) -> bool:
        if not isinstance(analysis, dict):
            return False
        items = ((analysis.get("quality_checks") or {}).get("items") or [])
        return any(str(item.get("code") or "").strip() == code and item.get("level") == "warning" for item in items)

    def _should_retry_browser_capture(self, exc: Exception) -> bool:
        error_codes = self._capture_quality_error_codes(exc)
        retryable = {
            "weak-start-alignment",
            "weak-end-alignment",
            "insufficient-sync-bursts",
            "sync-cluster-a-insufficient",
            "sync-cluster-b-insufficient",
            "sync-order-invalid",
            "sync-fit-residual-high",
            "sync-burst-residual-high",
            "corrected-sweep-weak",
            "browser-clock-drift-excessive",
        }
        return bool(error_codes) and error_codes.issubset(retryable)

    async def _run_browser_playback_job(self, job_id: str) -> None:
        job = self._jobs[job_id]
        job["status"] = "recording"
        job["updated_at"] = self._utc_now()
        job["message"] = "Browser microphone recording armed. FXRoute will play the sweep shortly."
        self._persist_job(job)
        try:
            result = await asyncio.to_thread(self._execute_browser_playback_job, deepcopy(job))
            job["status"] = "awaiting-upload"
            job["updated_at"] = self._utc_now()
            job["message"] = "Sweep finished on FXRoute. Uploading browser capture…"
            job["playback_result"] = result
            job["error"] = None
        except Exception as exc:
            job["status"] = "failed"
            job["updated_at"] = self._utc_now()
            job["message"] = str(exc) or "Browser sweep playback failed"
            job["error"] = {"detail": str(exc)}
        finally:
            self._persist_job(job)

    def _execute_browser_playback_job(self, job: dict[str, Any]) -> dict[str, Any]:
        playback = job.get("playback") or {}
        playback_path = Path(str(playback.get("path") or "")).expanduser()
        target_name = str(playback.get("target_name") or "").strip()
        if not playback_path.exists():
            raise RuntimeError("Browser playback sweep file is missing")
        if not target_name:
            raise RuntimeError("No active output target is available for browser sweep playback")

        original_output_volume = None
        pinned_output_volume = None
        try:
            original_output_volume = get_output_volume()
            pinned_output_volume = set_output_volume(100)
        except SystemVolumeError:
            original_output_volume = None
            pinned_output_volume = None

        play_node_name = f"fxroute-browser-play-{job['id']}"
        play_command = [
            "pw-play",
            "-P",
            f"node.name={play_node_name}",
            "--target",
            target_name,
            str(playback_path),
        ]

        time.sleep(BROWSER_SWEEP_START_DELAY_SECONDS)
        play_process = subprocess.Popen(play_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        duration_seconds = float(playback.get("duration_seconds") or 0)
        try:
            play_stdout, play_stderr = play_process.communicate(timeout=max(duration_seconds, 1.0) + 8)
        finally:
            if play_process.poll() is None:
                play_process.kill()
                play_process.communicate(timeout=2)
            if original_output_volume is not None:
                try:
                    set_output_volume(original_output_volume)
                except SystemVolumeError:
                    pass
        if play_process.returncode != 0:
            detail = (play_stderr or play_stdout or f"pw-play exited with {play_process.returncode}").strip()
            raise RuntimeError(f"Sweep playback failed: {detail}")
        return {
            "play_node": play_node_name,
            "target_name": target_name,
            "target_label": str(playback.get("target_label") or target_name),
            "original_output_volume": original_output_volume,
            "pinned_output_volume": pinned_output_volume,
        }

    async def complete_browser_measurement(
        self,
        *,
        job_id: str,
        capture_filename: str,
        capture_bytes: bytes,
        browser_input_label: str | None = None,
        browser_capture_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        job = self._jobs.get(job_id)
        if job is None:
            path = self.job_records_dir / f"{job_id}.json"
            if not path.exists():
                raise KeyError(job_id)
            job = json.loads(path.read_text(encoding="utf-8"))
            self._jobs[job_id] = job
        if job.get("mode") != "browser-microphone":
            raise ValueError("Measurement job is not a browser microphone job")
        if job.get("status") == "failed":
            raise RuntimeError(str((job.get("error") or {}).get("detail") or job.get("message") or "Measurement failed"))
        if job.get("status") == "completed":
            return self.get_job(job_id)
        if not capture_bytes:
            raise ValueError("Browser capture file is required")

        task = self._job_tasks.get(job_id)
        if task is not None:
            await task
            if job.get("status") == "failed":
                raise RuntimeError(str((job.get("error") or {}).get("detail") or job.get("message") or "Measurement failed"))

        capture_path = self.captures_dir / f"{job_id}-{self._safe_filename(capture_filename or 'browser-capture.wav')}"
        capture_path.write_bytes(capture_bytes)

        playback_path_value = str((job.get("playback") or {}).get("path") or "").strip()
        playback_path = Path(playback_path_value) if playback_path_value else None
        playback_leak = self._detect_browser_capture_playback_leak(capture_path, playback_path)
        if playback_leak is not None:
            correlation = float(playback_leak.get("correlation") or 0.0)
            residual_db = float(playback_leak.get("residual_db") or 0.0)
            raise RuntimeError(
                "Browser capture matches the generated playback sweep too closely "
                f"(corr {correlation:.6f}, residual {residual_db:.1f} dB). "
                "The uploaded file looks like the sweep stimulus, not a microphone recording."
            )

        calibration_meta = job.get("calibration") if isinstance(job.get("calibration"), dict) else {"filename": "", "applied": False}
        calibration_curve = None
        calibration_applied = False
        if calibration_meta.get("path"):
            calibration_curve = self._parse_calibration_file(Path(calibration_meta["path"]))
            calibration_applied = calibration_curve is not None and len(calibration_curve[0]) >= 2
        calibration_result = {
            "filename": str(calibration_meta.get("filename") or ""),
            "path": str(calibration_meta.get("path") or ""),
            "applied": calibration_applied,
        }

        analysis_reference = job.get("analysis_reference") or {}
        try:
            try:
                analysis_mode = str(analysis_reference.get("mode") or "")
                if analysis_mode == BROWSER_REFERENCE_MODE_V2:
                    analysis = self._analyze_browser_sync_scaffold_capture(
                        capture_path,
                        expected_sample_rate=int(analysis_reference.get("sample_rate") or 48_000),
                        channel=str(job.get("channel") or "left"),
                        reference_sweep=np.array(analysis_reference.get("analysis_sweep") or [], dtype=np.float32),
                        inverse_sweep=np.array(analysis_reference.get("inverse_sweep") or [], dtype=np.float64),
                        sync_bursts=analysis_reference.get("sync_bursts") or [],
                        emit=analysis_reference.get("emit") or {},
                        calibration_curve=calibration_curve,
                        browser_capture_meta=browser_capture_meta,
                        capture_label="Browser capture",
                    )
                elif analysis_mode == "browser-acoustic-reference-v1":
                    analysis = self._analyze_browser_acoustic_reference_capture(
                        capture_path,
                        expected_sample_rate=int(analysis_reference.get("sample_rate") or 48_000),
                        channel=str(job.get("channel") or "left"),
                        reference_sweep=np.array(analysis_reference.get("analysis_sweep") or [], dtype=np.float32),
                        inverse_sweep=np.array(analysis_reference.get("inverse_sweep") or [], dtype=np.float64),
                        marker_a=np.array(analysis_reference.get("marker_a") or [], dtype=np.float32),
                        marker_b=np.array(analysis_reference.get("marker_b") or [], dtype=np.float32),
                        emit=analysis_reference.get("emit") or {},
                        calibration_curve=calibration_curve,
                        browser_capture_meta=browser_capture_meta,
                        capture_label="Browser capture",
                    )
                else:
                    analysis = self._analyze_sweep_capture(
                        capture_path,
                        expected_sample_rate=int(analysis_reference.get("sample_rate") or 48_000),
                        channel=str(job.get("channel") or "left"),
                        reference_sweep=np.array(analysis_reference.get("analysis_sweep") or [], dtype=np.float32),
                        inverse_sweep=np.array(analysis_reference.get("inverse_sweep") or [], dtype=np.float64),
                        calibration_curve=calibration_curve,
                        browser_capture_meta=browser_capture_meta,
                        capture_label="Browser capture",
                    )
            except CaptureQualityError as exc:
                if not self._should_retry_browser_capture(exc) or not isinstance(exc.analysis, dict):
                    raise
                analysis = exc.analysis

            measurement = self._build_measurement_from_analysis(
                analysis,
                input_device={
                    "id": "browser-client-microphone",
                    "label": str(browser_input_label or "Browser microphone"),
                },
                channel=str(job.get("channel") or "left"),
                calibration=calibration_result,
            )
            warning_count = sum(1 for item in analysis.get("quality_checks", {}).get("items", []) if item.get("level") == "warning")
            error_count = sum(1 for item in analysis.get("quality_checks", {}).get("items", []) if item.get("level") == "error")
            completion_message = "Browser microphone measurement finished. Trusted trace is ready."
            if error_count:
                completion_message = "Browser microphone measurement finished, but capture timing was unstable. This run should be retried automatically."
            elif warning_count:
                completion_message = f"Browser microphone measurement finished with {warning_count} QC warning{'s' if warning_count != 1 else ''}."

            playback = job.get("playback") or {}
            playback_result = job.get("playback_result") or {}
            job["status"] = "completed"
            job["updated_at"] = self._utc_now()
            job["message"] = completion_message
            job["result"] = {
                "measurement": measurement,
                "calibration": calibration_result,
                "capture": {
                    "path": str(capture_path),
                    "duration_seconds": round(float((job.get("browser_capture") or {}).get("record_seconds") or 0), 3),
                    "sample_rate": int(analysis_reference.get("sample_rate") or 48_000),
                    "input_label": str(browser_input_label or "Browser microphone"),
                },
                "playback": {
                    "path": str(playback.get("path") or ""),
                    "duration_seconds": round(float(playback.get("duration_seconds") or 0), 3),
                    "sweep_seconds": round(float(playback.get("sweep_seconds") or 0), 3),
                    "lead_in_seconds": round(float(playback.get("lead_in_seconds") or 0), 3),
                    "tail_seconds": round(float(playback.get("tail_seconds") or 0), 3),
                    "play_node": str(playback_result.get("play_node") or ""),
                    "target_name": str(playback.get("target_name") or ""),
                    "target_label": str(playback.get("target_label") or ""),
                },
                "analysis": {
                    "method": analysis["method"],
                    "rms_dbfs": analysis["rms_dbfs"],
                    "peak_dbfs": analysis["peak_dbfs"],
                    "normalized_by_db": analysis["normalized_by_db"],
                    "alignment_samples": analysis["alignment_samples"],
                    "alignment_seconds": analysis["alignment_seconds"],
                    "window_count": analysis["window_count"],
                    "trusted_min_hz": analysis["trusted_min_hz"],
                    "trusted_max_hz": analysis["trusted_max_hz"],
                    "raw_point_count": analysis["raw_point_count"],
                    "review_point_count": analysis["review_point_count"],
                    "display_point_count": analysis["display_point_count"],
                    "trusted_band_meta": analysis["trusted_band_meta"],
                    "review_band_meta": analysis["review_band_meta"],
                    "quality_checks": analysis["quality_checks"],
                    "capture_audit": analysis["capture_audit"],
                    "clock": analysis["clock"],
                    "impulse_response": analysis["impulse_response"],
                },
                "limitations": [
                    "Primary browser path: the client browser records its own microphone while FXRoute plays the sweep on the active output.",
                    "The client microphone, browser audio stack, and clock drift all affect the result; compare against REW before trusting fine corrections.",
                    "Host-local .104 capture still exists as a secondary route when you want on-box PipeWire recording instead.",
                    "Measurements stay separate from EasyEffects presets and active PEQ state. No Auto-PEQ or Copy-to-PEQ is included here.",
                ],
                "message": completion_message,
                "scope_note": MEASUREMENT_SCOPE_NOTE,
            }
            job["calibration"] = deepcopy(calibration_result)
            job["error"] = None
            self._persist_job(job)
            return self.get_job(job_id)
        except Exception as exc:
            job["status"] = "failed"
            job["updated_at"] = self._utc_now()
            job["message"] = str(exc) or "Browser capture analysis failed"
            job["error"] = {"detail": str(exc)}
            self._persist_job(job)
            raise

    def _build_measurement_from_analysis(
        self,
        analysis: dict[str, Any],
        *,
        input_device: dict[str, str],
        channel: str,
        calibration: dict[str, Any],
    ) -> dict[str, Any]:
        timestamp = datetime.now(timezone.utc).replace(microsecond=0)
        created_at = timestamp.isoformat().replace("+00:00", "Z")
        label = f"Current sweep {timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}"
        payload = {
            "id": f"sweep-{timestamp.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}",
            "name": label,
            "created_at": created_at,
            "input_device": input_device,
            "channel": channel,
            "calibration": calibration,
            "display": deepcopy(DISPLAY_DEFAULTS),
            "traces": [
                {
                    "kind": "sweep-response",
                    "label": f"{label} · trusted",
                    "color": TRACE_COLORS[0],
                    "role": "trusted",
                    "points": analysis["trusted_points"],
                }
            ],
            "review_traces": [
                {
                    "kind": "sweep-response-review",
                    "label": f"{label} · raw/full-band review",
                    "color": TRACE_COLORS[1],
                    "role": "raw-review",
                    "points": analysis["review_points"],
                }
            ],
            "measurement_kind": "sweep-response-v3",
            "notes": [
                MEASUREMENT_SCOPE_NOTE,
                "Trusted trace stays conservative for the normal measurement UX. Raw/full-band review trace is separate and can include edge regions excluded from the trusted band.",
            ] + [item["message"] for item in analysis.get("quality_checks", {}).get("items", []) if item.get("level") == "warning"],
            "analysis": {
                "method": analysis["method"],
                "rms_dbfs": analysis["rms_dbfs"],
                "peak_dbfs": analysis["peak_dbfs"],
                "window_count": analysis["window_count"],
                "normalized_by_db": analysis["normalized_by_db"],
                "alignment_samples": analysis["alignment_samples"],
                "alignment_seconds": analysis["alignment_seconds"],
                "trusted_min_hz": analysis["trusted_min_hz"],
                "trusted_max_hz": analysis["trusted_max_hz"],
                "raw_point_count": analysis["raw_point_count"],
                "review_point_count": analysis["review_point_count"],
                "display_point_count": analysis["display_point_count"],
                "trusted_band_meta": analysis["trusted_band_meta"],
                "review_band_meta": analysis["review_band_meta"],
                "quality_checks": analysis["quality_checks"],
                "capture_audit": analysis["capture_audit"],
                "clock": analysis["clock"],
                "impulse_response": analysis["impulse_response"],
            },
        }
        return self._normalize_measurement(payload)

    def _analyze_sweep_capture(
        self,
        capture_path: Path,
        *,
        expected_sample_rate: int,
        channel: str,
        reference_sweep: np.ndarray,
        inverse_sweep: np.ndarray,
        calibration_curve: tuple[np.ndarray, np.ndarray] | None,
        browser_capture_meta: dict[str, Any] | None = None,
        capture_label: str = "Capture",
        reference_channel_index: int | None = None,
        analysis_channel_index: int | None = None,
        reference_channel_label: str = "reference",
        timing_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        sample_rate, raw_signal = self._load_wav_array(capture_path)
        signal = self._select_analysis_channel(raw_signal, channel=channel, channel_index=analysis_channel_index)
        timing_signal = self._select_analysis_channel(raw_signal, channel=channel, channel_index=reference_channel_index)
        if sample_rate != expected_sample_rate:
            raise RuntimeError(f"Unexpected capture sample rate: {sample_rate} Hz (expected {expected_sample_rate} Hz)")
        if signal.size < reference_sweep.size or timing_signal.size < reference_sweep.size:
            raise RuntimeError("Capture is too short for sweep analysis")

        rms = float(np.sqrt(np.mean(np.square(signal, dtype=np.float64))))
        peak = float(np.max(np.abs(signal)))
        rms_dbfs = 20.0 * math.log10(max(rms, 1e-9))
        peak_dbfs = 20.0 * math.log10(max(peak, 1e-9))
        if peak_dbfs <= -90.0 and rms_dbfs <= -100.0:
            raise RuntimeError("Recorded sweep was effectively silent")

        reference_rms = float(np.sqrt(np.mean(np.square(timing_signal, dtype=np.float64))))
        reference_peak = float(np.max(np.abs(timing_signal)))
        reference_rms_dbfs = 20.0 * math.log10(max(reference_rms, 1e-9))
        reference_peak_dbfs = 20.0 * math.log10(max(reference_peak, 1e-9))
        if reference_peak_dbfs <= -90.0 and reference_rms_dbfs <= -100.0:
            raise RuntimeError(f"{reference_channel_label.capitalize()} channel was effectively silent")

        if timing_override is None:
            coarse_start = self._find_sweep_start(timing_signal, reference_sweep)
            timing = self._estimate_sweep_timing(
                timing_signal,
                reference_sweep,
                coarse_start,
                sample_rate,
                browser_capture=browser_capture_meta is not None,
            )
        else:
            timing = {
                "aligned_start": int(timing_override["alignment_samples"]),
                "aligned_end": int(timing_override["alignment_samples"] + timing_override["observed_sweep_samples"]),
                "observed_sweep_samples": int(timing_override["observed_sweep_samples"]),
                "stretch_ratio": float(timing_override.get("stretch_ratio") or 1.0),
                "drift_ppm": float(timing_override.get("drift_ppm") or 0.0),
                "compensated": bool(timing_override.get("compensated")),
                "anchor_seconds": float(timing_override.get("anchor_seconds") or 0.0),
                "start_score": float(timing_override.get("start_score") or 0.0),
                "end_score": float(timing_override.get("end_score") or 0.0),
                "anchor_strategy": str(timing_override.get("anchor_strategy") or "timing override"),
                "anchor_matches": deepcopy(timing_override.get("anchor_matches") or []),
            }
        aligned_start = int(timing["aligned_start"])
        aligned_end = int(timing["aligned_end"])
        if aligned_end > signal.size or aligned_end > timing_signal.size:
            raise RuntimeError("Aligned sweep window exceeded recorded capture")

        analysis_segment = signal[aligned_start:].astype(np.float64)
        reference_segment = timing_signal[aligned_start:].astype(np.float64)
        if analysis_segment.size < max(2048, reference_sweep.size // 4):
            raise RuntimeError("Aligned sweep segment was too short after timing estimation")
        stretch_ratio = float(timing.get("stretch_ratio") or 1.0)
        corrected_segment_size = max(reference_sweep.size, int(round(analysis_segment.size / max(stretch_ratio, 1e-9))))
        corrected_segment = self._resample_signal(analysis_segment, corrected_segment_size)
        corrected_reference_segment = self._resample_signal(reference_segment, corrected_segment_size)
        captured_tail_samples = max(0, analysis_segment.size - int(timing["observed_sweep_samples"]))

        impulse_response = self._fft_convolve(corrected_segment, inverse_sweep.astype(np.float64))
        reference_impulse_response = self._fft_convolve(corrected_reference_segment, inverse_sweep.astype(np.float64))
        windowed_ir, ir_meta = self._window_impulse_response(impulse_response, sample_rate)
        reference_ir_peak = float(np.max(np.abs(reference_impulse_response))) if reference_impulse_response.size else 0.0
        reference_ir_rms = float(np.sqrt(np.mean(np.square(reference_impulse_response, dtype=np.float64)))) if reference_impulse_response.size else 0.0
        reference_ir_peak_db = 20.0 * math.log10(max(reference_ir_peak, 1e-9))
        reference_ir_rms_db = 20.0 * math.log10(max(reference_ir_rms, 1e-9))
        reference_ir_sharpness_db = reference_ir_peak_db - reference_ir_rms_db
        fft_size = self._next_pow2(max(sample_rate, windowed_ir.size * 2))
        magnitude = np.abs(np.fft.rfft(windowed_ir, n=fft_size))
        frequencies = np.fft.rfftfreq(fft_size, d=1.0 / sample_rate)

        display_data = self._build_display_points(
            frequencies=frequencies,
            magnitude=magnitude,
            calibration_curve=calibration_curve,
            browser_capture=browser_capture_meta is not None,
        )
        capture_audit = self._build_capture_audit(
            raw_signal=raw_signal,
            sample_rate=sample_rate,
            browser_capture_meta=browser_capture_meta,
        )
        quality_checks = self._build_capture_quality_checks(
            capture_audit=capture_audit,
            timing=timing,
            peak_dbfs=peak_dbfs,
            trusted_band_meta=display_data["trusted_band_meta"],
            trusted_max_hz=display_data["trusted_band"][1],
            response_outliers=display_data.get("response_outliers") or [],
            capture_label=capture_label,
            expect_dual_mono_channels=reference_channel_index is None,
        )
        analysis = {
            "method": "inverse log-sweep deconvolution with anchor timing compensation and IR windowing",
            "trusted_points": display_data["trusted_points"],
            "review_points": display_data["review_points"],
            "normalized_by_db": round(display_data["normalized_by"], 3),
            "rms_dbfs": round(rms_dbfs, 2),
            "peak_dbfs": round(peak_dbfs, 2),
            "window_count": 1,
            "alignment_samples": int(aligned_start),
            "alignment_seconds": round(aligned_start / sample_rate, 6),
            "trusted_min_hz": round(display_data["trusted_band"][0], 3),
            "trusted_max_hz": round(display_data["trusted_band"][1], 3),
            "raw_point_count": display_data["raw_point_count"],
            "review_point_count": len(display_data["review_points"]),
            "display_point_count": len(display_data["trusted_points"]),
            "trusted_band_meta": display_data["trusted_band_meta"],
            "review_band_meta": display_data["review_band_meta"],
            "quality_checks": quality_checks,
            "capture_audit": capture_audit,
            "clock": {
                "observed_sweep_samples": int(timing["observed_sweep_samples"]),
                "reference_sweep_samples": int(reference_sweep.size),
                "analysis_segment_samples": int(analysis_segment.size),
                "corrected_segment_samples": int(corrected_segment.size),
                "captured_tail_samples": int(captured_tail_samples),
                "captured_tail_seconds": round(float(captured_tail_samples) / sample_rate, 6),
                "stretch_ratio": round(float(timing["stretch_ratio"]), 8),
                "drift_ppm": round(float(timing["drift_ppm"]), 2),
                "compensated": bool(timing["compensated"]),
                "anchor_seconds": round(float(timing["anchor_seconds"]), 4),
                "anchor_strategy": str(timing.get("anchor_strategy") or "edge anchors"),
                "anchor_matches": timing.get("anchor_matches") or [],
                "start_score": round(float(timing["start_score"]), 5),
                "end_score": round(float(timing["end_score"]), 5),
                "timing_channel": reference_channel_label,
            },
            "reference_path": {
                "channel": reference_channel_label,
                "peak_dbfs": round(reference_peak_dbfs, 2),
                "rms_dbfs": round(reference_rms_dbfs, 2),
                "alignment_score": round(min(float(timing["start_score"]), float(timing["end_score"])), 5),
                "start_score": round(float(timing["start_score"]), 5),
                "end_score": round(float(timing["end_score"]), 5),
                "drift_ppm": round(float(timing["drift_ppm"]), 2),
                "ir_peak_dbfs": round(reference_ir_peak_db, 2),
                "ir_sharpness_db": round(reference_ir_sharpness_db, 2),
                "clipped": bool(reference_peak_dbfs >= CAPTURE_CLIP_FAIL_DBFS),
            },
            "impulse_response": {
                "peak_index": int(ir_meta["peak_index"]),
                "peak_seconds": round(float(ir_meta["peak_seconds"]), 6),
                "window_start_index": int(ir_meta["window_start_index"]),
                "window_end_index": int(ir_meta["window_end_index"]),
                "window_seconds": round(float(ir_meta["window_seconds"]), 6),
                "pre_window_seconds": round(float(ir_meta["pre_window_seconds"]), 6),
                "post_window_seconds": round(float(ir_meta["post_window_seconds"]), 6),
                "peak_dbfs": round(float(ir_meta["peak_dbfs"]), 2),
            },
        }
        hard_failures = [item["message"] for item in quality_checks["items"] if item.get("level") == "error"]
        if hard_failures:
            raise CaptureQualityError(capture_label, quality_checks["items"], analysis=analysis)
        return analysis

    def _analyze_browser_acoustic_reference_capture(
        self,
        capture_path: Path,
        *,
        expected_sample_rate: int,
        channel: str,
        reference_sweep: np.ndarray,
        inverse_sweep: np.ndarray,
        marker_a: np.ndarray,
        marker_b: np.ndarray,
        emit: dict[str, Any],
        calibration_curve: tuple[np.ndarray, np.ndarray] | None,
        browser_capture_meta: dict[str, Any] | None = None,
        capture_label: str = "Browser capture",
    ) -> dict[str, Any]:
        sample_rate, raw_signal = self._load_wav_array(capture_path)
        signal = self._select_analysis_channel(raw_signal, channel=channel)
        timing_signal = self._select_analysis_channel(raw_signal, channel=channel)
        if sample_rate != expected_sample_rate:
            raise RuntimeError(f"Unexpected capture sample rate: {sample_rate} Hz (expected {expected_sample_rate} Hz)")
        if signal.size < max(marker_a.size, marker_b.size, reference_sweep.size):
            raise RuntimeError("Capture is too short for browser acoustic-reference analysis")

        rms = float(np.sqrt(np.mean(np.square(signal, dtype=np.float64))))
        peak = float(np.max(np.abs(signal)))
        rms_dbfs = 20.0 * math.log10(max(rms, 1e-9))
        peak_dbfs = 20.0 * math.log10(max(peak, 1e-9))
        if peak_dbfs <= -90.0 and rms_dbfs <= -100.0:
            raise RuntimeError("Recorded sweep was effectively silent")

        marker_a_match = self._detect_browser_timing_marker(timing_signal, marker_a, sample_rate)
        marker_b_match = self._detect_browser_timing_marker(timing_signal, marker_b, sample_rate)
        timing = self._estimate_browser_reference_timing(
            signal=signal,
            timing_signal=timing_signal,
            reference_sweep=reference_sweep,
            marker_a=marker_a,
            marker_b=marker_b,
            marker_a_match=marker_a_match,
            marker_b_match=marker_b_match,
            emit=emit,
            sample_rate=sample_rate,
        )

        corrected_signal = timing["corrected_signal"]
        corrected_timing_signal = timing["corrected_timing_signal"]
        sweep_start = int(timing["sweep_start_emit"])
        analysis_segment = corrected_signal[sweep_start:].astype(np.float64)
        reference_segment = corrected_timing_signal[sweep_start:].astype(np.float64)
        if analysis_segment.size < max(2048, reference_sweep.size // 4):
            raise RuntimeError("Corrected browser sweep segment was too short after marker timing estimation")
        corrected_segment_size = analysis_segment.size
        captured_tail_samples = max(0, analysis_segment.size - int(reference_sweep.size))

        impulse_response = self._fft_convolve(analysis_segment, inverse_sweep.astype(np.float64))
        reference_impulse_response = self._fft_convolve(reference_segment, inverse_sweep.astype(np.float64))
        windowed_ir, ir_meta = self._window_impulse_response(impulse_response, sample_rate)
        reference_ir_peak = float(np.max(np.abs(reference_impulse_response))) if reference_impulse_response.size else 0.0
        reference_ir_rms = float(np.sqrt(np.mean(np.square(reference_impulse_response, dtype=np.float64)))) if reference_impulse_response.size else 0.0
        reference_ir_peak_db = 20.0 * math.log10(max(reference_ir_peak, 1e-9))
        reference_ir_rms_db = 20.0 * math.log10(max(reference_ir_rms, 1e-9))
        reference_ir_sharpness_db = reference_ir_peak_db - reference_ir_rms_db
        fft_size = self._next_pow2(max(sample_rate, windowed_ir.size * 2))
        magnitude = np.abs(np.fft.rfft(windowed_ir, n=fft_size))
        frequencies = np.fft.rfftfreq(fft_size, d=1.0 / sample_rate)

        display_data = self._build_display_points(
            frequencies=frequencies,
            magnitude=magnitude,
            calibration_curve=calibration_curve,
            browser_capture=True,
        )
        capture_audit = self._build_capture_audit(
            raw_signal=raw_signal,
            sample_rate=sample_rate,
            browser_capture_meta=browser_capture_meta,
        )
        quality_checks = self._build_capture_quality_checks(
            capture_audit=capture_audit,
            timing=timing,
            peak_dbfs=peak_dbfs,
            trusted_band_meta=display_data["trusted_band_meta"],
            trusted_max_hz=display_data["trusted_band"][1],
            response_outliers=display_data.get("response_outliers") or [],
            capture_label=capture_label,
            expect_dual_mono_channels=True,
        )
        analysis = {
            "method": "inverse log-sweep deconvolution with browser acoustic-reference marker timing correction",
            "trusted_points": display_data["trusted_points"],
            "review_points": display_data["review_points"],
            "normalized_by_db": round(display_data["normalized_by"], 3),
            "rms_dbfs": round(rms_dbfs, 2),
            "peak_dbfs": round(peak_dbfs, 2),
            "window_count": 1,
            "alignment_samples": int(sweep_start),
            "alignment_seconds": round(sweep_start / sample_rate, 6),
            "trusted_min_hz": round(display_data["trusted_band"][0], 3),
            "trusted_max_hz": round(display_data["trusted_band"][1], 3),
            "raw_point_count": display_data["raw_point_count"],
            "review_point_count": len(display_data["review_points"]),
            "display_point_count": len(display_data["trusted_points"]),
            "trusted_band_meta": display_data["trusted_band_meta"],
            "review_band_meta": display_data["review_band_meta"],
            "quality_checks": quality_checks,
            "capture_audit": capture_audit,
            "clock": {
                "observed_sweep_samples": int(timing["observed_sweep_samples"]),
                "reference_sweep_samples": int(reference_sweep.size),
                "analysis_segment_samples": int(analysis_segment.size),
                "corrected_segment_samples": int(corrected_segment_size),
                "captured_tail_samples": int(captured_tail_samples),
                "captured_tail_seconds": round(float(captured_tail_samples) / sample_rate, 6),
                "stretch_ratio": round(float(timing["stretch_ratio"]), 8),
                "drift_ppm": round(float(timing["drift_ppm"]), 2),
                "compensated": True,
                "anchor_seconds": 0.7,
                "anchor_strategy": "marker-a-b affine fit",
                "anchor_matches": timing["marker_matches"],
                "start_score": round(float(timing["corrected_sweep_score"]), 5),
                "end_score": round(float(timing["corrected_sweep_score"]), 5),
                "timing_channel": "browser-acoustic-reference",
                "marker_a_score": round(float(marker_a_match["score"]), 5),
                "marker_a_peak_ratio": round(float(marker_a_match["peak_ratio"]), 5),
                "marker_a_width_ms": round(float(marker_a_match["width_ms"]), 3),
                "marker_b_score": round(float(marker_b_match["score"]), 5),
                "marker_b_peak_ratio": round(float(marker_b_match["peak_ratio"]), 5),
                "marker_b_width_ms": round(float(marker_b_match["width_ms"]), 3),
                "marker_fit_residual_ms": round(float(timing["marker_fit_residual_ms"]), 3),
                "marker_spacing_error_ms": round(float(timing["marker_spacing_error_ms"]), 3),
                "corrected_sweep_score": round(float(timing["corrected_sweep_score"]), 5),
                "emit": timing["emit"],
            },
            "reference_path": {
                "channel": "browser-acoustic-reference",
                "peak_dbfs": round(peak_dbfs, 2),
                "rms_dbfs": round(rms_dbfs, 2),
                "alignment_score": round(float(timing["corrected_sweep_score"]), 5),
                "start_score": round(float(timing["corrected_sweep_score"]), 5),
                "end_score": round(float(timing["corrected_sweep_score"]), 5),
                "drift_ppm": round(float(timing["drift_ppm"]), 2),
                "ir_peak_dbfs": round(reference_ir_peak_db, 2),
                "ir_sharpness_db": round(reference_ir_sharpness_db, 2),
                "clipped": bool(peak_dbfs >= CAPTURE_CLIP_FAIL_DBFS),
            },
            "impulse_response": {
                "peak_index": int(ir_meta["peak_index"]),
                "peak_seconds": round(float(ir_meta["peak_seconds"]), 6),
                "window_start_index": int(ir_meta["window_start_index"]),
                "window_end_index": int(ir_meta["window_end_index"]),
                "window_seconds": round(float(ir_meta["window_seconds"]), 6),
                "pre_window_seconds": round(float(ir_meta["pre_window_seconds"]), 6),
                "post_window_seconds": round(float(ir_meta["post_window_seconds"]), 6),
                "peak_dbfs": round(float(ir_meta["peak_dbfs"]), 2),
            },
        }
        hard_failures = [item["message"] for item in quality_checks["items"] if item.get("level") == "error"]
        if hard_failures:
            raise CaptureQualityError(capture_label, quality_checks["items"], analysis=analysis)
        return analysis

    def _analyze_browser_sync_scaffold_capture(
        self,
        capture_path: Path,
        *,
        expected_sample_rate: int,
        channel: str,
        reference_sweep: np.ndarray,
        inverse_sweep: np.ndarray,
        sync_bursts: list[dict[str, Any]],
        emit: dict[str, Any],
        calibration_curve: tuple[np.ndarray, np.ndarray] | None,
        browser_capture_meta: dict[str, Any] | None = None,
        capture_label: str = "Browser capture",
    ) -> dict[str, Any]:
        sample_rate, raw_signal = self._load_wav_array(capture_path)
        signal = self._select_analysis_channel(raw_signal, channel=channel)
        timing_signal = self._select_analysis_channel(raw_signal, channel=channel)
        if sample_rate != expected_sample_rate:
            raise RuntimeError(f"Unexpected capture sample rate: {sample_rate} Hz (expected {expected_sample_rate} Hz)")
        if reference_sweep.size == 0 or not sync_bursts:
            raise RuntimeError("Browser sync scaffold metadata was incomplete")
        if signal.size < reference_sweep.size:
            raise RuntimeError("Capture is too short for browser sync-scaffold analysis")

        rms = float(np.sqrt(np.mean(np.square(signal, dtype=np.float64))))
        peak = float(np.max(np.abs(signal)))
        rms_dbfs = 20.0 * math.log10(max(rms, 1e-9))
        peak_dbfs = 20.0 * math.log10(max(peak, 1e-9))
        if peak_dbfs <= -90.0 and rms_dbfs <= -100.0:
            raise RuntimeError("Recorded sweep was effectively silent")

        burst_defs = [
            {
                "name": str(burst.get("name") or "burst"),
                "cluster": str(burst.get("cluster") or "?"),
                "seed": int(burst.get("seed") or 0),
                "start_emit": int(burst.get("start_emit") or 0),
                "end_emit": int(burst.get("end_emit") or 0),
                "template": np.array(burst.get("template") or [], dtype=np.float32),
            }
            for burst in sync_bursts
        ]
        burst_matches = []
        for burst in burst_defs:
            match = self._detect_browser_reference_template(
                timing_signal,
                burst["template"],
                sample_rate,
                low_hz=BROWSER_REFERENCE_SYNC_START_HZ,
                high_hz=BROWSER_REFERENCE_SYNC_END_HZ,
            )
            burst_matches.append({**burst, "match": match})

        timing = self._estimate_browser_sync_scaffold_timing(
            signal=signal,
            timing_signal=timing_signal,
            reference_sweep=reference_sweep,
            burst_matches=burst_matches,
            emit=emit,
            sample_rate=sample_rate,
        )

        corrected_signal = timing["corrected_signal"]
        corrected_timing_signal = timing["corrected_timing_signal"]
        sweep_start = int(timing["sweep_start_emit"])
        analysis_segment = corrected_signal[sweep_start:].astype(np.float64)
        reference_segment = corrected_timing_signal[sweep_start:].astype(np.float64)
        if analysis_segment.size < max(2048, reference_sweep.size // 4):
            raise RuntimeError("Corrected browser sweep segment was too short after sync scaffold estimation")
        corrected_segment_size = analysis_segment.size
        captured_tail_samples = max(0, analysis_segment.size - int(reference_sweep.size))

        impulse_response = self._fft_convolve(analysis_segment, inverse_sweep.astype(np.float64))
        reference_impulse_response = self._fft_convolve(reference_segment, inverse_sweep.astype(np.float64))
        windowed_ir, ir_meta = self._window_impulse_response(impulse_response, sample_rate)
        reference_ir_peak = float(np.max(np.abs(reference_impulse_response))) if reference_impulse_response.size else 0.0
        reference_ir_rms = float(np.sqrt(np.mean(np.square(reference_impulse_response, dtype=np.float64)))) if reference_impulse_response.size else 0.0
        reference_ir_peak_db = 20.0 * math.log10(max(reference_ir_peak, 1e-9))
        reference_ir_rms_db = 20.0 * math.log10(max(reference_ir_rms, 1e-9))
        reference_ir_sharpness_db = reference_ir_peak_db - reference_ir_rms_db
        fft_size = self._next_pow2(max(sample_rate, windowed_ir.size * 2))
        magnitude = np.abs(np.fft.rfft(windowed_ir, n=fft_size))
        frequencies = np.fft.rfftfreq(fft_size, d=1.0 / sample_rate)

        display_data = self._build_display_points(
            frequencies=frequencies,
            magnitude=magnitude,
            calibration_curve=calibration_curve,
            browser_capture=True,
        )
        capture_audit = self._build_capture_audit(
            raw_signal=raw_signal,
            sample_rate=sample_rate,
            browser_capture_meta=browser_capture_meta,
        )
        quality_checks = self._build_capture_quality_checks(
            capture_audit=capture_audit,
            timing=timing,
            peak_dbfs=peak_dbfs,
            trusted_band_meta=display_data["trusted_band_meta"],
            trusted_max_hz=display_data["trusted_band"][1],
            response_outliers=display_data.get("response_outliers") or [],
            capture_label=capture_label,
            expect_dual_mono_channels=True,
        )
        analysis = {
            "method": "inverse log-sweep deconvolution with browser sync-scaffold timing correction",
            "trusted_points": display_data["trusted_points"],
            "review_points": display_data["review_points"],
            "normalized_by_db": round(display_data["normalized_by"], 3),
            "rms_dbfs": round(rms_dbfs, 2),
            "peak_dbfs": round(peak_dbfs, 2),
            "window_count": 1,
            "alignment_samples": int(sweep_start),
            "alignment_seconds": round(sweep_start / sample_rate, 6),
            "trusted_min_hz": round(display_data["trusted_band"][0], 3),
            "trusted_max_hz": round(display_data["trusted_band"][1], 3),
            "raw_point_count": display_data["raw_point_count"],
            "review_point_count": len(display_data["review_points"]),
            "display_point_count": len(display_data["trusted_points"]),
            "trusted_band_meta": display_data["trusted_band_meta"],
            "review_band_meta": display_data["review_band_meta"],
            "quality_checks": quality_checks,
            "capture_audit": capture_audit,
            "clock": {
                "observed_sweep_samples": int(timing["observed_sweep_samples"]),
                "reference_sweep_samples": int(reference_sweep.size),
                "analysis_segment_samples": int(analysis_segment.size),
                "corrected_segment_samples": int(corrected_segment_size),
                "captured_tail_samples": int(captured_tail_samples),
                "captured_tail_seconds": round(float(captured_tail_samples) / sample_rate, 6),
                "stretch_ratio": round(float(timing["stretch_ratio"]), 8),
                "drift_ppm": round(float(timing["drift_ppm"]), 2),
                "compensated": True,
                "anchor_seconds": round(float(BROWSER_REFERENCE_SYNC_BURST_SECONDS), 4),
                "anchor_strategy": "sync-scaffold multi-burst affine fit",
                "anchor_matches": timing["burst_matches"],
                "timing_channel": "browser-sync-scaffold",
                "accepted_burst_count": int(timing["accepted_burst_count"]),
                "accepted_cluster_a_count": int(timing["accepted_cluster_a_count"]),
                "accepted_cluster_b_count": int(timing["accepted_cluster_b_count"]),
                "fit_residual_rms_ms": round(float(timing["fit_residual_rms_ms"]), 3),
                "max_burst_residual_ms": round(float(timing["max_burst_residual_ms"]), 3),
                "cluster_order_valid": bool(timing["cluster_order_valid"]),
                "corrected_sweep_score": round(float(timing["corrected_sweep_score"]), 5),
                "emit": timing["emit"],
            },
            "reference_path": {
                "channel": "browser-sync-scaffold",
                "peak_dbfs": round(peak_dbfs, 2),
                "rms_dbfs": round(rms_dbfs, 2),
                "alignment_score": round(float(timing["corrected_sweep_score"]), 5),
                "start_score": round(float(timing["corrected_sweep_score"]), 5),
                "end_score": round(float(timing["corrected_sweep_score"]), 5),
                "drift_ppm": round(float(timing["drift_ppm"]), 2),
                "ir_peak_dbfs": round(reference_ir_peak_db, 2),
                "ir_sharpness_db": round(reference_ir_sharpness_db, 2),
                "clipped": bool(peak_dbfs >= CAPTURE_CLIP_FAIL_DBFS),
            },
            "impulse_response": {
                "peak_index": int(ir_meta["peak_index"]),
                "peak_seconds": round(float(ir_meta["peak_seconds"]), 6),
                "window_start_index": int(ir_meta["window_start_index"]),
                "window_end_index": int(ir_meta["window_end_index"]),
                "window_seconds": round(float(ir_meta["window_seconds"]), 6),
                "pre_window_seconds": round(float(ir_meta["pre_window_seconds"]), 6),
                "post_window_seconds": round(float(ir_meta["post_window_seconds"]), 6),
                "peak_dbfs": round(float(ir_meta["peak_dbfs"]), 2),
            },
        }
        hard_failures = [item["message"] for item in quality_checks["items"] if item.get("level") == "error"]
        if hard_failures:
            raise CaptureQualityError(capture_label, quality_checks["items"], analysis=analysis)
        return analysis

    def _estimate_browser_sync_scaffold_timing(
        self,
        *,
        signal: np.ndarray,
        timing_signal: np.ndarray,
        reference_sweep: np.ndarray,
        burst_matches: list[dict[str, Any]],
        emit: dict[str, Any],
        sample_rate: int,
    ) -> dict[str, Any]:
        sweep_start_emit = int(emit.get("sweep_start_emit") or 0)
        sweep_end_emit = int(emit.get("sweep_end_emit") or 0)
        program_samples = int(emit.get("program_samples") or 0)
        if sweep_end_emit <= sweep_start_emit or program_samples <= 0:
            raise RuntimeError("Browser sync scaffold metadata was incomplete")

        hypothesis = self._select_browser_sync_hypothesis(burst_matches, sample_rate)
        accepted = hypothesis["accepted"]
        alpha, beta = self._fit_affine_mapping(
            [float(item["start_emit"]) for item in accepted],
            [float(item["observed_start"]) for item in accepted],
            [float(item["weight"]) for item in accepted],
        )
        refined = self._collect_sync_inliers(
            burst_matches,
            alpha,
            beta,
            sample_rate,
            window_seconds=BROWSER_SYNC_REFINED_WINDOW_SECONDS,
        )
        accepted = refined["accepted"]
        if refined["cluster_counts"]["A"] < BROWSER_SYNC_MIN_CLUSTER_BURSTS or refined["cluster_counts"]["B"] < BROWSER_SYNC_MIN_CLUSTER_BURSTS or len(accepted) < BROWSER_SYNC_MIN_TOTAL_BURSTS:
            accepted = hypothesis["accepted"]
            alpha = float(hypothesis["alpha"])
            beta = float(hypothesis["beta"])
        else:
            alpha, beta = self._fit_affine_mapping(
                [float(item["start_emit"]) for item in accepted],
                [float(item["observed_start"]) for item in accepted],
                [float(item["weight"]) for item in accepted],
            )

        if not np.isfinite(alpha) or alpha <= 0.0:
            raise RuntimeError("Unable to estimate browser timing drift from sync scaffold")
        drift_ppm = (alpha - 1.0) * 1_000_000.0
        corrected_signal, corrected_timing_signal = self._warp_browser_reference_capture(
            signal=signal,
            timing_signal=timing_signal,
            alpha=alpha,
            beta=beta,
            emit=emit,
        )

        corrected_matches = []
        for burst in burst_matches:
            corrected_match = self._detect_browser_reference_template(
                corrected_timing_signal,
                burst["template"],
                sample_rate,
                search_start=max(0, int(burst["start_emit"]) - int(round(sample_rate * 0.06))),
                search_end=min(corrected_timing_signal.size, int(burst["end_emit"]) + int(round(sample_rate * 0.06))),
                low_hz=BROWSER_REFERENCE_SYNC_START_HZ,
                high_hz=BROWSER_REFERENCE_SYNC_END_HZ,
            )
            residual_ms = abs(float(corrected_match["index"]) - float(burst["start_emit"])) * 1000.0 / sample_rate
            corrected_matches.append(
                {
                    "name": str(burst["name"]),
                    "cluster": str(burst["cluster"]),
                    "seed": int(burst["seed"]),
                    "offset_samples": int(burst["start_emit"]),
                    "score": round(float(burst["match"]["score"]), 5),
                    "raw_score": round(float(burst["match"]["raw_score"]), 5),
                    "polarity": int(-1 if float(burst["match"]["polarity"]) < 0 else 1),
                    "observed_start": int(round(next((item["observed_start"] for item in accepted if item["name"] == burst["name"]), burst["match"]["index"]))),
                    "residual_samples": int(round(float(corrected_match["index"]) - float(burst["start_emit"]))),
                    "inlier": any(item["name"] == burst["name"] for item in accepted),
                    "peak_ratio": round(float(burst["match"]["peak_ratio"]), 5),
                    "width_ms": round(float(burst["match"]["width_ms"]), 3),
                    "corrected_score": round(float(corrected_match["score"]), 5),
                    "corrected_residual_ms": round(float(residual_ms), 3),
                    "candidates": burst["match"]["candidates"],
                }
            )

        fit_residuals_ms = [
            abs((float(item["observed_start"]) - (alpha * float(item["start_emit"]) + beta)) * 1000.0 / sample_rate)
            for item in accepted
        ]
        fit_residual_rms_ms = math.sqrt(sum(value * value for value in fit_residuals_ms) / max(len(fit_residuals_ms), 1))
        max_burst_residual_ms = max((float(item["corrected_residual_ms"]) for item in corrected_matches if item["inlier"]), default=max(fit_residuals_ms, default=0.0))
        corrected_sweep_segment = corrected_timing_signal[sweep_start_emit:sweep_end_emit]
        corrected_sweep_score = self._normalized_correlation_score(corrected_sweep_segment, reference_sweep)
        observed_sweep_samples = max(1, int(round(reference_sweep.size * alpha)))
        ordered_inliers = [item for item in corrected_matches if item["inlier"]]
        ordered_inliers.sort(key=lambda item: int(item["offset_samples"]))
        cluster_order_valid = all(
            int(ordered_inliers[index]["observed_start"]) < int(ordered_inliers[index + 1]["observed_start"])
            for index in range(len(ordered_inliers) - 1)
        )
        accepted_cluster_a_count = sum(1 for item in corrected_matches if item["inlier"] and item["cluster"] == "A")
        accepted_cluster_b_count = sum(1 for item in corrected_matches if item["inlier"] and item["cluster"] == "B")
        return {
            "method": "sync-scaffold-affine-reference",
            "aligned_start": int(sweep_start_emit),
            "aligned_end": int(sweep_end_emit),
            "observed_sweep_samples": int(observed_sweep_samples),
            "stretch_ratio": float(alpha),
            "drift_ppm": float(drift_ppm),
            "burst_matches": corrected_matches,
            "accepted_burst_count": int(sum(1 for item in corrected_matches if item["inlier"])),
            "accepted_cluster_a_count": int(accepted_cluster_a_count),
            "accepted_cluster_b_count": int(accepted_cluster_b_count),
            "fit_residual_rms_ms": float(fit_residual_rms_ms),
            "max_burst_residual_ms": float(max_burst_residual_ms),
            "cluster_order_valid": bool(cluster_order_valid),
            "corrected_sweep_score": float(corrected_sweep_score),
            "corrected_signal": corrected_signal,
            "corrected_timing_signal": corrected_timing_signal,
            "sweep_start_emit": int(sweep_start_emit),
            "emit": dict(emit),
        }

    def _select_browser_sync_hypothesis(self, burst_matches: list[dict[str, Any]], sample_rate: int) -> dict[str, Any]:
        best: dict[str, Any] | None = None
        cluster_a = [burst for burst in burst_matches if str(burst["cluster"]) == "A"]
        cluster_b = [burst for burst in burst_matches if str(burst["cluster"]) == "B"]
        for burst_a in cluster_a:
            for candidate_a in burst_a["match"]["candidates"]:
                obs_a = float(candidate_a["index"])
                emit_a = float(burst_a["start_emit"])
                for burst_b in cluster_b:
                    for candidate_b in burst_b["match"]["candidates"]:
                        obs_b = float(candidate_b["index"])
                        emit_b = float(burst_b["start_emit"])
                        if obs_b <= obs_a or emit_b <= emit_a:
                            continue
                        alpha = (obs_b - obs_a) / (emit_b - emit_a)
                        if not np.isfinite(alpha) or alpha <= 0.0:
                            continue
                        drift_ppm = abs(alpha - 1.0) * 1_000_000.0
                        if drift_ppm > SWEEP_TIMING_MAX_ABS_PPM:
                            continue
                        beta = obs_a - (alpha * emit_a)
                        collected = self._collect_sync_inliers(
                            burst_matches,
                            alpha,
                            beta,
                            sample_rate,
                            window_seconds=BROWSER_SYNC_ACCEPT_WINDOW_SECONDS,
                        )
                        accepted = collected["accepted"]
                        cluster_counts = collected["cluster_counts"]
                        if cluster_counts["A"] < 1 or cluster_counts["B"] < 1 or len(accepted) < BROWSER_SYNC_MIN_TOTAL_BURSTS:
                            continue
                        ordered = [item["observed_start"] for item in sorted(accepted, key=lambda item: int(item["start_emit"]))]
                        if any(float(ordered[index]) >= float(ordered[index + 1]) for index in range(len(ordered) - 1)):
                            continue
                        score = (
                            len(accepted),
                            min(cluster_counts["A"], cluster_counts["B"]),
                            -float(collected["residual_rms_ms"]),
                            float(sum(item["weight"] for item in accepted)),
                        )
                        if best is None or score > best["score"]:
                            best = {
                                "alpha": float(alpha),
                                "beta": float(beta),
                                "accepted": accepted,
                                "cluster_counts": cluster_counts,
                                "score": score,
                            }
        if best is None:
            raise RuntimeError("Unable to recover a trustworthy browser sync scaffold fit")
        return best

    def _collect_sync_inliers(
        self,
        burst_matches: list[dict[str, Any]],
        alpha: float,
        beta: float,
        sample_rate: int,
        *,
        window_seconds: float,
    ) -> dict[str, Any]:
        window_samples = max(1.0, float(window_seconds) * sample_rate)
        accepted = []
        cluster_counts = {"A": 0, "B": 0}
        residuals_ms: list[float] = []
        for burst in burst_matches:
            predicted = (alpha * float(burst["start_emit"])) + beta
            best_candidate: dict[str, Any] | None = None
            best_residual = 0.0
            for candidate in burst["match"]["candidates"]:
                residual = float(candidate["index"]) - predicted
                if abs(residual) > window_samples:
                    continue
                if best_candidate is None or abs(residual) < abs(best_residual) or (
                    abs(abs(residual) - abs(best_residual)) <= 1.0 and float(candidate["score"]) > float(best_candidate["score"])
                ):
                    best_candidate = candidate
                    best_residual = residual
            if best_candidate is None:
                continue
            weight = self._browser_sync_candidate_weight(best_candidate, burst["match"])
            residual_ms = abs(best_residual) * 1000.0 / sample_rate
            accepted.append(
                {
                    "name": str(burst["name"]),
                    "cluster": str(burst["cluster"]),
                    "seed": int(burst["seed"]),
                    "start_emit": int(burst["start_emit"]),
                    "end_emit": int(burst["end_emit"]),
                    "observed_start": int(round(float(best_candidate["index"]))),
                    "score": float(best_candidate["score"]),
                    "raw_score": float(best_candidate["raw_score"]),
                    "peak_ratio": float(burst["match"]["peak_ratio"]),
                    "width_ms": float(burst["match"]["width_ms"]),
                    "weight": float(weight),
                    "residual_ms": float(residual_ms),
                }
            )
            cluster_counts[str(burst["cluster"])] += 1
            residuals_ms.append(float(residual_ms))
        residual_rms_ms = math.sqrt(sum(value * value for value in residuals_ms) / max(len(residuals_ms), 1))
        return {
            "accepted": accepted,
            "cluster_counts": cluster_counts,
            "residual_rms_ms": float(residual_rms_ms),
        }

    def _browser_sync_candidate_weight(self, candidate: dict[str, Any], match: dict[str, Any]) -> float:
        score = max(float(candidate.get("score") or 0.0), 0.05)
        ratio = max(float(match.get("peak_ratio") or 0.0), 1.0)
        width_ms = max(float(match.get("width_ms") or 0.0), 0.25)
        sharpness = 1.0 / max(width_ms, 0.25)
        return score * min(ratio, 4.0) * sharpness

    def _fit_affine_mapping(self, emit_points: list[float], observed_points: list[float], weights: list[float]) -> tuple[float, float]:
        x = np.array(emit_points, dtype=np.float64)
        y = np.array(observed_points, dtype=np.float64)
        w = np.array(weights, dtype=np.float64)
        if x.size < 2 or y.size != x.size or w.size != x.size:
            raise RuntimeError("Not enough sync points for affine timing fit")
        w = np.maximum(w, 1e-6)
        design = np.column_stack([x, np.ones_like(x)])
        weighted_design = design * np.sqrt(w)[:, None]
        weighted_y = y * np.sqrt(w)
        coeffs, *_ = np.linalg.lstsq(weighted_design, weighted_y, rcond=None)
        return float(coeffs[0]), float(coeffs[1])

    def _estimate_browser_reference_timing(
        self,
        *,
        signal: np.ndarray,
        timing_signal: np.ndarray,
        reference_sweep: np.ndarray,
        marker_a: np.ndarray,
        marker_b: np.ndarray,
        marker_a_match: dict[str, Any],
        marker_b_match: dict[str, Any],
        emit: dict[str, Any],
        sample_rate: int,
    ) -> dict[str, Any]:
        marker_a_emit = int(emit.get("marker_a_start_emit") or 0)
        marker_b_emit = int(emit.get("marker_b_start_emit") or 0)
        sweep_start_emit = int(emit.get("sweep_start_emit") or 0)
        sweep_end_emit = int(emit.get("sweep_end_emit") or 0)
        program_samples = int(emit.get("program_samples") or 0)
        if marker_b_emit <= marker_a_emit or sweep_end_emit <= sweep_start_emit or program_samples <= 0:
            raise RuntimeError("Browser acoustic-reference metadata was incomplete")

        marker_a_obs = float(marker_a_match["index"])
        marker_b_obs = float(marker_b_match["index"])
        observed_spacing = marker_b_obs - marker_a_obs
        emitted_spacing = float(marker_b_emit - marker_a_emit)
        if observed_spacing <= 0.0:
            raise RuntimeError("Observed browser timing markers were out of order")
        alpha = observed_spacing / emitted_spacing
        if not np.isfinite(alpha) or alpha <= 0.0:
            raise RuntimeError("Unable to estimate browser timing drift from markers")
        beta = marker_a_obs - (alpha * marker_a_emit)
        drift_ppm = (alpha - 1.0) * 1_000_000.0
        corrected_signal, corrected_timing_signal = self._warp_browser_reference_capture(
            signal=signal,
            timing_signal=timing_signal,
            alpha=alpha,
            beta=beta,
            emit=emit,
        )

        corrected_marker_a = self._detect_browser_timing_marker(
            corrected_timing_signal,
            marker_a,
            sample_rate,
            search_start=max(0, marker_a_emit - int(round(sample_rate * 0.08))),
            search_end=min(corrected_timing_signal.size, marker_a_emit + marker_a.size + int(round(sample_rate * 0.08))),
        )
        corrected_marker_b = self._detect_browser_timing_marker(
            corrected_timing_signal,
            marker_b,
            sample_rate,
            search_start=max(0, marker_b_emit - int(round(sample_rate * 0.08))),
            search_end=min(corrected_timing_signal.size, marker_b_emit + marker_b.size + int(round(sample_rate * 0.08))),
        )
        marker_a_error_ms = abs(float(corrected_marker_a["index"]) - marker_a_emit) * 1000.0 / sample_rate
        marker_b_error_ms = abs(float(corrected_marker_b["index"]) - marker_b_emit) * 1000.0 / sample_rate
        marker_fit_residual_ms = max(marker_a_error_ms, marker_b_error_ms)
        corrected_spacing = float(corrected_marker_b["index"]) - float(corrected_marker_a["index"])
        marker_spacing_error_ms = abs(corrected_spacing - emitted_spacing) * 1000.0 / sample_rate
        corrected_sweep_segment = corrected_timing_signal[sweep_start_emit:sweep_end_emit]
        corrected_sweep_score = self._normalized_correlation_score(corrected_sweep_segment, reference_sweep)
        observed_sweep_samples = max(1, int(round(reference_sweep.size * alpha)))
        marker_matches = [
            {
                "name": "marker-a",
                "offset_samples": int(marker_a_emit),
                "score": round(float(marker_a_match["score"]), 5),
                "raw_score": round(float(marker_a_match["raw_score"]), 5),
                "polarity": int(-1 if float(marker_a_match["polarity"]) < 0 else 1),
                "observed_start": int(round(marker_a_obs)),
                "residual_samples": int(round(float(corrected_marker_a["index"]) - marker_a_emit)),
                "inlier": True,
                "peak_ratio": round(float(marker_a_match["peak_ratio"]), 5),
                "width_ms": round(float(marker_a_match["width_ms"]), 3),
                "candidates": marker_a_match["candidates"],
            },
            {
                "name": "marker-b",
                "offset_samples": int(marker_b_emit),
                "score": round(float(marker_b_match["score"]), 5),
                "raw_score": round(float(marker_b_match["raw_score"]), 5),
                "polarity": int(-1 if float(marker_b_match["polarity"]) < 0 else 1),
                "observed_start": int(round(marker_b_obs)),
                "residual_samples": int(round(float(corrected_marker_b["index"]) - marker_b_emit)),
                "inlier": True,
                "peak_ratio": round(float(marker_b_match["peak_ratio"]), 5),
                "width_ms": round(float(marker_b_match["width_ms"]), 3),
                "candidates": marker_b_match["candidates"],
            },
        ]
        return {
            "method": "marker-affine-reference",
            "aligned_start": int(sweep_start_emit),
            "aligned_end": int(sweep_end_emit),
            "observed_sweep_samples": int(observed_sweep_samples),
            "stretch_ratio": float(alpha),
            "drift_ppm": float(drift_ppm),
            "marker_matches": marker_matches,
            "marker_a_score": float(marker_a_match["score"]),
            "marker_a_peak_ratio": float(marker_a_match["peak_ratio"]),
            "marker_b_score": float(marker_b_match["score"]),
            "marker_b_peak_ratio": float(marker_b_match["peak_ratio"]),
            "marker_fit_residual_ms": float(marker_fit_residual_ms),
            "marker_spacing_error_ms": float(marker_spacing_error_ms),
            "corrected_sweep_score": float(corrected_sweep_score),
            "corrected_signal": corrected_signal,
            "corrected_timing_signal": corrected_timing_signal,
            "sweep_start_emit": int(sweep_start_emit),
            "emit": dict(emit),
        }

    def _warp_browser_reference_capture(
        self,
        *,
        signal: np.ndarray,
        timing_signal: np.ndarray,
        alpha: float,
        beta: float,
        emit: dict[str, Any],
    ) -> tuple[np.ndarray, np.ndarray]:
        program_samples = int(emit.get("program_samples") or 0)
        stop_margin_samples = int(emit.get("stop_margin_samples") or 0)
        corrected_size = max(program_samples + stop_margin_samples, 1)
        emit_positions = np.arange(corrected_size, dtype=np.float64)
        source_positions = beta + (alpha * emit_positions)
        source_indices = np.arange(signal.size, dtype=np.float64)
        corrected_signal = np.interp(source_positions, source_indices, signal.astype(np.float64), left=0.0, right=0.0)
        corrected_timing_signal = np.interp(source_positions, source_indices, timing_signal.astype(np.float64), left=0.0, right=0.0)
        return corrected_signal, corrected_timing_signal

    def _detect_browser_timing_marker(
        self,
        signal: np.ndarray,
        template: np.ndarray,
        sample_rate: int,
        search_start: int | None = None,
        search_end: int | None = None,
    ) -> dict[str, Any]:
        return self._detect_browser_reference_template(
            signal,
            template,
            sample_rate,
            search_start=search_start,
            search_end=search_end,
            low_hz=BROWSER_REFERENCE_SYNC_START_HZ,
            high_hz=BROWSER_REFERENCE_SYNC_END_HZ,
        )

    def _detect_browser_reference_template(
        self,
        signal: np.ndarray,
        template: np.ndarray,
        sample_rate: int,
        *,
        search_start: int | None = None,
        search_end: int | None = None,
        low_hz: float,
        high_hz: float,
    ) -> dict[str, Any]:
        signal64 = signal.astype(np.float64)
        template64 = template.astype(np.float64)
        start = max(0, int(search_start or 0))
        end = min(signal64.size, int(search_end or signal64.size))
        region = signal64[start:end]
        if region.size < template64.size:
            raise RuntimeError("Browser timing search region was too short for template detection")
        filter_low = max(low_hz * 0.9, 20.0)
        filter_high = min((sample_rate / 2.0) - 200.0, high_hz * 1.02)
        filtered_region = self._fft_bandpass(region, sample_rate, filter_low, filter_high)
        best_result: dict[str, Any] | None = None
        for stretch_ratio in np.linspace(0.994, 1.006, 13, dtype=np.float64):
            template_size = max(1024, int(round(template64.size * stretch_ratio)))
            stretched_template = self._resample_signal(template64, template_size)
            filtered_template = self._fft_bandpass(stretched_template, sample_rate, filter_low, filter_high)
            corr = self._fft_correlate(filtered_region, filtered_template[::-1])
            valid = corr[filtered_template.size - 1 : filtered_region.size]
            template_norm = float(np.linalg.norm(filtered_template))
            if template_norm <= 1e-12 or valid.size == 0:
                continue
            energy = np.square(filtered_region, dtype=np.float64)
            cumulative = np.concatenate([np.zeros(1, dtype=np.float64), np.cumsum(energy)])
            window_energy = cumulative[filtered_template.size:] - cumulative[:-filtered_template.size]
            denominator = np.sqrt(np.maximum(window_energy, 1e-12)) * template_norm
            raw_scores = np.divide(valid, denominator, out=np.zeros_like(valid, dtype=np.float64), where=denominator > 1e-12)
            abs_scores = np.abs(raw_scores)
            best_index = int(np.argmax(abs_scores))
            best_score = float(abs_scores[best_index])
            if best_result is not None and best_score <= float(best_result["score"]):
                continue
            best_raw_score = float(raw_scores[best_index])
            exclusion = max(1, filtered_template.size // 3)
            second_mask = np.ones(abs_scores.size, dtype=bool)
            second_mask[max(0, best_index - exclusion):min(abs_scores.size, best_index + exclusion + 1)] = False
            second_score = float(np.max(abs_scores[second_mask])) if np.any(second_mask) else 0.0
            peak_ratio = best_score / max(second_score, 1e-6)
            width_samples = self._peak_width_samples(abs_scores, best_index, threshold=max(best_score * 0.5, 0.1))
            candidates = self._top_marker_candidates(abs_scores, raw_scores, sample_rate, base_index=start)
            best_result = {
                "index": int(start + best_index),
                "score": best_score,
                "raw_score": best_raw_score,
                "polarity": -1.0 if best_raw_score < 0 else 1.0,
                "second_score": second_score,
                "peak_ratio": float(peak_ratio),
                "width_ms": float(width_samples) * 1000.0 / sample_rate,
                "candidates": candidates,
                "stretch_ratio": stretch_ratio,
            }
        if best_result is None:
            raise RuntimeError("Unable to detect browser timing template")
        return best_result

    def _fft_bandpass(self, signal: np.ndarray, sample_rate: int, low_hz: float, high_hz: float) -> np.ndarray:
        signal64 = signal.astype(np.float64)
        if signal64.size == 0:
            return signal64
        spectrum = np.fft.rfft(signal64)
        freqs = np.fft.rfftfreq(signal64.size, d=1.0 / sample_rate)
        mask = (freqs >= max(low_hz, 0.0)) & (freqs <= min(high_hz, sample_rate / 2.0))
        spectrum[~mask] = 0.0
        return np.fft.irfft(spectrum, n=signal64.size)

    def _normalized_correlation_score(self, signal: np.ndarray, template: np.ndarray) -> float:
        signal64 = signal.astype(np.float64)
        template64 = template.astype(np.float64)
        size = min(signal64.size, template64.size)
        if size <= 0:
            return 0.0
        signal64 = signal64[:size]
        template64 = template64[:size]
        denominator = float(np.linalg.norm(signal64) * np.linalg.norm(template64))
        if denominator <= 1e-12:
            return 0.0
        return abs(float(np.dot(signal64, template64) / denominator))

    def _peak_width_samples(self, values: np.ndarray, peak_index: int, threshold: float) -> int:
        left = peak_index
        right = peak_index
        while left > 0 and float(values[left - 1]) >= threshold:
            left -= 1
        while right + 1 < values.size and float(values[right + 1]) >= threshold:
            right += 1
        return max(1, right - left + 1)

    def _top_marker_candidates(self, abs_scores: np.ndarray, raw_scores: np.ndarray, sample_rate: int, limit: int = 5, base_index: int = 0) -> list[dict[str, Any]]:
        if abs_scores.size == 0:
            return []
        order = np.argsort(abs_scores)[::-1]
        exclusion = max(1, int(round(sample_rate * 0.08)))
        chosen: list[int] = []
        for index in order:
            if any(abs(index - existing) <= exclusion for existing in chosen):
                continue
            chosen.append(int(index))
            if len(chosen) >= limit:
                break
        return [
            {
                "index": int(base_index + index),
                "score": round(float(abs_scores[index]), 5),
                "raw_score": round(float(raw_scores[index]), 5),
            }
            for index in chosen
        ]

    def _build_display_points(
        self,
        *,
        frequencies: np.ndarray,
        magnitude: np.ndarray,
        calibration_curve: tuple[np.ndarray, np.ndarray] | None,
        browser_capture: bool,
    ) -> dict[str, Any]:
        analysis_limit_hz = min(float(frequencies[-1]) - 1.0, SWEEP_END_HZ)
        display_max_hz = min(analysis_limit_hz, TRUSTED_MAX_HZ)
        nyquist = max(TRUSTED_MIN_HZ + 1.0, display_max_hz)
        centers = self._log_spaced_frequencies(TRUSTED_MIN_HZ, nyquist, DISPLAY_POINT_COUNT)
        smoothing_ratio = 2 ** (1 / 12)
        corrected_magnitude = magnitude.astype(np.float64, copy=True)
        if calibration_curve is not None:
            cal_freqs, cal_offsets = calibration_curve
            log_freqs = np.log(np.clip(frequencies, 1e-9, None))
            log_cal_freqs = np.log(cal_freqs)
            interpolated_offsets = np.interp(
                log_freqs,
                log_cal_freqs,
                cal_offsets,
                left=float(cal_offsets[0]),
                right=float(cal_offsets[-1]),
            )
            corrected_magnitude *= np.power(10.0, -interpolated_offsets / 20.0)

        raw_points: list[list[float]] = []
        raw_db_values: list[float] = []
        for center in centers:
            lower = center / smoothing_ratio
            upper = min(center * smoothing_ratio, analysis_limit_hz)
            if lower >= analysis_limit_hz:
                continue
            mask = (frequencies >= lower) & (frequencies <= upper)
            if not np.any(mask):
                continue
            band_mag = float(np.sqrt(np.mean(np.square(corrected_magnitude[mask], dtype=np.float64))))
            db = 20.0 * math.log10(max(band_mag, 1e-12))
            raw_points.append([round(center, 3), round(db, 3)])
            raw_db_values.append(db)
        if not raw_points:
            raise RuntimeError("Sweep analysis produced no displayable trace points")

        trusted_min_hz, trusted_max_hz, trusted_band_meta = self._select_trusted_band(
            raw_points,
            browser_capture=browser_capture,
        )
        trusted_points = [point for point in raw_points if trusted_min_hz <= point[0] <= trusted_max_hz]
        if not trusted_points:
            raise RuntimeError("Sweep analysis produced no trusted trace points")

        response_outliers = self._find_response_outliers(
            raw_points,
            min_hz=max(RESPONSE_OUTLIER_MIN_HZ, trusted_min_hz),
            max_hz=trusted_max_hz,
        )

        reference_values = [db for freq, db in trusted_points if 120.0 <= freq <= 8_000.0] or raw_db_values
        normalized_by = float(np.median(reference_values)) if reference_values else 0.0
        normalized_trusted_points = [[freq, round(db - normalized_by, 3)] for freq, db in trusted_points]
        normalized_review_points = [[freq, round(db - normalized_by, 3)] for freq, db in raw_points]
        return {
            "trusted_points": normalized_trusted_points,
            "review_points": normalized_review_points,
            "normalized_by": normalized_by,
            "trusted_band": (trusted_min_hz, trusted_max_hz),
            "raw_point_count": len(raw_points),
            "trusted_band_meta": trusted_band_meta,
            "review_band_meta": {
                "selection": "full-band raw review",
                "min_hz": round(float(raw_points[0][0]), 3),
                "max_hz": round(float(raw_points[-1][0]), 3),
                "point_count": len(raw_points),
                "normalization_reference": "shared with trusted trace",
                "excluded_from_trusted_below_hz": round(float(trusted_min_hz), 3),
                "excluded_from_trusted_above_hz": round(float(trusted_max_hz), 3),
                "trusted_comparison_upper_hz": round(float(trusted_max_hz), 3),
            },
            "response_outliers": response_outliers,
        }

    def _select_trusted_band(
        self,
        raw_points: list[list[float]],
        *,
        browser_capture: bool = False,
    ) -> tuple[float, float, dict[str, Any]]:
        freqs = [float(point[0]) for point in raw_points]
        if not freqs:
            raise RuntimeError("Sweep analysis produced no points for full-band display")

        levels = [float(point[1]) for point in raw_points]
        total_points = len(freqs)
        window_points = min(EDGE_STABILITY_WINDOW_POINTS, total_points)
        min_trusted_points = min(MIN_TRUSTED_POINTS, total_points)
        low_index = 0
        high_index = total_points - 1

        while (high_index - low_index + 1) > min_trusted_points and not self._edge_window_is_stable(levels[low_index : low_index + window_points]):
            low_index += 1
        while (high_index - low_index + 1) > min_trusted_points and not self._edge_window_is_stable(levels[high_index - window_points + 1 : high_index + 1]):
            high_index -= 1

        low_stable = self._edge_window_is_stable(levels[low_index : low_index + window_points])
        high_stable = self._edge_window_is_stable(levels[high_index - window_points + 1 : high_index + 1])
        edge_trimmed = low_index > 0 or high_index < total_points - 1

        upper_edge_guard_applied = False
        upper_edge_guard_rejected_points = 0
        if browser_capture:
            high_index, upper_edge_guard_rejected_points = self._apply_browser_upper_edge_guard(
                freqs,
                levels,
                low_index=low_index,
                high_index=high_index,
                min_trusted_points=min_trusted_points,
            )
            upper_edge_guard_applied = upper_edge_guard_rejected_points > 0

        trimmed = low_index > 0 or high_index < total_points - 1
        selection_reasons = []
        if edge_trimmed:
            selection_reasons.append("edge-stability")
        if upper_edge_guard_applied:
            selection_reasons.append("upper-edge-guard")
        if trimmed and (high_index - low_index + 1) >= min_trusted_points:
            selection = "+".join(selection_reasons) + "-trimmed" if selection_reasons else "trimmed"
        elif not trimmed:
            selection = "full-band stable"
        else:
            selection = "minimum-point fallback"

        return freqs[low_index], freqs[high_index], {
            "selection": selection,
            "edge_window_points": window_points,
            "low_rejected_points": low_index,
            "high_rejected_points": total_points - high_index - 1,
            "min_trusted_points": min_trusted_points,
            "stable_low_edge": bool(low_stable),
            "stable_high_edge": bool(high_stable),
            "trusted_point_count": high_index - low_index + 1,
            "upper_edge_guard_start_hz": round(float(BROWSER_UPPER_EDGE_GUARD_START_HZ), 3),
            "upper_edge_guard_applied": bool(upper_edge_guard_applied),
            "upper_edge_guard_rejected_points": int(upper_edge_guard_rejected_points),
            "upper_edge_guard_reason": "browser final-edge instability" if upper_edge_guard_applied else "",
        }

    def _apply_browser_upper_edge_guard(
        self,
        freqs: list[float],
        levels: list[float],
        *,
        low_index: int,
        high_index: int,
        min_trusted_points: int,
    ) -> tuple[int, int]:
        guard_start_index = next((index for index in range(low_index, high_index + 1) if freqs[index] >= BROWSER_UPPER_EDGE_GUARD_START_HZ), high_index)
        inspect_start = max(guard_start_index, high_index - BROWSER_UPPER_EDGE_REJECT_MAX_POINTS + 1)
        earliest_unstable_index = None
        for index in range(inspect_start, high_index + 1):
            if self._browser_upper_edge_point_is_unstable(levels, index, low_index=low_index):
                earliest_unstable_index = index
                break
        if earliest_unstable_index is None:
            return high_index, 0
        candidate_high = earliest_unstable_index - 1
        if (candidate_high - low_index + 1) < min_trusted_points:
            return high_index, 0
        return candidate_high, high_index - candidate_high

    @staticmethod
    def _browser_upper_edge_point_is_unstable(
        levels: list[float],
        index: int,
        *,
        low_index: int,
    ) -> bool:
        if index <= low_index:
            return False
        context_start = max(low_index, index - 3)
        context_levels = [levels[position] for position in range(context_start, index)]
        if not context_levels:
            return False
        local_median = float(np.median(context_levels))
        local_deviation = abs(float(levels[index]) - local_median)
        delta_from_previous = abs(float(levels[index]) - float(levels[index - 1]))
        return (
            local_deviation >= BROWSER_UPPER_EDGE_SINGLE_POINT_MAX_DEVIATION_DB
            or delta_from_previous >= BROWSER_UPPER_EDGE_SINGLE_POINT_MAX_DELTA_DB
        )

    @staticmethod
    def _find_response_outliers(
        raw_points: list[list[float]],
        *,
        min_hz: float,
        max_hz: float,
    ) -> list[dict[str, float | str]]:
        if len(raw_points) < (RESPONSE_OUTLIER_NEIGHBOR_RADIUS * 2 + 1):
            return []

        outliers: list[dict[str, float | str]] = []
        radius = RESPONSE_OUTLIER_NEIGHBOR_RADIUS
        for index in range(radius, len(raw_points) - radius):
            frequency = float(raw_points[index][0])
            if frequency < min_hz or frequency > max_hz:
                continue
            neighbor_levels = [
                float(raw_points[neighbor_index][1])
                for neighbor_index in range(index - radius, index + radius + 1)
                if neighbor_index != index
            ]
            if not neighbor_levels:
                continue
            local_median = float(np.median(neighbor_levels))
            deviation_db = abs(float(raw_points[index][1]) - local_median)
            if deviation_db < RESPONSE_OUTLIER_WARN_DB:
                continue
            outliers.append(
                {
                    "frequency_hz": round(frequency, 3),
                    "deviation_db": round(deviation_db, 3),
                    "severity": "fail" if deviation_db >= RESPONSE_OUTLIER_FAIL_DB else "warn",
                }
            )
        return outliers

    @staticmethod
    def _edge_window_is_stable(window_levels: list[float]) -> bool:
        if len(window_levels) < 2:
            return True
        deltas = [abs(window_levels[index + 1] - window_levels[index]) for index in range(len(window_levels) - 1)]
        span = max(window_levels) - min(window_levels)
        return max(deltas) <= EDGE_STABILITY_MAX_DELTA_DB and span <= EDGE_STABILITY_MAX_SPAN_DB

    def _write_sweep_file(
        self,
        path: Path,
        *,
        sample_rate: int,
        sweep_seconds: float,
        lead_in_seconds: float,
        tail_seconds: float,
        channel: str,
        start_hz: float = SWEEP_START_HZ,
        end_hz: float = SWEEP_END_HZ,
    ) -> dict[str, Any]:
        sweep = self._generate_log_sweep(
            sample_rate=sample_rate,
            duration_seconds=sweep_seconds,
            start_hz=start_hz,
            end_hz=end_hz,
            peak_scale=HOST_SWEEP_PEAK_SCALE,
        )
        inverse_sweep = self._build_inverse_sweep(
            sweep,
            sample_rate=sample_rate,
            duration_seconds=sweep_seconds,
            start_hz=start_hz,
            end_hz=end_hz,
        )
        lead_in = np.zeros(int(round(sample_rate * lead_in_seconds)), dtype=np.float32)
        tail = np.zeros(int(round(sample_rate * tail_seconds)), dtype=np.float32)
        mono_program = np.concatenate([lead_in, sweep, tail]).astype(np.float32)

        if channel == "right":
            playback = np.column_stack([np.zeros_like(mono_program), mono_program])
        elif channel == "stereo":
            playback = np.column_stack([mono_program, mono_program])
        else:
            playback = np.column_stack([mono_program, np.zeros_like(mono_program)])

        self._write_wav(path, playback, sample_rate)
        return {
            "analysis_sweep": sweep,
            "inverse_sweep": inverse_sweep,
            "samples": int(mono_program.size),
            "channels": 2,
        }

    def _write_browser_reference_file(
        self,
        path: Path,
        *,
        sample_rate: int,
        channel: str,
    ) -> dict[str, Any]:
        sync_bursts = [
            self._generate_browser_sync_burst(
                sample_rate=sample_rate,
                duration_seconds=BROWSER_REFERENCE_SYNC_BURST_SECONDS,
                seed=seed,
            )
            for seed in BROWSER_REFERENCE_SYNC_SEEDS
        ]
        sweep = self._generate_log_sweep(
            sample_rate=sample_rate,
            duration_seconds=SWEEP_V2_SECONDS,
            start_hz=SWEEP_START_HZ,
            end_hz=SWEEP_END_HZ,
            peak_scale=BROWSER_REFERENCE_SWEEP_PEAK_SCALE,
        )
        inverse_sweep = self._build_inverse_sweep(
            sweep,
            sample_rate=sample_rate,
            duration_seconds=SWEEP_V2_SECONDS,
            start_hz=SWEEP_START_HZ,
            end_hz=SWEEP_END_HZ,
        )
        pre_roll = np.zeros(int(round(sample_rate * BROWSER_REFERENCE_PRE_ROLL_SECONDS)), dtype=np.float32)
        burst_gap = np.zeros(int(round(sample_rate * BROWSER_REFERENCE_SYNC_GAP_SECONDS)), dtype=np.float32)
        guard_gap = np.zeros(int(round(sample_rate * BROWSER_REFERENCE_SYNC_GUARD_SECONDS)), dtype=np.float32)
        tail = np.zeros(int(round(sample_rate * BROWSER_REFERENCE_TAIL_SECONDS)), dtype=np.float32)

        segments = [pre_roll]
        burst_meta: list[dict[str, Any]] = []
        names = ("A1", "A2", "A3", "B1", "B2", "B3")
        current = int(pre_roll.size)
        for index, burst in enumerate(sync_bursts[:3]):
            segments.append(burst)
            start_emit = current
            current += int(burst.size)
            burst_meta.append(
                {
                    "name": names[index],
                    "cluster": "A",
                    "seed": int(BROWSER_REFERENCE_SYNC_SEEDS[index]),
                    "start_emit": int(start_emit),
                    "end_emit": int(current),
                }
            )
            if index != 2:
                segments.append(burst_gap)
                current += int(burst_gap.size)
        segments.append(guard_gap)
        current += int(guard_gap.size)
        sweep_start = current
        segments.append(sweep)
        current += int(sweep.size)
        sweep_end = current
        segments.append(guard_gap)
        current += int(guard_gap.size)
        for local_index, burst in enumerate(sync_bursts[3:]):
            index = local_index + 3
            segments.append(burst)
            start_emit = current
            current += int(burst.size)
            burst_meta.append(
                {
                    "name": names[index],
                    "cluster": "B",
                    "seed": int(BROWSER_REFERENCE_SYNC_SEEDS[index]),
                    "start_emit": int(start_emit),
                    "end_emit": int(current),
                }
            )
            if index != 5:
                segments.append(burst_gap)
                current += int(burst_gap.size)
        segments.append(tail)
        mono_program = np.concatenate(segments).astype(np.float32)
        program_peak = float(np.max(np.abs(mono_program))) or 1.0
        mono_program = (mono_program * (BROWSER_REFERENCE_PROGRAM_PEAK / program_peak)).astype(np.float32)
        sweep = mono_program[sweep_start:sweep_end].astype(np.float32)
        burst_payload = []
        for burst in burst_meta:
            template = mono_program[int(burst["start_emit"]):int(burst["end_emit"])]
            burst_payload.append({**burst, "template": template.astype(np.float32)})
        emit = {
            "program_sample_rate": int(sample_rate),
            "program_samples": int(mono_program.size),
            "sweep_start_emit": int(sweep_start),
            "sweep_end_emit": int(sweep_end),
            "sync_bursts": [
                {
                    "name": str(burst["name"]),
                    "cluster": str(burst["cluster"]),
                    "seed": int(burst["seed"]),
                    "start_emit": int(burst["start_emit"]),
                    "end_emit": int(burst["end_emit"]),
                }
                for burst in burst_payload
            ],
            "tail_samples": int(tail.size),
            "stop_margin_samples": int(round(sample_rate * BROWSER_REFERENCE_STOP_MARGIN_SECONDS)),
        }

        if channel == "right":
            playback = np.column_stack([np.zeros_like(mono_program), mono_program])
        elif channel == "stereo":
            playback = np.column_stack([mono_program, mono_program])
        else:
            playback = np.column_stack([mono_program, np.zeros_like(mono_program)])

        self._write_wav(path, playback, sample_rate)
        return {
            "analysis_sweep": sweep,
            "inverse_sweep": inverse_sweep,
            "sync_bursts": burst_payload,
            "emit": emit,
            "samples": int(mono_program.size),
            "channels": 2,
            "sweep_seconds": SWEEP_V2_SECONDS,
            "pre_roll_seconds": BROWSER_REFERENCE_PRE_ROLL_SECONDS,
            "tail_seconds": BROWSER_REFERENCE_TAIL_SECONDS,
            "playback_duration_seconds": mono_program.size / sample_rate,
        }

    def _generate_browser_sync_burst(
        self,
        *,
        sample_rate: int,
        duration_seconds: float,
        seed: int,
    ) -> np.ndarray:
        sample_count = max(2048, int(round(sample_rate * duration_seconds)))
        rng = np.random.default_rng(int(seed))
        burst = rng.standard_normal(sample_count).astype(np.float64)
        burst = self._fft_bandpass(burst, sample_rate, BROWSER_REFERENCE_SYNC_START_HZ, BROWSER_REFERENCE_SYNC_END_HZ)
        spectrum = np.fft.rfft(burst)
        freqs = np.fft.rfftfreq(sample_count, d=1.0 / sample_rate)
        weights = np.ones_like(freqs)
        valid = freqs >= max(BROWSER_REFERENCE_SYNC_START_HZ, 1.0)
        weights[valid] = 1.0 / np.sqrt(np.maximum(freqs[valid] / BROWSER_REFERENCE_SYNC_START_HZ, 1.0))
        burst = np.fft.irfft(spectrum * weights, n=sample_count)
        ramp_samples = min(sample_count // 4, max(64, int(round(sample_rate * BROWSER_REFERENCE_SYNC_RAMP_SECONDS))))
        if ramp_samples > 1:
            ramp = 0.5 - (0.5 * np.cos(np.linspace(0.0, math.pi, ramp_samples, dtype=np.float64)))
            burst[:ramp_samples] *= ramp
            burst[-ramp_samples:] *= ramp[::-1]
        peak = float(np.max(np.abs(burst))) or 1.0
        burst = (float(BROWSER_REFERENCE_SYNC_PEAK_SCALE) * burst / peak).astype(np.float32)
        return burst

    def _generate_log_sweep(
        self,
        *,
        sample_rate: int,
        duration_seconds: float,
        start_hz: float,
        end_hz: float,
        peak_scale: float = 0.8,
    ) -> np.ndarray:
        sample_count = max(2048, int(round(sample_rate * duration_seconds)))
        t = np.arange(sample_count, dtype=np.float64) / sample_rate
        log_ratio = math.log(end_hz / start_hz)
        phase = 2.0 * math.pi * start_hz * duration_seconds / log_ratio * (np.exp(t * log_ratio / duration_seconds) - 1.0)
        sweep = np.sin(phase).astype(np.float32)
        fade_len = min(sample_count // 8, max(64, int(round(sample_rate * 0.01))))
        if fade_len > 1:
            fade_in = np.linspace(0.0, 1.0, fade_len, dtype=np.float32)
            fade_out = np.linspace(1.0, 0.0, fade_len, dtype=np.float32)
            sweep[:fade_len] *= fade_in
            sweep[-fade_len:] *= fade_out
        peak = float(np.max(np.abs(sweep))) or 1.0
        sweep = (float(peak_scale) * sweep / peak).astype(np.float32)
        return sweep

    def _build_inverse_sweep(
        self,
        sweep: np.ndarray,
        *,
        sample_rate: int,
        duration_seconds: float,
        start_hz: float,
        end_hz: float,
    ) -> np.ndarray:
        sample_count = max(1, int(sweep.size))
        t = np.arange(sample_count, dtype=np.float64) / sample_rate
        log_ratio = math.log(end_hz / start_hz)
        envelope = np.exp(-t * log_ratio / max(duration_seconds, 1e-9))
        inverse = sweep[::-1].astype(np.float64) * envelope
        reference_ir = self._fft_convolve(sweep.astype(np.float64), inverse)
        peak = float(np.max(np.abs(reference_ir)))
        if peak <= 1e-12:
            raise RuntimeError("Unable to build inverse sweep kernel")
        inverse /= peak
        return inverse

    def _estimate_sweep_timing(
        self,
        signal: np.ndarray,
        reference_sweep: np.ndarray,
        coarse_start: int,
        sample_rate: int,
        browser_capture: bool = False,
    ) -> dict[str, Any]:
        edge_anchor_samples = min(
            reference_sweep.size // 2,
            max(4096, int(round(sample_rate * SWEEP_TIMING_ANCHOR_SECONDS))),
        )
        anchor_samples = min(
            edge_anchor_samples,
            max(2048, int(round(sample_rate * SWEEP_TIMING_MULTI_ANCHOR_SECONDS))),
        )
        edge_inset = min(
            max(0, reference_sweep.size // 8),
            max(0, int(round(sample_rate * SWEEP_TIMING_EDGE_INSET_SECONDS))),
        )
        search_margin = max(edge_anchor_samples // 2, int(round(sample_rate * SWEEP_TIMING_SEARCH_SECONDS)))

        anchors = self._build_sweep_timing_anchors(
            reference_sweep=reference_sweep,
            anchor_samples=anchor_samples,
            edge_inset=edge_inset,
        )
        matches = []
        for anchor in anchors:
            expected_start = coarse_start + int(anchor["offset_samples"])
            local_search_margin = search_margin
            if browser_capture and str(anchor.get("name")) in {"start-inner", "start-body", "mid-low"}:
                local_search_margin = int(round(search_margin * BROWSER_SWEEP_TIMING_START_SEARCH_MULTIPLIER))
            search_start = max(0, expected_start - local_search_margin)
            search_end = min(signal.size, expected_start + anchor_samples + local_search_margin)
            match = self._find_best_alignment_in_region(
                signal[search_start:search_end],
                anchor["template"],
            )
            observed_start = search_start + int(match["index"])
            matches.append(
                {
                    **anchor,
                    "expected_start": int(expected_start),
                    "observed_start": int(observed_start),
                    "residual_samples": int(observed_start - expected_start),
                    "score": float(match["score"]),
                    "raw_score": float(match["raw_score"]),
                    "polarity": float(match["polarity"]),
                }
            )

        fit = self._fit_sweep_timing_from_matches(
            matches=matches,
            reference_sweep_samples=reference_sweep.size,
            sample_rate=sample_rate,
            browser_capture=browser_capture,
        )
        aligned_start = int(fit["aligned_start"])
        observed_sweep_samples = int(fit["observed_sweep_samples"])
        stretch_ratio = float(fit["stretch_ratio"])
        drift_ppm = (stretch_ratio - 1.0) * 1_000_000.0
        compensated = (
            SWEEP_TIMING_MIN_COMPENSATION_PPM <= abs(drift_ppm) <= SWEEP_TIMING_MAX_ABS_PPM
            and observed_sweep_samples != reference_sweep.size
        )
        if not compensated:
            observed_sweep_samples = int(reference_sweep.size)
            stretch_ratio = 1.0
            drift_ppm = 0.0

        return {
            "aligned_start": int(aligned_start),
            "aligned_end": int(aligned_start + observed_sweep_samples),
            "observed_sweep_samples": int(observed_sweep_samples),
            "stretch_ratio": stretch_ratio,
            "drift_ppm": drift_ppm,
            "compensated": compensated,
            "anchor_seconds": anchor_samples / sample_rate,
            "start_score": float(fit["start_score"]),
            "end_score": float(fit["end_score"]),
            "anchor_strategy": "multi-anchor weighted fit",
            "anchor_matches": fit["anchor_matches"],
        }

    def _build_sweep_timing_anchors(
        self,
        *,
        reference_sweep: np.ndarray,
        anchor_samples: int,
        edge_inset: int,
    ) -> list[dict[str, Any]]:
        max_start = max(0, reference_sweep.size - anchor_samples)
        min_start = min(max_start, max(0, edge_inset))
        max_start = max(min_start, max_start - edge_inset)
        anchors = []
        for name, fraction in SWEEP_TIMING_ANCHOR_LAYOUT:
            center = int(round(reference_sweep.size * fraction))
            start = center - (anchor_samples // 2)
            start = max(min_start, min(max_start, start))
            end = start + anchor_samples
            anchors.append(
                {
                    "name": name,
                    "offset_samples": int(start),
                    "template": reference_sweep[start:end],
                }
            )
        return anchors

    def _fit_sweep_timing_from_matches(
        self,
        *,
        matches: list[dict[str, Any]],
        reference_sweep_samples: int,
        sample_rate: int,
        browser_capture: bool = False,
    ) -> dict[str, Any]:
        if len(matches) < 2:
            raise RuntimeError("Sweep timing fit did not have enough anchors")

        offsets = np.array([float(item["offset_samples"]) for item in matches], dtype=np.float64)
        observed = np.array([float(item["observed_start"]) for item in matches], dtype=np.float64)
        scores = np.array([max(float(item.get("score") or 0.0), 1e-6) for item in matches], dtype=np.float64)
        weights = np.square(scores)
        if browser_capture:
            weights = np.array([
                weights[index] * float(BROWSER_TIMING_WEIGHT_MULTIPLIERS.get(str(item.get("name") or ""), 1.0))
                for index, item in enumerate(matches)
            ], dtype=np.float64)
        design = np.column_stack([np.ones(offsets.size, dtype=np.float64), offsets])
        sqrt_weights = np.sqrt(weights)
        weighted_design = design * sqrt_weights[:, None]
        weighted_observed = observed * sqrt_weights
        coeffs, *_ = np.linalg.lstsq(weighted_design, weighted_observed, rcond=None)
        intercept = float(coeffs[0])
        slope = float(coeffs[1])
        residuals = observed - (intercept + slope * offsets)
        residual_limit = max(1.0, sample_rate * SWEEP_TIMING_RESIDUAL_TOLERANCE_SECONDS)
        inlier_mask = np.abs(residuals) <= residual_limit

        if int(np.count_nonzero(inlier_mask)) >= 3 and not bool(np.all(inlier_mask)):
            inlier_design = design[inlier_mask]
            inlier_weights = weights[inlier_mask]
            inlier_sqrt = np.sqrt(inlier_weights)
            coeffs, *_ = np.linalg.lstsq(inlier_design * inlier_sqrt[:, None], observed[inlier_mask] * inlier_sqrt, rcond=None)
            intercept = float(coeffs[0])
            slope = float(coeffs[1])
            residuals = observed - (intercept + slope * offsets)
        else:
            inlier_mask = np.ones(offsets.size, dtype=bool)

        slope = min(max(slope, 0.5), 1.5)
        observed_sweep_samples = int(round(reference_sweep_samples * slope))
        min_sweep = max(int(reference_sweep_samples * 0.5), 1)
        if observed_sweep_samples < min_sweep:
            observed_sweep_samples = int(reference_sweep_samples)
            slope = 1.0

        start_score = self._aggregate_anchor_region_score(matches, inlier_mask, region="start", browser_capture=browser_capture)
        end_score = self._aggregate_anchor_region_score(matches, inlier_mask, region="end", browser_capture=browser_capture)
        anchor_matches = []
        for index, item in enumerate(matches):
            anchor_matches.append(
                {
                    "name": str(item["name"]),
                    "offset_samples": int(item["offset_samples"]),
                    "score": round(float(item["score"]), 5),
                    "raw_score": round(float(item["raw_score"]), 5),
                    "polarity": int(-1 if float(item["polarity"]) < 0 else 1),
                    "observed_start": int(item["observed_start"]),
                    "residual_samples": int(round(float(residuals[index]))),
                    "inlier": bool(inlier_mask[index]),
                }
            )

        return {
            "aligned_start": int(round(intercept)),
            "observed_sweep_samples": int(observed_sweep_samples),
            "stretch_ratio": float(slope),
            "start_score": float(start_score),
            "end_score": float(end_score),
            "anchor_matches": anchor_matches,
        }

    def _aggregate_anchor_region_score(
        self,
        matches: list[dict[str, Any]],
        inlier_mask: np.ndarray,
        *,
        region: str,
        browser_capture: bool = False,
    ) -> float:
        if region == "start":
            labels = {"start-inner", "start-body", "mid-low"}
        else:
            labels = {"mid-high", "end-body", "end-inner"}

        def collect_scores(valid_labels: set[str], only_inliers: bool = True) -> list[float]:
            values = []
            for index, item in enumerate(matches):
                if str(item.get("name")) not in valid_labels:
                    continue
                if only_inliers and not bool(inlier_mask[index]):
                    continue
                values.append(float(item.get("score") or 0.0))
            return values

        region_scores = collect_scores(labels, only_inliers=True)
        if not region_scores:
            region_scores = collect_scores(labels, only_inliers=False)
        if not region_scores:
            return 0.0
        region_scores.sort(reverse=True)
        top_scores = region_scores[:2]
        base_score = float(sum(top_scores) / len(top_scores))
        if not browser_capture:
            return base_score

        middle_labels = {"mid-low", "mid-high"}
        middle_scores = collect_scores(middle_labels, only_inliers=True)
        if len(middle_scores) < 2:
            middle_scores = collect_scores(middle_labels, only_inliers=False)
        if not middle_scores:
            return base_score
        middle_scores.sort(reverse=True)
        middle_score = float(sum(middle_scores[:2]) / len(middle_scores[:2]))
        return float((base_score * 0.6) + (middle_score * 0.4))

    def _find_best_alignment_in_region(self, region: np.ndarray, template: np.ndarray) -> dict[str, float]:
        region64 = region.astype(np.float64)
        template64 = template.astype(np.float64)
        if region64.size < template64.size:
            raise RuntimeError("Timing search region was too short for sweep alignment")
        corr = self._fft_correlate(region64, template64[::-1])
        valid = corr[template64.size - 1 : region64.size]
        if valid.size == 0:
            raise RuntimeError("Unable to refine sweep timing")
        index = int(np.argmax(np.abs(valid)))
        snippet = region64[index : index + template64.size]
        denominator = float(np.linalg.norm(snippet) * np.linalg.norm(template64))
        raw_score = 0.0 if denominator <= 1e-12 else float(np.dot(snippet, template64) / denominator)
        return {
            "index": float(index),
            "score": abs(raw_score),
            "raw_score": raw_score,
            "polarity": -1.0 if raw_score < 0 else 1.0,
        }

    def _window_impulse_response(self, impulse_response: np.ndarray, sample_rate: int) -> tuple[np.ndarray, dict[str, Any]]:
        ir64 = impulse_response.astype(np.float64)
        peak_index = int(np.argmax(np.abs(ir64)))
        pre_samples = max(32, int(round(sample_rate * IR_WINDOW_PRE_SECONDS)))
        post_samples = max(pre_samples * 2, int(round(sample_rate * IR_WINDOW_POST_SECONDS)))
        fade_samples = max(16, int(round(sample_rate * IR_WINDOW_FADE_SECONDS)))
        start = max(0, peak_index - pre_samples)
        end = min(ir64.size, peak_index + post_samples)
        window = np.zeros(ir64.size, dtype=np.float64)
        core = end - start
        if core <= 0:
            raise RuntimeError("Impulse-response window could not be constructed")
        shaped = np.ones(core, dtype=np.float64)
        rise = min(fade_samples, peak_index - start)
        fall = min(fade_samples, end - peak_index)
        if rise > 1:
            shaped[:rise] = 0.5 - 0.5 * np.cos(np.linspace(0.0, math.pi, rise, dtype=np.float64))
        if fall > 1:
            shaped[-fall:] = 0.5 + 0.5 * np.cos(np.linspace(0.0, math.pi, fall, dtype=np.float64))
        window[start:end] = shaped
        windowed = ir64 * window
        peak = float(np.max(np.abs(windowed)))
        return windowed, {
            "peak_index": peak_index,
            "peak_seconds": peak_index / sample_rate,
            "window_start_index": start,
            "window_end_index": end,
            "window_seconds": (end - start) / sample_rate,
            "pre_window_seconds": (peak_index - start) / sample_rate,
            "post_window_seconds": (end - peak_index) / sample_rate,
            "peak_dbfs": 20.0 * math.log10(max(peak, 1e-12)),
        }

    def _resample_signal(self, signal: np.ndarray, target_size: int) -> np.ndarray:
        signal64 = signal.astype(np.float64)
        if target_size <= 0:
            raise RuntimeError("Invalid resample target size")
        if signal64.size == target_size:
            return signal64
        if signal64.size < 2:
            raise RuntimeError("Sweep segment was too short to resample")
        source_positions = np.linspace(0.0, 1.0, signal64.size, dtype=np.float64)
        target_positions = np.linspace(0.0, 1.0, target_size, dtype=np.float64)
        return np.interp(target_positions, source_positions, signal64)

    def _find_sweep_start(self, signal: np.ndarray, reference_sweep: np.ndarray) -> int:
        signal64 = signal.astype(np.float64)
        sweep64 = reference_sweep.astype(np.float64)
        corr = self._fft_correlate(signal64, sweep64[::-1])
        valid = corr[sweep64.size - 1 : signal64.size]
        if valid.size == 0:
            raise RuntimeError("Unable to align recorded sweep")
        start_index = int(np.argmax(np.abs(valid)))
        return start_index

    def _fft_correlate(self, signal: np.ndarray, kernel: np.ndarray) -> np.ndarray:
        fft_size = self._next_pow2(signal.size + kernel.size - 1)
        signal_fft = np.fft.rfft(signal, n=fft_size)
        kernel_fft = np.fft.rfft(kernel, n=fft_size)
        corr = np.fft.irfft(signal_fft * kernel_fft, n=fft_size)
        return corr[: signal.size + kernel.size - 1]

    def _fft_convolve(self, signal: np.ndarray, kernel: np.ndarray) -> np.ndarray:
        fft_size = self._next_pow2(signal.size + kernel.size - 1)
        signal_fft = np.fft.rfft(signal, n=fft_size)
        kernel_fft = np.fft.rfft(kernel, n=fft_size)
        convolved = np.fft.irfft(signal_fft * kernel_fft, n=fft_size)
        return convolved[: signal.size + kernel.size - 1]

    def _resolve_playback_target(self) -> dict[str, Any]:
        overview = get_audio_output_overview()
        current_output = overview.get("current_output") or {}
        selected_output = overview.get("selected_output") or {}
        default_output = overview.get("default_output") or {}
        target_name = str(
            current_output.get("name")
            or current_output.get("target_name")
            or selected_output.get("target_name")
            or default_output.get("target_name")
            or ""
        ).strip()
        if not target_name:
            raise RuntimeError("No active output target is available for sweep playback")
        return {
            "target_name": target_name,
            "target_label": str(
                current_output.get("label")
                or current_output.get("target_label")
                or selected_output.get("label")
                or selected_output.get("target_label")
                or default_output.get("label")
                or default_output.get("target_label")
                or target_name
            ),
            "active_rate": current_output.get("active_rate") or selected_output.get("active_rate") or default_output.get("active_rate"),
        }

    def _resolve_measurement_sample_rate(self) -> int:
        status = get_samplerate_status()
        clock_rate = status.get("clock_rate")
        if clock_rate:
            try:
                resolved = int(clock_rate)
                if resolved > 0:
                    return resolved
            except (TypeError, ValueError):
                pass

        overview = get_audio_output_overview()
        current_output = overview.get("current_output") or {}
        selected_output = overview.get("selected_output") or {}
        default_output = overview.get("default_output") or {}
        for candidate in (
            current_output.get("active_rate"),
            selected_output.get("active_rate"),
            default_output.get("active_rate"),
            status.get("active_rate"),
            status.get("default_rate"),
            48_000,
        ):
            try:
                resolved = int(candidate)
            except (TypeError, ValueError):
                continue
            if resolved > 0:
                return resolved
        return 48_000

    def _resolve_host_reference_capture(
        self,
        *,
        playback_target: dict[str, Any],
        mic_source_node_name: str,
        requested_channel: str,
    ) -> dict[str, str]:
        sink_node_name = str(playback_target.get("target_name") or "").strip()
        if not sink_node_name:
            raise RuntimeError("No active output sink is available for host-reference capture")
        if not mic_source_node_name or mic_source_node_name.endswith(".monitor"):
            raise RuntimeError("Host-reference capture requires a real microphone source")
        monitor_source_node_name = f"{sink_node_name}.monitor"
        monitor_channel = "right" if requested_channel == "right" else "left"
        monitor_ports = self._list_source_output_ports(monitor_source_node_name)
        preferred_suffixes = [":monitor_FR", ":output_FR", ":capture_FR", ":capture_MONO", ":output_MONO"] if monitor_channel == "right" else [":monitor_FL", ":output_FL", ":capture_FL", ":capture_MONO", ":output_MONO"]
        if not self._pick_port(monitor_ports, preferred_suffixes):
            raise RuntimeError(f"Active sink monitor for {requested_channel} is not available on {sink_node_name}")
        return {
            "source_node_name": monitor_source_node_name,
            "sink_node_name": sink_node_name,
            "channel": monitor_channel,
            "channel_label": f"monitor_{'FR' if monitor_channel == 'right' else 'FL'}",
        }

    def _load_wav_array(self, capture_path: Path) -> tuple[int, np.ndarray]:
        with wave.open(str(capture_path), "rb") as handle:
            sample_rate = int(handle.getframerate())
            channels = int(handle.getnchannels())
            sample_width = int(handle.getsampwidth())
            frames = handle.readframes(handle.getnframes())
        if sample_width != 2:
            raise RuntimeError(f"Unsupported sample width from capture: {sample_width * 8}-bit")
        signal = np.frombuffer(frames, dtype=np.int16).astype(np.float32)
        if channels > 1:
            signal = signal.reshape(-1, channels)
        else:
            signal = signal.reshape(-1, 1)
        signal /= 32768.0
        return sample_rate, signal

    def _load_wav_signal(self, capture_path: Path, *, channel: str) -> tuple[int, np.ndarray]:
        sample_rate, raw_signal = self._load_wav_array(capture_path)
        return sample_rate, self._select_analysis_channel(raw_signal, channel=channel)

    def _detect_browser_capture_playback_leak(self, capture_path: Path, playback_path: Path | None) -> dict[str, float] | None:
        if playback_path is None or not playback_path.exists():
            return None
        try:
            capture_sample_rate, capture_signal = self._load_wav_array(capture_path)
            playback_sample_rate, playback_signal = self._load_wav_array(playback_path)
        except Exception:
            return None
        if capture_sample_rate != playback_sample_rate or capture_signal.shape != playback_signal.shape:
            return None
        capture64 = capture_signal.astype(np.float64).reshape(-1)
        playback64 = playback_signal.astype(np.float64).reshape(-1)
        if capture64.size == 0 or playback64.size == 0:
            return None
        residual = capture64 - playback64
        playback_rms = float(np.sqrt(np.mean(np.square(playback64))))
        residual_rms = float(np.sqrt(np.mean(np.square(residual))))
        if playback_rms <= 1e-9:
            return None
        correlation = 0.0
        capture_std = float(np.std(capture64))
        playback_std = float(np.std(playback64))
        if capture_std > 1e-9 and playback_std > 1e-9:
            correlation = float(np.corrcoef(capture64, playback64)[0, 1])
        residual_db = 20.0 * math.log10(max(residual_rms / playback_rms, 1e-12))
        max_abs_diff = float(np.max(np.abs(residual)))
        if correlation >= 0.999999 and (residual_db <= -90.0 or max_abs_diff <= (1.0 / 32768.0)):
            return {
                "correlation": correlation,
                "residual_db": residual_db,
                "max_abs_diff": max_abs_diff,
            }
        return None

    def _select_analysis_channel(self, raw_signal: np.ndarray, *, channel: str, channel_index: int | None = None) -> np.ndarray:
        if raw_signal.ndim == 1 or raw_signal.shape[1] == 1:
            return raw_signal.reshape(-1)
        if channel_index is not None:
            normalized_index = max(0, min(int(channel_index), raw_signal.shape[1] - 1))
            return raw_signal[:, normalized_index]
        if channel == "right" and raw_signal.shape[1] >= 2:
            return raw_signal[:, 1]
        if channel == "stereo":
            return np.mean(raw_signal[:, : min(raw_signal.shape[1], 2)], axis=1)
        return raw_signal[:, 0]

    def _build_capture_audit(
        self,
        *,
        raw_signal: np.ndarray,
        sample_rate: int,
        browser_capture_meta: dict[str, Any] | None,
    ) -> dict[str, Any]:
        normalized_meta = self._normalize_browser_capture_meta(browser_capture_meta)
        channel_count = int(raw_signal.shape[1]) if raw_signal.ndim > 1 else 1
        duration_seconds = float(raw_signal.shape[0]) / float(sample_rate)
        peak = float(np.max(np.abs(raw_signal))) if raw_signal.size else 0.0
        rms = float(np.sqrt(np.mean(np.square(raw_signal, dtype=np.float64)))) if raw_signal.size else 0.0
        per_channel_peak_dbfs = []
        per_channel_rms_dbfs = []
        for channel_index in range(channel_count):
            channel_signal = raw_signal[:, channel_index] if channel_count > 1 else raw_signal.reshape(-1)
            channel_peak = float(np.max(np.abs(channel_signal))) if channel_signal.size else 0.0
            channel_rms = float(np.sqrt(np.mean(np.square(channel_signal, dtype=np.float64)))) if channel_signal.size else 0.0
            per_channel_peak_dbfs.append(round(20.0 * math.log10(max(channel_peak, 1e-9)), 2))
            per_channel_rms_dbfs.append(round(20.0 * math.log10(max(channel_rms, 1e-9)), 2))

        stereo_correlation = None
        stereo_level_delta_db = None
        if channel_count >= 2:
            left = raw_signal[:, 0].astype(np.float64)
            right = raw_signal[:, 1].astype(np.float64)
            left_rms = float(np.sqrt(np.mean(np.square(left)))) if left.size else 0.0
            right_rms = float(np.sqrt(np.mean(np.square(right)))) if right.size else 0.0
            if left_rms > 1e-9 and right_rms > 1e-9:
                stereo_level_delta_db = round(20.0 * math.log10(max(left_rms, 1e-9) / max(right_rms, 1e-9)), 3)
            left_std = float(np.std(left))
            right_std = float(np.std(right))
            if left_std > 1e-9 and right_std > 1e-9:
                stereo_correlation = round(float(np.corrcoef(left, right)[0, 1]), 6)

        return {
            "sample_rate": int(sample_rate),
            "channels": channel_count,
            "duration_seconds": round(duration_seconds, 3),
            "peak_dbfs": round(20.0 * math.log10(max(peak, 1e-9)), 2),
            "rms_dbfs": round(20.0 * math.log10(max(rms, 1e-9)), 2),
            "per_channel_peak_dbfs": per_channel_peak_dbfs,
            "per_channel_rms_dbfs": per_channel_rms_dbfs,
            "stereo_correlation": stereo_correlation,
            "stereo_level_delta_db": stereo_level_delta_db,
            "browser_capture_meta": normalized_meta,
        }

    def _normalize_browser_capture_meta(self, browser_capture_meta: dict[str, Any] | None) -> dict[str, Any]:
        meta = browser_capture_meta if isinstance(browser_capture_meta, dict) else {}
        settings = meta.get("trackSettings") if isinstance(meta.get("trackSettings"), dict) else {}
        constraints = meta.get("trackConstraints") if isinstance(meta.get("trackConstraints"), dict) else {}
        capabilities = meta.get("trackCapabilities") if isinstance(meta.get("trackCapabilities"), dict) else {}
        recorder = meta.get("recorder") if isinstance(meta.get("recorder"), dict) else {}
        browser = meta.get("browser") if isinstance(meta.get("browser"), dict) else {}

        def normalize_scalar(value: Any) -> Any:
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return value
            if isinstance(value, str):
                text = value.strip()
                return text or None
            return None

        def normalize_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
            return {str(key): normalize_scalar(value) for key, value in mapping.items() if normalize_scalar(value) is not None}

        def normalize_number_list(values: Any) -> list[float]:
            if not isinstance(values, list):
                return []
            normalized: list[float] = []
            for value in values:
                if value in {None, ""}:
                    continue
                try:
                    normalized.append(float(value))
                except (TypeError, ValueError):
                    continue
            return normalized

        return {
            "input_label": str(meta.get("inputLabel") or "").strip(),
            "requested_input_id": str(meta.get("requestedInputId") or "").strip(),
            "secure_context": bool(meta.get("secureContext")),
            "track_settings": {
                "device_id": str(settings.get("deviceId") or "").strip(),
                "channel_count": int(settings.get("channelCount") or 0) if str(settings.get("channelCount") or "").strip() else None,
                "sample_rate": int(settings.get("sampleRate") or 0) if str(settings.get("sampleRate") or "").strip() else None,
                "echo_cancellation": settings.get("echoCancellation"),
                "noise_suppression": settings.get("noiseSuppression"),
                "auto_gain_control": settings.get("autoGainControl"),
                "latency": float(settings.get("latency")) if settings.get("latency") not in {None, ""} else None,
                "sample_size": int(settings.get("sampleSize") or 0) if str(settings.get("sampleSize") or "").strip() else None,
            },
            "track_constraints": normalize_mapping(constraints),
            "track_capabilities": normalize_mapping(capabilities),
            "recorder": {
                "processing_model": str(recorder.get("processingModel") or "").strip(),
                "sample_rate": int(recorder.get("sampleRate") or 0) if str(recorder.get("sampleRate") or "").strip() else None,
                "channel_count": int(recorder.get("channelCount") or 0) if str(recorder.get("channelCount") or "").strip() else None,
                "base_latency": float(recorder.get("baseLatency")) if recorder.get("baseLatency") not in {None, ""} else None,
                "output_latency": float(recorder.get("outputLatency")) if recorder.get("outputLatency") not in {None, ""} else None,
                "context_state": str(recorder.get("contextState") or "").strip(),
                "input_channel_count": int(recorder.get("inputChannelCount") or 0) if str(recorder.get("inputChannelCount") or "").strip() else None,
                "peak": float(recorder.get("peak")) if recorder.get("peak") not in {None, ""} else None,
                "rms": float(recorder.get("rms")) if recorder.get("rms") not in {None, ""} else None,
                "frames_captured": int(recorder.get("framesCaptured") or 0) if str(recorder.get("framesCaptured") or "").strip() else None,
                "total_samples": int(recorder.get("totalSamples") or 0) if str(recorder.get("totalSamples") or "").strip() else None,
                "per_channel_peak": normalize_number_list(recorder.get("perChannelPeak")),
                "per_channel_rms": normalize_number_list(recorder.get("perChannelRms")),
                "per_channel_samples": normalize_number_list(recorder.get("perChannelSamples")),
            },
            "browser": {
                "user_agent": str(browser.get("userAgent") or "").strip(),
                "platform": str(browser.get("platform") or "").strip(),
                "language": str(browser.get("language") or "").strip(),
                "visibility_state": str(browser.get("visibilityState") or "").strip(),
            },
        }

    def _build_capture_quality_checks(
        self,
        *,
        capture_audit: dict[str, Any],
        timing: dict[str, Any],
        peak_dbfs: float,
        trusted_band_meta: dict[str, Any],
        trusted_max_hz: float,
        response_outliers: list[dict[str, float | str]] | None = None,
        capture_label: str = "Capture",
        expect_dual_mono_channels: bool = True,
    ) -> dict[str, Any]:
        items: list[dict[str, str]] = []

        def add(level: str, code: str, message: str) -> None:
            items.append({"level": level, "code": code, "message": message})

        meta = capture_audit.get("browser_capture_meta") or {}
        settings = meta.get("track_settings") or {}
        recorder = meta.get("recorder") or {}
        capture_subject = capture_label if capture_label else "Capture"
        capture_subject_lower = capture_subject[:1].lower() + capture_subject[1:] if capture_subject else "capture"
        playback_subject = "browser/playback" if meta else "capture/playback"
        if settings.get("echo_cancellation") is True:
            add("error", "echo-cancellation-enabled", "Browser mic echo cancellation stayed enabled during capture.")
        if settings.get("noise_suppression") is True:
            add("error", "noise-suppression-enabled", "Browser mic noise suppression stayed enabled during capture.")
        if settings.get("auto_gain_control") is True:
            add("error", "auto-gain-enabled", "Browser mic automatic gain control stayed enabled during capture.")
        track_sample_rate = settings.get("sample_rate")
        if track_sample_rate and int(track_sample_rate) != BROWSER_MEASUREMENT_SAMPLE_RATE:
            add("error", "unexpected-track-sample-rate", f"Browser track ran at {track_sample_rate} Hz instead of {BROWSER_MEASUREMENT_SAMPLE_RATE} Hz.")
        recorder_model = recorder.get("processing_model")
        if recorder_model and recorder_model != "audio-worklet":
            add("warning", "script-processor-fallback", "Browser recorder fell back to ScriptProcessor; capture timing may still be fragile.")
        if peak_dbfs >= CAPTURE_CLIP_FAIL_DBFS:
            add("error", "capture-clipped", f"Recorded sweep clipped at {peak_dbfs:.2f} dBFS.")
        elif peak_dbfs >= CAPTURE_CLIP_WARN_DBFS:
            add("warning", "capture-near-clipping", f"Recorded sweep peaked very close to clipping ({peak_dbfs:.2f} dBFS).")
        rms_dbfs = float(capture_audit.get("rms_dbfs") or 0.0)
        if meta and (peak_dbfs <= BROWSER_CAPTURE_LEVEL_WARN_PEAK_DBFS or rms_dbfs <= BROWSER_CAPTURE_LEVEL_WARN_RMS_DBFS):
            input_label = str(meta.get("input_label") or "Browser microphone").strip() or "Browser microphone"
            add(
                "warning",
                "capture-level-low",
                f"Browser capture level was unusually low for sweep analysis (peak {peak_dbfs:.2f} dBFS, rms {rms_dbfs:.2f} dBFS, input {input_label}). This often means the wrong browser mic path, browser/OS input attenuation, or a much weaker acoustic capture state.",
            )
        drift_ppm = abs(float(timing.get("drift_ppm") or 0.0))
        timing_method = str(timing.get("method") or "")
        if timing_method == "sync-scaffold-affine-reference":
            accepted_total = int(timing.get("accepted_burst_count") or 0)
            accepted_cluster_a = int(timing.get("accepted_cluster_a_count") or 0)
            accepted_cluster_b = int(timing.get("accepted_cluster_b_count") or 0)
            fit_residual_ms = float(timing.get("fit_residual_rms_ms") or 0.0)
            max_burst_residual_ms = float(timing.get("max_burst_residual_ms") or 0.0)
            corrected_sweep_score = float(timing.get("corrected_sweep_score") or 0.0)
            cluster_order_valid = bool(timing.get("cluster_order_valid"))
            burst_matches = timing.get("burst_matches") or []
            if accepted_total < BROWSER_SYNC_MIN_TOTAL_BURSTS:
                add("error", "insufficient-sync-bursts", f"Only {accepted_total}/6 sync bursts were trustworthy; need at least {BROWSER_SYNC_MIN_TOTAL_BURSTS}.")
            elif accepted_total == BROWSER_SYNC_MIN_TOTAL_BURSTS:
                add("warning", "sync-burst-count-low", f"Only {accepted_total}/6 sync bursts were trustworthy; this run is usable but thin.")
            if accepted_cluster_a < BROWSER_SYNC_MIN_CLUSTER_BURSTS:
                add("error", "sync-cluster-a-insufficient", f"Cluster A only produced {accepted_cluster_a} trustworthy sync burst(s).")
            if accepted_cluster_b < BROWSER_SYNC_MIN_CLUSTER_BURSTS:
                add("error", "sync-cluster-b-insufficient", f"Cluster B only produced {accepted_cluster_b} trustworthy sync burst(s).")
            if not cluster_order_valid:
                add("error", "sync-order-invalid", "Recovered sync bursts did not preserve the expected scaffold order.")
            if fit_residual_ms > BROWSER_SYNC_RESIDUAL_FAIL_MS:
                add("error", "sync-fit-residual-high", f"Sync scaffold fit residual stayed too high ({fit_residual_ms:.3f} ms RMS).")
            elif fit_residual_ms > BROWSER_SYNC_RESIDUAL_WARN_MS:
                add("warning", "sync-fit-residual-soft", f"Sync scaffold fit residual was higher than expected ({fit_residual_ms:.3f} ms RMS).")
            if max_burst_residual_ms > BROWSER_SYNC_MAX_RESIDUAL_FAIL_MS:
                add("error", "sync-burst-residual-high", f"At least one corrected sync burst stayed too far off ({max_burst_residual_ms:.3f} ms).")
            elif max_burst_residual_ms > BROWSER_SYNC_MAX_RESIDUAL_WARN_MS:
                add("warning", "sync-burst-residual-soft", f"At least one corrected sync burst was softer than expected ({max_burst_residual_ms:.3f} ms).")
            weak_bursts = [item for item in burst_matches if float(item.get("score") or 0.0) < BROWSER_SYNC_SCORE_WARN_THRESHOLD]
            ambiguous_bursts = [item for item in burst_matches if float(item.get("peak_ratio") or 0.0) < BROWSER_SYNC_RATIO_WARN_THRESHOLD]
            if weak_bursts:
                add("warning", "sync-burst-score-soft", f"{len(weak_bursts)} sync burst(s) matched more softly than expected.")
            if ambiguous_bursts:
                add("warning", "sync-burst-ratio-soft", f"{len(ambiguous_bursts)} sync burst(s) had ambiguous peak separation.")
            if drift_ppm > BROWSER_DRIFT_FAIL_PPM:
                add("error", "browser-clock-drift-excessive", f"Observed browser/playback drift was too large ({drift_ppm:.0f} ppm).")
            elif drift_ppm > BROWSER_DRIFT_WARN_PPM:
                add("warning", "clock-drift-high", f"Observed browser/playback drift was high ({drift_ppm:.0f} ppm).")
            if corrected_sweep_score < BROWSER_CORRECTED_SWEEP_SCORE_FAIL:
                add("error", "corrected-sweep-weak", f"Corrected sweep-body confidence stayed too weak ({corrected_sweep_score:.3f}).")
            elif corrected_sweep_score < BROWSER_CORRECTED_SWEEP_SCORE_WARN:
                add("warning", "corrected-sweep-soft", f"Corrected sweep-body confidence was softer than expected ({corrected_sweep_score:.3f}).")
        elif timing_method == "marker-affine-reference":
            marker_a_score = float(timing.get("marker_a_score") or 0.0)
            marker_b_score = float(timing.get("marker_b_score") or 0.0)
            marker_a_ratio = float(timing.get("marker_a_peak_ratio") or 0.0)
            marker_b_ratio = float(timing.get("marker_b_peak_ratio") or 0.0)
            fit_residual_ms = float(timing.get("marker_fit_residual_ms") or 0.0)
            corrected_sweep_score = float(timing.get("corrected_sweep_score") or 0.0)
            if marker_a_score < 0.82:
                add("error", "marker-a-weak", f"Marker A detection was too weak ({marker_a_score:.3f}).")
            elif marker_a_score < 0.90:
                add("warning", "marker-a-soft", f"Marker A detection was softer than expected ({marker_a_score:.3f}).")
            if marker_b_score < 0.82:
                add("error", "marker-b-weak", f"Marker B detection was too weak ({marker_b_score:.3f}).")
            elif marker_b_score < 0.90:
                add("warning", "marker-b-soft", f"Marker B detection was softer than expected ({marker_b_score:.3f}).")
            if marker_a_ratio < 1.4:
                add("error", "marker-a-ambiguous", f"Marker A correlation was ambiguous (peak ratio {marker_a_ratio:.2f}).")
            elif marker_a_ratio < 1.8:
                add("warning", "marker-a-peak-ratio-soft", f"Marker A peak ratio was softer than expected ({marker_a_ratio:.2f}).")
            if marker_b_ratio < 1.4:
                add("error", "marker-b-ambiguous", f"Marker B correlation was ambiguous (peak ratio {marker_b_ratio:.2f}).")
            elif marker_b_ratio < 1.8:
                add("warning", "marker-b-peak-ratio-soft", f"Marker B peak ratio was softer than expected ({marker_b_ratio:.2f}).")
            if fit_residual_ms > 1.0:
                add("error", "marker-fit-residual-high", f"Marker timing residual stayed too high after correction ({fit_residual_ms:.3f} ms).")
            elif fit_residual_ms > 0.5:
                add("warning", "marker-fit-residual-soft", f"Marker timing residual was higher than expected ({fit_residual_ms:.3f} ms).")
            if drift_ppm > BROWSER_DRIFT_FAIL_PPM:
                add("error", "browser-clock-drift-excessive", f"Observed browser/playback drift was too large ({drift_ppm:.0f} ppm).")
            elif drift_ppm > BROWSER_DRIFT_WARN_PPM:
                add("warning", "clock-drift-high", f"Observed browser/playback drift was high ({drift_ppm:.0f} ppm).")
            if corrected_sweep_score < BROWSER_CORRECTED_SWEEP_SCORE_FAIL:
                add("error", "corrected-sweep-weak", f"Corrected sweep-body confidence stayed too weak ({corrected_sweep_score:.3f}).")
            elif corrected_sweep_score < BROWSER_CORRECTED_SWEEP_SCORE_WARN:
                add("warning", "corrected-sweep-soft", f"Corrected sweep-body confidence was softer than expected ({corrected_sweep_score:.3f}).")
        else:
            alignment_fail_threshold = ALIGNMENT_SCORE_FAIL_THRESHOLD
            alignment_warn_threshold = ALIGNMENT_SCORE_WARN_THRESHOLD
            if capture_label == "Host-local capture":
                alignment_fail_threshold = HOST_ALIGNMENT_SCORE_FAIL_THRESHOLD
                alignment_warn_threshold = HOST_ALIGNMENT_SCORE_WARN_THRESHOLD
            start_score = float(timing.get("start_score") or 0.0)
            end_score = float(timing.get("end_score") or 0.0)
            browser_start_marginal_but_usable = bool(meta) and all([
                start_score >= 0.87,
                start_score < alignment_fail_threshold,
                end_score >= 0.947,
                peak_dbfs > -6.5,
                rms_dbfs > -21.5,
                drift_ppm <= 1000.0,
            ])
            if start_score < alignment_fail_threshold:
                if browser_start_marginal_but_usable:
                    add("warning", "soft-start-alignment", f"Sweep start alignment score was slightly soft for browser capture but still within the currently accepted usable range ({start_score:.3f}).")
                else:
                    add("error", "weak-start-alignment", f"Sweep start alignment score was too weak ({start_score:.3f}).")
            elif start_score < alignment_warn_threshold:
                add("warning", "soft-start-alignment", f"Sweep start alignment score was softer than expected ({start_score:.3f}).")
            if end_score < alignment_fail_threshold:
                add("error", "weak-end-alignment", f"Sweep end alignment score was too weak ({end_score:.3f}).")
            elif end_score < alignment_warn_threshold:
                add("warning", "soft-end-alignment", f"Sweep end alignment score was softer than expected ({end_score:.3f}).")
            if drift_ppm > CLOCK_DRIFT_WARN_PPM:
                add("warning", "clock-drift-high", f"Observed {playback_subject} clock drift was high ({drift_ppm:.0f} ppm).")
        stereo_correlation = capture_audit.get("stereo_correlation")
        if expect_dual_mono_channels and capture_audit.get("channels", 1) >= 2 and stereo_correlation is not None and stereo_correlation < CHANNEL_CORRELATION_WARN_THRESHOLD:
            add("warning", "stereo-mismatch", f"{capture_subject} channels were not close dual-mono (corr {stereo_correlation:.3f}).")
        if trusted_band_meta.get("upper_edge_guard_applied"):
            add(
                "warning",
                "upper-edge-review-only",
                f"Trusted comparison trace stops at {float(trusted_max_hz):.0f} Hz because the final {capture_subject_lower} edge point(s) were unstable; inspect raw/full-band review above that range only.",
            )
        elif not trusted_band_meta.get("stable_high_edge", True):
            add("warning", "high-edge-unstable", f"Trusted comparison trace stops at {float(trusted_max_hz):.0f} Hz because the high-frequency edge was unstable.")
        if response_outliers:
            worst_outlier = max(response_outliers, key=lambda item: float(item.get("deviation_db") or 0.0))
            add(
                "warning",
                "response-outlier-detected",
                f"Detected an isolated response outlier near {float(worst_outlier.get('frequency_hz') or 0.0):.0f} Hz ({float(worst_outlier.get('deviation_db') or 0.0):.1f} dB from its local neighborhood). Treat this run as less reproducible.",
            )

        status = "pass"
        if any(item["level"] == "error" for item in items):
            status = "fail"
        elif any(item["level"] == "warning" for item in items):
            status = "warn"
        return {"status": status, "items": items}

    def _write_wav(self, path: Path, samples: np.ndarray, sample_rate: int) -> None:
        clipped = np.clip(samples, -1.0, 1.0)
        int_samples = np.round(clipped * 32767.0).astype(np.int16)
        with wave.open(str(path), "wb") as handle:
            channels = 1 if int_samples.ndim == 1 else int_samples.shape[1]
            handle.setnchannels(channels)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            handle.writeframes(int_samples.tobytes())

    def _store_calibration_file(self, filename: str, data: bytes) -> dict[str, Any]:
        safe_name = self._safe_filename(filename or "calibration.txt")
        target_path = self.calibrations_dir / f"{uuid4().hex[:10]}-{safe_name}"
        target_path.write_bytes(data)
        return {
            "id": target_path.name,
            "filename": safe_name,
            "path": str(target_path),
            "applied": False,
        }

    def _read_settings(self) -> dict[str, Any]:
        if not self.settings_path.exists():
            return {}
        try:
            payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _write_settings(self, settings: dict[str, Any]) -> None:
        self.settings_path.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _resolve_calibration_meta(
        self,
        *,
        calibration_filename: str | None = None,
        calibration_bytes: bytes | None = None,
        calibration_ref: str | None = None,
    ) -> dict[str, Any] | None:
        if calibration_bytes:
            meta = self._store_calibration_file(calibration_filename or "calibration.txt", calibration_bytes)
            self.set_active_calibration_file_id(str(meta.get("id") or ""))
            return meta
        ref = Path(str(calibration_ref or "")).name.strip() or self.get_active_calibration_file_id()
        if ref:
            return self._lookup_calibration_file(ref)
        return None

    def _list_calibration_files(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        seen_filenames: set[str] = set()
        for path in sorted(self.calibrations_dir.glob("*"), key=lambda item: item.stat().st_mtime, reverse=True):
            if not path.is_file():
                continue
            display_name = self._display_calibration_filename(path.name)
            if display_name in seen_filenames:
                continue
            seen_filenames.add(display_name)
            entries.append(
                {
                    "id": path.name,
                    "filename": display_name,
                    "path": str(path),
                    "modified_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
                }
            )
        return entries

    def _lookup_calibration_file(self, calibration_ref: str) -> dict[str, Any] | None:
        ref = Path(str(calibration_ref or "")).name.strip()
        if not ref:
            return None
        path = self.calibrations_dir / ref
        if not path.exists() or not path.is_file():
            return None
        return {
            "id": path.name,
            "filename": self._display_calibration_filename(path.name),
            "path": str(path),
            "applied": False,
        }

    @staticmethod
    def _display_calibration_filename(value: str) -> str:
        name = Path(value or "").name
        return re.sub(r"^[0-9a-f]{10}-", "", name, count=1) or name or "calibration.txt"

    def _parse_calibration_file(self, path: Path) -> tuple[np.ndarray, np.ndarray] | None:
        text = path.read_text(encoding="utf-8", errors="ignore")
        frequencies: list[float] = []
        offsets: list[float] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(("#", ";", "*", "//")):
                continue
            parts = re.split(r"[\s,;]+", line)
            if len(parts) < 2:
                continue
            try:
                frequency = float(parts[0])
                offset = float(parts[1])
            except ValueError:
                continue
            if not math.isfinite(frequency) or not math.isfinite(offset) or frequency <= 0:
                continue
            frequencies.append(frequency)
            offsets.append(offset)
        if len(frequencies) < 2:
            return None
        ordered = sorted(zip(frequencies, offsets), key=lambda item: item[0])
        return (
            np.array([item[0] for item in ordered], dtype=np.float64),
            np.array([item[1] for item in ordered], dtype=np.float64),
        )

    def _discover_capture_inputs(self) -> list[dict[str, Any]]:
        inputs: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_node_names: set[str] = set()
        inputs.extend(self._discover_capture_inputs_from_wpctl(seen_ids, seen_node_names))
        inputs.extend(self._discover_capture_inputs_from_pactl(seen_ids, seen_node_names))
        inputs.sort(key=lambda item: (0 if item.get("is_default") else 1, item.get("label") or item.get("node_name") or item["id"]))
        return inputs

    def _discover_capture_inputs_from_wpctl(self, seen_ids: set[str], seen_node_names: set[str]) -> list[dict[str, Any]]:
        try:
            completed = subprocess.run(["wpctl", "status", "-n"], capture_output=True, text=True, timeout=4)
        except Exception:
            return []
        if completed.returncode != 0:
            return []

        lines = (completed.stdout or "").splitlines()
        active_section: str | None = None
        media_section: str | None = None
        inputs: list[dict[str, Any]] = []
        for line in lines:
            stripped = line.strip()
            if stripped in {"Audio", "Video", "Settings"}:
                media_section = stripped.lower()
                active_section = None
                continue
            if media_section == "audio" and stripped.startswith(("├─ Sources:", "└─ Sources:")):
                active_section = "audio-sources"
                continue
            if stripped.startswith(("├─", "└─")):
                active_section = None
                continue
            if active_section != "audio-sources":
                continue

            match = re.search(r"(?P<star>\*)?\s*(?P<serial>\d+)\.\s+(?P<label>.+?)(?:\s+\[(?P<meta>.*)\])?$", stripped)
            if not match:
                continue
            label = match.group("label").strip()
            if any(token in label for token in (" < ", " > ", ":input_", ":output_", ":monitor_")):
                continue
            serial = match.group("serial")
            label = match.group("label").strip()
            details = self._inspect_source_details(serial)
            node_name = str(details.get("node_name") or label.split()[0]).strip()
            if not node_name or node_name == "easyeffects_source" or node_name.endswith(".monitor"):
                continue
            input_id = f"pw-source-{serial}"
            if input_id in seen_ids or node_name in seen_node_names:
                continue
            seen_ids.add(input_id)
            seen_node_names.add(node_name)
            inputs.append(
                {
                    "id": input_id,
                    "label": label,
                    "kind": "pipewire-source",
                    "available": True,
                    "node_serial": serial,
                    "node_name": node_name,
                    "channels": details.get("channels", 1),
                    "sample_rate": details.get("sample_rate"),
                    "is_default": bool(match.group("star")),
                    "note": "Real PipeWire capture source",
                }
            )
        return inputs

    def _discover_capture_inputs_from_pactl(self, seen_ids: set[str], seen_node_names: set[str]) -> list[dict[str, Any]]:
        try:
            completed = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True, text=True, timeout=4)
        except Exception:
            return []
        if completed.returncode != 0:
            return []

        inputs: list[dict[str, Any]] = []
        for line in (completed.stdout or "").splitlines():
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            serial, node_name, driver, spec = parts[:4]
            node_name = str(node_name or "").strip()
            if not node_name or node_name == "easyeffects_source" or node_name.endswith(".monitor"):
                continue
            input_id = f"pw-source-{serial}"
            if input_id in seen_ids or node_name in seen_node_names:
                continue
            channels_match = re.search(r"(\d+)ch", spec)
            sample_rate_match = re.search(r"(\d+)Hz", spec)
            seen_ids.add(input_id)
            seen_node_names.add(node_name)
            inputs.append(
                {
                    "id": input_id,
                    "label": node_name,
                    "kind": "pipewire-source",
                    "available": True,
                    "node_serial": serial,
                    "node_name": node_name,
                    "channels": int(channels_match.group(1)) if channels_match else 1,
                    "sample_rate": int(sample_rate_match.group(1)) if sample_rate_match else None,
                    "is_default": False,
                    "note": "Real PipeWire capture source",
                    "driver": driver,
                }
            )
        return inputs

    def _inspect_source_details(self, serial: str) -> dict[str, Any]:
        try:
            completed = subprocess.run(["wpctl", "inspect", str(serial)], capture_output=True, text=True, timeout=3)
        except Exception:
            return {}
        if completed.returncode != 0:
            return {}
        details: dict[str, Any] = {}
        for line in (completed.stdout or "").splitlines():
            if "audio.channels" in line:
                match = re.search(r'"(\d+)"', line)
                if match:
                    details["channels"] = int(match.group(1))
            elif "audio.rate" in line:
                match = re.search(r'"(\d+)"', line)
                if match:
                    details["sample_rate"] = int(match.group(1))
            elif "node.name" in line:
                match = re.search(r'"([^"]+)"', line)
                if match:
                    details["node_name"] = match.group(1)
        return details

    def _link_host_reference_capture(
        self,
        *,
        reference_source_node_name: str,
        mic_source_node_name: str,
        record_node_name: str,
        requested_channel: str,
    ) -> None:
        deadline = time.monotonic() + 4.0
        reference_ports: list[str] = []
        mic_ports: list[str] = []
        record_inputs: list[str] = []
        while time.monotonic() < deadline:
            reference_ports = self._list_source_output_ports(reference_source_node_name)
            mic_ports = self._list_source_output_ports(mic_source_node_name)
            record_ports = self._list_pw_ports(record_node_name)
            record_inputs = [port for port in record_ports if ":input_" in port]
            if reference_ports and mic_ports and record_inputs:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError(f"Unable to discover PipeWire ports for host-reference capture into {record_node_name}")

        reference_suffixes = [":monitor_FR", ":output_FR", ":capture_FR", ":capture_MONO", ":output_MONO", ":monitor_FL", ":output_FL", ":capture_FL"] if requested_channel == "right" else [":monitor_FL", ":output_FL", ":capture_FL", ":capture_MONO", ":output_MONO", ":monitor_FR", ":output_FR", ":capture_FR"]
        mic_suffixes = [":capture_MONO", ":output_MONO", ":capture_FL", ":output_FL", ":capture_FR", ":output_FR", ":monitor_FL", ":monitor_FR"]
        reference_port = self._pick_port(reference_ports, reference_suffixes)
        mic_port = self._pick_port(mic_ports, mic_suffixes)
        input_left = self._pick_port(record_inputs, [":input_FL", ":input_MONO"])
        input_right = self._pick_port(record_inputs, [":input_FR", ":input_MONO", ":input_FL"])
        if not reference_port or not mic_port or not input_left or not input_right:
            raise RuntimeError("Could not resolve PipeWire ports for host-reference capture")

        subprocess.run(["pw-link", reference_port, input_left], capture_output=True, text=True, timeout=3, check=True)
        subprocess.run(["pw-link", mic_port, input_right], capture_output=True, text=True, timeout=3, check=True)
        time.sleep(0.15)

    def _link_source_to_record_stream(
        self,
        *,
        source_node_name: str,
        record_node_name: str,
        requested_channel: str,
        capture_channels: int,
    ) -> None:
        deadline = time.monotonic() + 4.0
        source_ports: list[str] = []
        record_ports: list[str] = []
        candidate_source_names = [source_node_name]
        if source_node_name.endswith(".monitor"):
            candidate_source_names.append(source_node_name[: -len(".monitor")])
        while time.monotonic() < deadline:
            source_ports = []
            for candidate_name in candidate_source_names:
                source_ports = self._list_pw_ports(candidate_name)
                if source_ports:
                    break
            record_ports = self._list_pw_ports(record_node_name)
            source_outputs = [port for port in source_ports if ":capture_" in port or ":output_" in port or ":monitor_" in port]
            record_inputs = [port for port in record_ports if ":input_" in port]
            if source_outputs and record_inputs:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError(f"Unable to discover PipeWire ports for {source_node_name} -> {record_node_name}")

        source_outputs = [port for port in source_ports if ":capture_" in port or ":output_" in port or ":monitor_" in port]
        record_inputs = [port for port in record_ports if ":input_" in port]
        if not source_outputs or not record_inputs:
            raise RuntimeError(f"PipeWire ports not ready for {source_node_name} -> {record_node_name}")

        if capture_channels >= 2:
            source_left = self._pick_port(source_outputs, [":capture_FL", ":output_FL", ":monitor_FL", ":capture_MONO", ":output_MONO"])
            source_right = self._pick_port(source_outputs, [":capture_FR", ":output_FR", ":monitor_FR", ":capture_MONO", ":output_MONO", ":capture_FL", ":output_FL", ":monitor_FL"])
            input_left = self._pick_port(record_inputs, [":input_FL", ":input_MONO"])
            input_right = self._pick_port(record_inputs, [":input_FR", ":input_MONO", ":input_FL"])
            pairs = [(source_left, input_left), (source_right, input_right)]
        else:
            preferred_source = self._pick_port(
                source_outputs,
                [":capture_FR", ":output_FR", ":monitor_FR", ":capture_FL", ":output_FL", ":monitor_FL", ":capture_MONO", ":output_MONO"]
                if requested_channel == "right"
                else [":capture_FL", ":output_FL", ":monitor_FL", ":capture_MONO", ":output_MONO", ":capture_FR", ":output_FR", ":monitor_FR"],
            )
            preferred_input = self._pick_port(record_inputs, [":input_FL", ":input_MONO", ":input_FR"])
            pairs = [(preferred_source, preferred_input)]

        if any(not src or not dst for src, dst in pairs):
            raise RuntimeError(f"Could not resolve PipeWire ports for selected source {source_node_name}")

        for source_port, input_port in pairs:
            subprocess.run(["pw-link", source_port, input_port], capture_output=True, text=True, timeout=3, check=True)

        time.sleep(0.15)

    def _list_source_output_ports(self, source_node_name: str) -> list[str]:
        candidate_source_names = [source_node_name]
        if source_node_name.endswith(".monitor"):
            candidate_source_names.append(source_node_name[: -len(".monitor")])
        for candidate_name in candidate_source_names:
            source_ports = self._list_pw_ports(candidate_name)
            source_outputs = [port for port in source_ports if ":capture_" in port or ":output_" in port or ":monitor_" in port]
            if source_outputs:
                return source_outputs
        return []

    def _list_pw_ports(self, node_name: str) -> list[str]:
        try:
            completed = subprocess.run(["pw-link", "-io"], capture_output=True, text=True, timeout=3)
        except Exception:
            return []
        if completed.returncode != 0:
            return []
        prefix = f"{node_name}:"
        return [line.strip() for line in (completed.stdout or "").splitlines() if line.strip().startswith(prefix)]

    @staticmethod
    def _pw_record_supports_option(option: str) -> bool:
        try:
            completed = subprocess.run(["pw-record", "--help"], capture_output=True, text=True, timeout=3)
        except Exception:
            return False
        help_text = f"{completed.stdout or ''}\n{completed.stderr or ''}"
        return option in help_text

    @staticmethod
    def _pick_port(ports: list[str], preferred_suffixes: list[str]) -> str | None:
        for suffix in preferred_suffixes:
            for port in ports:
                if port.endswith(suffix):
                    return port
        return ports[0] if ports else None

    def _normalize_measurement(self, payload: dict[str, Any], source_path: Path | None = None) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("Measurement payload must be an object")

        now = self._utc_now()
        measurement_id = self._slugify(payload.get("id") or payload.get("name") or f"measurement-{uuid4().hex[:8]}")
        traces = self._normalize_traces(payload.get("traces") or [])
        review_traces = self._normalize_traces(payload.get("review_traces") or [])
        if not traces:
            raise ValueError("Measurement must include at least one trace with points")

        name = str(payload.get("name") or measurement_id).strip() or measurement_id
        created_at = str(payload.get("created_at") or now).strip() or now
        input_device = payload.get("input_device") if isinstance(payload.get("input_device"), dict) else {}
        calibration = payload.get("calibration") if isinstance(payload.get("calibration"), dict) else {}
        display = deepcopy(DISPLAY_DEFAULTS)
        if isinstance(payload.get("display"), dict):
            display.update(payload["display"])

        result = {
            "id": measurement_id,
            "name": name,
            "created_at": created_at,
            "input_device": {
                "id": str(input_device.get("id") or "capture-input"),
                "label": str(input_device.get("label") or "Capture input"),
            },
            "channel": str(payload.get("channel") or "left").lower(),
            "calibration": {
                "filename": str(calibration.get("filename") or ""),
                "applied": bool(calibration.get("applied")),
            },
            "display": display,
            "traces": traces,
            "summary": self._build_summary(traces),
        }
        if review_traces:
            result["review_traces"] = review_traces
            result["review_summary"] = self._build_summary(review_traces)
        if payload.get("measurement_kind"):
            result["measurement_kind"] = str(payload.get("measurement_kind"))
        if payload.get("notes"):
            result["notes"] = [str(item) for item in payload.get("notes") if str(item).strip()]
        if payload.get("analysis") and isinstance(payload.get("analysis"), dict):
            result["analysis"] = payload["analysis"]
        if source_path is not None:
            result["storage_path"] = str(source_path)
        return result

    def _normalize_traces(self, traces: list[Any]) -> list[dict[str, Any]]:
        normalized = []
        for index, trace in enumerate(traces):
            if not isinstance(trace, dict):
                continue
            points = []
            for point in trace.get("points") or []:
                if not isinstance(point, (list, tuple)) or len(point) != 2:
                    continue
                frequency = float(point[0])
                level = float(point[1])
                if not math.isfinite(frequency) or not math.isfinite(level) or frequency <= 0:
                    continue
                points.append([round(frequency, 3), round(level, 3)])
            if not points:
                continue
            points.sort(key=lambda pair: pair[0])
            item = {
                "kind": str(trace.get("kind") or "measured"),
                "label": str(trace.get("label") or f"Trace {index + 1}"),
                "color": str(trace.get("color") or TRACE_COLORS[index % len(TRACE_COLORS)]),
                "points": points,
            }
            if trace.get("role"):
                item["role"] = str(trace.get("role"))
            normalized.append(item)
        return normalized

    def _build_summary(self, traces: list[dict[str, Any]]) -> dict[str, Any]:
        frequencies = []
        levels = []
        for trace in traces:
            for frequency, level in trace.get("points") or []:
                frequencies.append(frequency)
                levels.append(level)
        return {
            "trace_count": len(traces),
            "point_count": len(levels),
            "min_db": round(min(levels), 2) if levels else None,
            "max_db": round(max(levels), 2) if levels else None,
            "min_hz": round(min(frequencies), 2) if frequencies else None,
            "max_hz": round(max(frequencies), 2) if frequencies else None,
        }

    def _persist_job(self, job: dict[str, Any]) -> None:
        path = self.job_records_dir / f"{job['id']}.json"
        path.write_text(json.dumps(job, indent=2) + "\n", encoding="utf-8")

    def _log_spaced_frequencies(self, start_hz: float, end_hz: float, count: int) -> list[float]:
        if count <= 1:
            return [start_hz]
        start_log = math.log10(start_hz)
        end_log = math.log10(end_hz)
        step = (end_log - start_log) / (count - 1)
        return [round(10 ** (start_log + step * index), 3) for index in range(count)]

    def _next_pow2(self, value: int) -> int:
        return 1 << max(1, int(value - 1)).bit_length()

    def _slugify(self, value: Any) -> str:
        raw = str(value or "measurement").strip().lower()
        chars = []
        for char in raw:
            if char.isalnum():
                chars.append(char)
            elif char in {"-", "_"}:
                chars.append("-")
            else:
                chars.append("-")
        slug = "".join(chars).strip("-")
        while "--" in slug:
            slug = slug.replace("--", "-")
        return slug or f"measurement-{uuid4().hex[:8]}"

    def _safe_filename(self, value: str) -> str:
        name = Path(value).name.strip() or "calibration.txt"
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
        return cleaned or "calibration.txt"

    def _utc_now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
