"""Separate measurement capture, analysis, and persistence for FXRoute."""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import math
import os
import re
import subprocess
import threading
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

logger = logging.getLogger(__name__)


def _env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))

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

SWEEP_V2_SECONDS = 11.0
SWEEP_V2_LEAD_IN_SECONDS = 0.5
SWEEP_V2_TAIL_SECONDS = 1.25
LR_REPEAT_SWEEP_SECONDS = 6.0
LR_REPEAT_LEAD_IN_SECONDS = 0.2
LR_REPEAT_TAIL_SECONDS = 0.75
LR_REPEAT_RECORD_PREROLL_SECONDS = 0.3
LR_REPEAT_RECORD_POSTROLL_SECONDS = 0.35
LR_REPEAT_ELECTRICAL_TIMING_CLUSTER_MS = 0.35
LR_REPEAT_ACOUSTIC_TIMING_CLUSTER_MS = 0.75
SWEEP_START_HZ = 10.0
SWEEP_END_HZ = 22_000.0
HOST_SWEEP_PEAK_SCALE = 0.8
TRUSTED_MIN_HZ = 20.0
TRUSTED_MAX_HZ = 20_000.0
DISPLAY_POINT_COUNT = 192
EDGE_STABILITY_WINDOW_POINTS = 4
EDGE_STABILITY_MAX_DELTA_DB = 6.0
EDGE_STABILITY_MAX_SPAN_DB = 9.0
MIN_TRUSTED_POINTS = 24
RESPONSE_OUTLIER_NEIGHBOR_RADIUS = 2
RESPONSE_OUTLIER_WARN_DB = 8.0
RESPONSE_OUTLIER_FAIL_DB = 12.0
RESPONSE_OUTLIER_MIN_HZ = 250.0
SWEEP_TIMING_ANCHOR_SECONDS = 0.35
SWEEP_TIMING_MULTI_ANCHOR_SECONDS = 0.18
SWEEP_TIMING_EDGE_INSET_SECONDS = 0.08
SWEEP_TIMING_SEARCH_SECONDS = 0.35
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
IR_WINDOW_PRE_SECONDS = 0.004
IR_WINDOW_POST_SECONDS = 0.35
IR_WINDOW_POST_LOW_SECONDS = 0.50
IR_WINDOW_POST_HIGH_SECONDS = 0.18
IR_WINDOW_FADE_SECONDS = 0.012
IR_WINDOW_VARIABLE_LOW_HZ = 250.0
IR_WINDOW_VARIABLE_HIGH_HZ = 1_200.0
IR_DIRECT_SEARCH_PRE_SECONDS = 0.12
IR_DIRECT_RELATIVE_THRESHOLD = 0.05
IR_DIRECT_CANDIDATE_FLOOR_RELATIVE = 0.02
IR_DIRECT_CANDIDATE_LIMIT = 24
IR_DIRECT_WEAK_EARLY_RELATIVE = 0.075
IR_DIRECT_WEAK_EARLY_MIN_GAP_SAMPLES = 20
IR_DIRECT_WEAK_EARLY_NEXT_RATIO = 1.75
IR_DIRECT_SUPPORT_WINDOW_SECONDS = 0.00035
IR_DIRECT_NEARBY_WINDOW_SECONDS = 0.0009
IR_DIRECT_THRESHOLD_EDGE_SAMPLES = 12
IR_DIRECT_PROMOTION_WINDOW_SECONDS = 0.004
IR_DIRECT_PROMOTION_SUPPORT_RATIO = 1.6
IR_DIRECT_PROMOTION_SCORE_RATIO = 1.25
IR_DIRECT_PROMOTION_ENERGY_RATIO = 1.45
IR_DIRECT_PROMINENCE_REFERENCE = 0.15
IR_DEBUG_SEGMENT_ENABLED = _env_flag("FXROUTE_MEASUREMENT_IR_DEBUG_SEGMENT", True)
IR_DEBUG_SEGMENT_RADIUS_SAMPLES = _env_int(
    "FXROUTE_MEASUREMENT_IR_DEBUG_SEGMENT_RADIUS_SAMPLES",
    250,
    minimum=64,
    maximum=5000,
)
HOST_SWEEP_RECORD_PREROLL_SECONDS = 0.75
HOST_SWEEP_RECORD_POSTROLL_SECONDS = 0.75
HOST_SWEEP_MAX_ATTEMPTS = 3
HOST_SWEEP_RETRY_DELAY_SECONDS = 0.4
HOST_SWEEP_AUTO_GAIN_RETRY_ATTEMPT = 1
HOST_SWEEP_AUTO_GAIN_TARGET_PERCENT = 100
ALIGNMENT_SCORE_FAIL_THRESHOLD = 0.90
ALIGNMENT_SCORE_WARN_THRESHOLD = 0.94
HOST_ALIGNMENT_SCORE_FAIL_THRESHOLD = 0.84
HOST_ALIGNMENT_SCORE_WARN_THRESHOLD = 0.90
CAPTURE_CLIP_FAIL_DBFS = -0.2
CAPTURE_CLIP_WARN_DBFS = -1.0
ELECTRICAL_REFERENCE_MIN_PEAK_DBFS = -70.0
ELECTRICAL_REFERENCE_MIN_ALIGNMENT_SCORE = 0.84
ELECTRICAL_REFERENCE_MIN_IR_SHARPNESS_DB = 18.0
CAPTURE_LEVEL_STATUS_INTERVAL_SECONDS = 0.35
CAPTURE_LEVEL_STATUS_MIN_DBFS = -90.0
CLOCK_DRIFT_WARN_PPM = 3_000.0
CHANNEL_CORRELATION_WARN_THRESHOLD = 0.985

MEASUREMENT_SCOPE_NOTE = (
    "FXRoute measures with a host-local sweep through the active PipeWire output and selected microphone input. "
    "The result is a practical response trace for comparison and PEQ drafting, independent of the active EasyEffects preset."
)


def _detailed_measurement_diagnostics_enabled() -> bool:
    return logger.isEnabledFor(logging.DEBUG)


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
        self.diagnostics_dir = self.jobs_dir / "diagnostics"
        for directory in [
            self.measurements_dir,
            self.jobs_dir,
            self.captures_dir,
            self.calibrations_dir,
            self.job_records_dir,
            self.playbacks_dir,
            self.diagnostics_dir,
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
                    "note": "FXRoute plays and records on the host via PipeWire.",
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
        mic_input_channel: str | int | None = "1",
        reference_input_channel: str | int | None = "",
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
        input_channel_count = max(1, int(selected_input.get("channels") or 1))
        mic_input_channel_index = self._parse_input_channel_index(
            mic_input_channel,
            channel_count=input_channel_count,
            default=0,
            field_name="mic_input_channel",
        )
        reference_input_channel_index = self._parse_optional_input_channel_index(
            reference_input_channel,
            channel_count=input_channel_count,
            field_name="reference_input_channel",
        )
        reference_disabled_reason = ""
        if reference_input_channel_index is not None and reference_input_channel_index == mic_input_channel_index:
            reference_disabled_reason = "Mic input and electrical reference input are the same channel; reference compensation disabled."
            reference_input_channel_index = None

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
            "input_channels": {
                "mic": mic_input_channel_index + 1,
                "electrical_reference": reference_input_channel_index + 1 if reference_input_channel_index is not None else None,
                "reference_disabled_reason": reference_disabled_reason,
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

    async def start_lr_repeat_measurement(
        self,
        *,
        input_id: str,
        base_name: str = "",
        mic_input_channel: str | int | None = "1",
        reference_input_channel: str | int | None = "",
        calibration_filename: str | None = None,
        calibration_bytes: bytes | None = None,
        calibration_ref: str | None = None,
    ) -> dict[str, Any]:
        normalized_repeat_count = 3
        inputs = self._discover_capture_inputs()
        selected_input = next((item for item in inputs if item["id"] == input_id), None)
        if not selected_input:
            raise ValueError("Selected capture input is no longer available")
        if not selected_input.get("available"):
            raise ValueError("Selected capture input is not available")
        input_channel_count = max(1, int(selected_input.get("channels") or 1))
        mic_input_channel_index = self._parse_input_channel_index(
            mic_input_channel,
            channel_count=input_channel_count,
            default=0,
            field_name="mic_input_channel",
        )
        reference_input_channel_index = self._parse_optional_input_channel_index(
            reference_input_channel,
            channel_count=input_channel_count,
            field_name="reference_input_channel",
        )
        reference_disabled_reason = ""
        if reference_input_channel_index is not None and reference_input_channel_index == mic_input_channel_index:
            reference_disabled_reason = "Mic input and electrical reference input are the same channel; reference compensation disabled."
            reference_input_channel_index = None
        calibration_meta = self._resolve_calibration_meta(
            calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes,
            calibration_ref=calibration_ref,
        )
        job_id = f"measurement-repeat-job-{uuid4().hex[:12]}"
        now = self._utc_now()
        job = {
            "id": job_id,
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "job_kind": "lr-repeat",
            "repeat_count": normalized_repeat_count,
            "base_name": str(base_name or "").strip() or f"L/R Repeat {now[:19].replace('T', ' ')}",
            "input": {
                "id": selected_input["id"],
                "label": selected_input["label"],
                "node_serial": selected_input.get("node_serial"),
                "node_name": selected_input.get("node_name"),
                "channels": selected_input.get("channels"),
                "sample_rate": selected_input.get("sample_rate"),
            },
            "input_channels": {
                "mic": mic_input_channel_index + 1,
                "electrical_reference": reference_input_channel_index + 1 if reference_input_channel_index is not None else None,
                "reference_disabled_reason": reference_disabled_reason,
            },
            "channel": "stereo",
            "calibration": calibration_meta or {"filename": "", "applied": False},
            "message": "L/R repeat queued.",
            "scope_note": MEASUREMENT_SCOPE_NOTE,
            "result": None,
            "error": None,
        }
        self._jobs[job_id] = job
        self._persist_job(job)
        task = asyncio.create_task(self._run_measurement_job(job_id))
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

    def save_measurements(self, payloads: list[Any]) -> list[dict[str, Any]]:
        if not isinstance(payloads, list) or not payloads:
            raise ValueError("Measurements must include at least one measurement")
        normalized = [self._normalize_measurement(payload) for payload in payloads]
        measurement_ids = [measurement["id"] for measurement in normalized]
        if len(set(measurement_ids)) != len(measurement_ids):
            raise ValueError("Measurements must use distinct ids")
        paths = [self.measurements_dir / f"{measurement_id}.json" for measurement_id in measurement_ids]
        existing = next((path for path in paths if path.exists()), None)
        if existing is not None:
            raise ValueError(f"Measurement already exists: {existing.stem}")
        written = []
        try:
            for path, measurement in zip(paths, normalized):
                path.write_text(json.dumps(measurement, indent=2) + "\n", encoding="utf-8")
                written.append(path)
        except Exception:
            for path in written:
                path.unlink(missing_ok=True)
            raise
        return normalized

    def merge_measurements(self, measurement_ids: list[Any], name: str = "") -> dict[str, Any]:
        normalized_ids = [str(measurement_id or "").strip() for measurement_id in measurement_ids]
        if len(normalized_ids) < 2:
            raise ValueError("Select at least two saved measurements to merge")
        if any(not measurement_id or Path(measurement_id).name != measurement_id for measurement_id in normalized_ids):
            raise ValueError("Invalid measurement id")
        if len(set(normalized_ids)) != len(normalized_ids):
            raise ValueError("Select distinct saved measurements to merge")

        measurements = []
        for measurement_id in normalized_ids:
            path = self.measurements_dir / f"{measurement_id}.json"
            if not path.exists():
                raise KeyError(measurement_id)
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                measurements.append(self._normalize_measurement(payload, source_path=path))
            except ValueError:
                raise
            except Exception as exc:
                raise ValueError(f"Saved measurement could not be loaded: {measurement_id}") from exc

        merged_name = str(name or "").strip() or f"Merged {len(measurements)} measurements"
        trusted_traces = [
            self._select_merge_trace(measurement, "traces", preferred_role="trusted")
            for measurement in measurements
        ]
        review_traces = [
            self._select_merge_trace(measurement, "review_traces", preferred_role="raw-review", required=False)
            for measurement in measurements
        ]
        timestamp = datetime.now(timezone.utc).replace(microsecond=0)
        merged_id = f"merged-{timestamp.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"
        channels = {str(measurement.get("channel") or "").lower() for measurement in measurements}
        calibrations = [measurement.get("calibration") or {} for measurement in measurements]
        input_devices = [measurement.get("input_device") or {} for measurement in measurements]
        input_channels = [measurement.get("input_channels") or {} for measurement in measurements]
        payload = {
            "id": merged_id,
            "name": merged_name,
            "created_at": timestamp.isoformat().replace("+00:00", "Z"),
            "input_device": input_devices[0] if all(item == input_devices[0] for item in input_devices) else {
                "id": "merged-inputs",
                "label": "Merged capture inputs",
            },
            "input_channels": input_channels[0] if all(item == input_channels[0] for item in input_channels) else {},
            "channel": next(iter(channels)) if len(channels) == 1 else "stereo",
            "calibration": calibrations[0] if all(item == calibrations[0] for item in calibrations) else {
                "filename": "",
                "applied": False,
            },
            "display": deepcopy(measurements[0].get("display") or DISPLAY_DEFAULTS),
            "traces": [
                self._average_merge_traces(
                    trusted_traces,
                    label=f"{merged_name} · trusted average",
                    kind="merged-sweep-response",
                    role="trusted",
                    color=TRACE_COLORS[0],
                )
            ],
            "measurement_kind": "merged-measurement",
            "notes": [
                f"Averaged from {len(measurements)} saved measurements.",
                "Direct-arrival timing is intentionally not retained for merged measurements.",
            ],
            "analysis": {
                "method": "saved-measurement-average",
                "source_measurement_ids": normalized_ids,
                "source_count": len(measurements),
                "direct_arrival_timing_available": False,
            },
        }
        if all(review_traces):
            payload["review_traces"] = [
                self._average_merge_traces(
                    review_traces,
                    label=f"{merged_name} · raw/full-band review average",
                    kind="merged-sweep-response-review",
                    role="raw-review",
                    color=TRACE_COLORS[1],
                )
            ]
        elif any(review_traces):
            payload["notes"].append("Raw/full-band review traces were omitted because they were not available for every source measurement.")
        return self.save_measurement(payload)

    def _select_merge_trace(
        self,
        measurement: dict[str, Any],
        trace_key: str,
        *,
        preferred_role: str,
        required: bool = True,
    ) -> dict[str, Any] | None:
        traces = measurement.get(trace_key) or []
        preferred = [trace for trace in traces if str(trace.get("role") or "") == preferred_role]
        if len(preferred) == 1:
            return preferred[0]
        if len(preferred) > 1:
            raise ValueError(f"Saved measurement has multiple {preferred_role} traces: {measurement['id']}")
        if len(traces) == 1:
            return traces[0]
        if not traces and not required:
            return None
        label = "trusted" if trace_key == "traces" else "review"
        raise ValueError(f"Saved measurement does not have one unambiguous {label} trace: {measurement['id']}")

    def _average_merge_traces(
        self,
        traces: list[dict[str, Any]],
        *,
        label: str,
        kind: str,
        role: str,
        color: str,
    ) -> dict[str, Any]:
        point_sets = [trace.get("points") or [] for trace in traces]
        overlap_min_hz = max(points[0][0] for points in point_sets)
        overlap_max_hz = min(points[-1][0] for points in point_sets)
        frequencies = sorted({
            float(frequency)
            for points in point_sets
            for frequency, _level in points
            if overlap_min_hz <= frequency <= overlap_max_hz
        })
        if len(frequencies) < 2:
            raise ValueError("Selected measurements do not share a usable frequency range")

        merged_points = []
        for frequency in frequencies:
            levels = [
                float(np.interp(
                    frequency,
                    [point[0] for point in points],
                    [point[1] for point in points],
                ))
                for points in point_sets
            ]
            merged_points.append([round(frequency, 3), round(sum(levels) / len(levels), 3)])
        return {
            "kind": kind,
            "label": label,
            "color": color,
            "role": role,
            "points": merged_points,
        }

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
        job["message"] = "Running L/R repeat…" if job.get("job_kind") == "lr-repeat" else "Running sweep…"
        self._persist_job(job)
        try:
            executor = self._execute_lr_repeat_job if job.get("job_kind") == "lr-repeat" else self._execute_capture_job
            result = await asyncio.to_thread(executor, deepcopy(job))
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

    def _execute_lr_repeat_job(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = str(job["id"])
        repeat_count = int(job.get("repeat_count") or 1)
        captures: dict[str, list[dict[str, Any]]] = {"left": [], "right": []}
        total_sweeps = repeat_count * 2
        sweep_number = 0
        for repeat_index in range(repeat_count):
            for channel in ("left", "right"):
                if job_id in self._cancelled_jobs:
                    raise RuntimeError("Measurement cancelled.")
                sweep_number += 1
                live_job = self._jobs.get(job_id)
                if live_job is not None:
                    live_job["message"] = f"L/R repeat {sweep_number}/{total_sweeps}: {channel.upper()}{repeat_index + 1}…"
                    live_job["updated_at"] = self._utc_now()
                    self._persist_job(live_job)
                capture_job = deepcopy(job)
                capture_job["id"] = job_id
                capture_job["channel"] = channel
                capture_job["capture_profile"] = "lr-repeat"
                captures[channel].append(self._execute_capture_job(capture_job)["measurement"])

        summaries = []
        for channel in ("left", "right"):
            summary = self.summarize_repeat_measurements(
                captures[channel],
                base_name=str(job.get("base_name") or "L/R Repeat"),
                channel=channel,
                repeat_count=repeat_count,
            )
            summaries.append(summary)
        return {
            "measurements": summaries,
            "base_name": str(job.get("base_name") or "L/R Repeat"),
            "message": "L/R repeat finished. Review the combined L and R results, then save them together.",
            "scope_note": MEASUREMENT_SCOPE_NOTE,
        }

    def summarize_repeat_measurements(
        self,
        measurements: list[dict[str, Any]],
        *,
        base_name: str,
        channel: str,
        repeat_count: int,
    ) -> dict[str, Any]:
        if not measurements:
            raise ValueError("L/R repeat summary needs at least one measurement")
        normalized = [self._normalize_measurement(item) for item in measurements]
        timings = []
        for index, measurement in enumerate(normalized):
            analysis = measurement.get("analysis") if isinstance(measurement.get("analysis"), dict) else {}
            reference_path = analysis.get("reference_path") if isinstance(analysis.get("reference_path"), dict) else {}
            impulse = analysis.get("impulse_response") if isinstance(analysis.get("impulse_response"), dict) else {}
            timing_ms = reference_path.get("acoustic_arrival_corrected_ms", impulse.get("arrival_ms"))
            try:
                timing_ms = float(timing_ms)
            except (TypeError, ValueError):
                continue
            if math.isfinite(timing_ms):
                timings.append({
                    "index": index,
                    "timing_ms": timing_ms,
                    "electrical_reference_used": bool(reference_path.get("electrical_reference_used")),
                })
        electrical_reference_used = bool(timings) and all(item["electrical_reference_used"] for item in timings)
        cluster_limit_ms = (
            LR_REPEAT_ELECTRICAL_TIMING_CLUSTER_MS
            if electrical_reference_used
            else LR_REPEAT_ACOUSTIC_TIMING_CLUSTER_MS
        )
        accepted_indices, timing_center_ms, timing_spread_ms = self._select_repeat_timing_cluster(
            timings,
            repeat_count=repeat_count,
            cluster_limit_ms=cluster_limit_ms,
        )
        stable = bool(accepted_indices)
        magnitude_indices = accepted_indices or list(range(len(normalized)))
        accepted_measurements = [normalized[index] for index in magnitude_indices]
        side_label = "L" if channel == "left" else "R"
        summary_name = f"{str(base_name or 'L/R Repeat').strip()} · {side_label}"
        timestamp = datetime.now(timezone.utc).replace(microsecond=0)
        measurement_id = f"lr-repeat-{channel}-{timestamp.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"
        payload = deepcopy(normalized[0])
        payload.update({
            "id": measurement_id,
            "name": summary_name,
            "created_at": timestamp.isoformat().replace("+00:00", "Z"),
            "channel": channel,
            "measurement_kind": "lr-repeat-summary",
            "traces": [
                self._average_merge_traces(
                    [self._select_merge_trace(item, "traces", preferred_role="trusted") for item in accepted_measurements],
                    label=f"{summary_name} · trusted average",
                    kind="lr-repeat-sweep-response",
                    role="trusted",
                    color=TRACE_COLORS[0],
                )
            ],
            "notes": [
                MEASUREMENT_SCOPE_NOTE,
                f"Same-position L/R repeat summary from {len(normalized)} {side_label} sweep(s).",
                "Intermediate repeat sweeps were processed internally and were not saved as normal measurements.",
            ],
        })
        review_traces = [
            self._select_merge_trace(item, "review_traces", preferred_role="raw-review", required=False)
            for item in accepted_measurements
        ]
        if all(review_traces):
            payload["review_traces"] = [
                self._average_merge_traces(
                    review_traces,
                    label=f"{summary_name} · raw/full-band review average",
                    kind="lr-repeat-sweep-response-review",
                    role="raw-review",
                    color=TRACE_COLORS[1],
                )
            ]
        else:
            payload.pop("review_traces", None)
        analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
        analysis = deepcopy(analysis)
        analysis["method"] = "same-position-lr-repeat-average"
        reference_path = analysis.get("reference_path") if isinstance(analysis.get("reference_path"), dict) else {}
        reference_path = deepcopy(reference_path)
        reference_source = ""
        if electrical_reference_used:
            reference_channel = reference_path.get("electrical_reference_input_channel")
            reference_source = f"electrical-input-channel-{reference_channel}" if reference_channel else "electrical-input"
        elif reference_path.get("capture_mode"):
            reference_source = str(reference_path["capture_mode"])
        analysis["lr_repeat"] = {
            "repeat_count": int(repeat_count),
            "accepted_runs": len(accepted_indices),
            "rejected_runs": len(normalized) - len(accepted_indices),
            "accepted_run_numbers": [index + 1 for index in accepted_indices],
            "rejected_run_numbers": [index + 1 for index in range(len(normalized)) if index not in accepted_indices],
            "timing_spread_ms": timing_spread_ms,
            "timing_method": "electrical-reference-cluster-median" if electrical_reference_used else "acoustic-cluster-median",
            "electrical_reference_used": electrical_reference_used,
            "reference_source": reference_source,
            "timing_stable": stable,
        }
        impulse = analysis.get("impulse_response") if isinstance(analysis.get("impulse_response"), dict) else {}
        impulse = deepcopy(impulse)
        sample_rate = int(analysis.get("sample_rate") or 0)
        if stable and timing_center_ms is not None:
            arrival_samples = int(round(timing_center_ms / 1000.0 * sample_rate)) if sample_rate > 0 else None
            reference_path.update({
                "timing_status": "lr-repeat",
                "timing_label": "L/R repeat timing",
                "stability": "stable",
                "acoustic_arrival_corrected_ms": timing_center_ms,
                "acoustic_arrival_corrected_seconds": round(timing_center_ms / 1000.0, 9),
                "acoustic_arrival_corrected_samples": arrival_samples,
            })
            impulse.update({
                "arrival_ms": timing_center_ms,
                "arrival_seconds": round(timing_center_ms / 1000.0, 9),
                "arrival_samples": arrival_samples,
            })
        else:
            reference_path.update({
                "timing_status": "lr-repeat-unstable",
                "timing_label": "L/R repeat timing unstable",
                "stability": "unstable",
            })
            for key in ("acoustic_arrival_corrected_ms", "acoustic_arrival_corrected_seconds", "acoustic_arrival_corrected_samples"):
                reference_path.pop(key, None)
            for key in ("arrival_ms", "arrival_seconds", "arrival_samples", "direct_arrival_index"):
                impulse.pop(key, None)
            analysis["direct_arrival_timing_available"] = False
            payload["notes"].append("No stable timing cluster was found; timing-sensitive L/R alignment must not use this summary.")
        analysis["reference_path"] = reference_path
        analysis["impulse_response"] = impulse
        payload["analysis"] = analysis
        return self._normalize_measurement(payload)

    @staticmethod
    def _select_repeat_timing_cluster(
        timings: list[dict[str, Any]],
        *,
        repeat_count: int,
        cluster_limit_ms: float,
    ) -> tuple[list[int], float | None, float | None]:
        if not timings:
            return [], None, None
        minimum_cluster_size = 1 if repeat_count <= 1 else 2
        candidates = []
        for anchor in timings:
            cluster = [
                item for item in timings
                if abs(float(item["timing_ms"]) - float(anchor["timing_ms"])) <= cluster_limit_ms
            ]
            spread = max(item["timing_ms"] for item in cluster) - min(item["timing_ms"] for item in cluster)
            candidates.append((len(cluster), -spread, cluster))
        _size, _negative_spread, best = max(candidates, key=lambda item: (item[0], item[1]))
        if len(best) < minimum_cluster_size:
            return [], None, None
        values = sorted(float(item["timing_ms"]) for item in best)
        center = float(np.median(values))
        spread = max(values) - min(values)
        return sorted(int(item["index"]) for item in best), round(center, 6), round(spread, 6)

    def _execute_capture_job(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = str(job["id"])
        selected_input = job.get("input") or {}
        input_channels = job.get("input_channels") if isinstance(job.get("input_channels"), dict) else {}
        channel = str(job.get("channel") or "left")
        calibration_meta = job.get("calibration") if isinstance(job.get("calibration"), dict) else {"filename": "", "applied": False}

        sample_rate = self._resolve_measurement_sample_rate()
        repeat_profile = job.get("capture_profile") == "lr-repeat"
        sweep_seconds = LR_REPEAT_SWEEP_SECONDS if repeat_profile else SWEEP_V2_SECONDS
        lead_in_seconds = LR_REPEAT_LEAD_IN_SECONDS if repeat_profile else SWEEP_V2_LEAD_IN_SECONDS
        tail_seconds = LR_REPEAT_TAIL_SECONDS if repeat_profile else SWEEP_V2_TAIL_SECONDS
        duration_seconds = lead_in_seconds + sweep_seconds + tail_seconds
        record_preroll_seconds = LR_REPEAT_RECORD_PREROLL_SECONDS if repeat_profile else HOST_SWEEP_RECORD_PREROLL_SECONDS
        record_postroll_seconds = LR_REPEAT_RECORD_POSTROLL_SECONDS if repeat_profile else HOST_SWEEP_RECORD_POSTROLL_SECONDS
        record_duration_seconds = duration_seconds + record_preroll_seconds + record_postroll_seconds
        mic_input_channel_index = max(0, int(input_channels.get("mic") or 1) - 1)
        electrical_reference_input_channel = input_channels.get("electrical_reference")
        electrical_reference_channel_index = (
            max(0, int(electrical_reference_input_channel) - 1)
            if electrical_reference_input_channel is not None
            else None
        )
        use_electrical_reference = electrical_reference_channel_index is not None and electrical_reference_channel_index != mic_input_channel_index
        capture_channels = max(2, mic_input_channel_index + 1, (electrical_reference_channel_index + 1) if use_electrical_reference else 2)
        capture_path = self.captures_dir / f"{job_id}.wav"
        playback_path = self.playbacks_dir / f"{job_id}.wav"
        source_node_name = str(selected_input.get("node_name") or "").strip()
        if not source_node_name:
            raise RuntimeError("Selected capture input has no usable PipeWire source node")
        if source_node_name == "easyeffects_source" or source_node_name.endswith(".monitor"):
            raise RuntimeError("Refusing to measure through a non-microphone source; select a real PipeWire input")

        playback_channel = channel
        playback_target = self._resolve_playback_target()
        host_reference = self._resolve_host_reference_capture(
            playback_target=playback_target,
            mic_source_node_name=source_node_name,
            requested_channel=playback_channel,
        )
        electrical_reference = None
        if use_electrical_reference:
            electrical_reference = {
                "source_node_name": source_node_name,
                "sink_node_name": "",
                "channel": f"input_{electrical_reference_channel_index + 1}",
                "channel_label": f"input_{electrical_reference_channel_index + 1}_electrical_reference",
                "mic_channel_label": f"input_{mic_input_channel_index + 1}_mic",
                "mic_input_channel": mic_input_channel_index + 1,
                "electrical_reference_input_channel": electrical_reference_channel_index + 1,
            }
        sweep_meta = self._write_sweep_file(
            playback_path,
            sample_rate=sample_rate,
            sweep_seconds=sweep_seconds,
            lead_in_seconds=lead_in_seconds,
            tail_seconds=tail_seconds,
            channel=playback_channel,
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
        reference_warning = str(input_channels.get("reference_disabled_reason") or "").strip()
        for attempt_index in range(HOST_SWEEP_MAX_ATTEMPTS):
            attempts_used = attempt_index + 1
            try:
                if capture_path.exists():
                    capture_path.unlink()
                attempt_reference = electrical_reference if use_electrical_reference else host_reference
                analysis, capture_info, playback_info = self._run_host_capture_attempt(
                    job_id=job_id,
                    mic_source_node_name=source_node_name,
                    reference_capture=attempt_reference,
                    channel=playback_channel,
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
                    mic_input_channel_index=mic_input_channel_index,
                    electrical_reference_channel_index=electrical_reference_channel_index if use_electrical_reference else None,
                )
                if use_electrical_reference:
                    reference_status = self._evaluate_electrical_reference_status(analysis)
                    if not reference_status["usable"]:
                        reference_warning = reference_status["warning"]
                        logger.warning("Electrical measurement reference rejected for %s: %s", job_id, reference_warning)
                        if capture_path.exists():
                            capture_path.unlink()
                        analysis, capture_info, playback_info = self._run_host_capture_attempt(
                            job_id=job_id,
                            mic_source_node_name=source_node_name,
                            reference_capture=host_reference,
                            channel=playback_channel,
                            capture_channels=2,
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
                            mic_input_channel_index=mic_input_channel_index,
                            electrical_reference_channel_index=None,
                        )
                        self._append_reference_fallback_warning(analysis, reference_warning)
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
                if use_electrical_reference:
                    reference_warning = f"Electrical reference unavailable; used host monitor timing fallback ({exc})."
                    logger.warning("Electrical measurement reference failed for %s; falling back to host monitor timing: %s", job_id, exc)
                    try:
                        if capture_path.exists():
                            capture_path.unlink()
                        analysis, capture_info, playback_info = self._run_host_capture_attempt(
                            job_id=job_id,
                            mic_source_node_name=source_node_name,
                            reference_capture=host_reference,
                            channel=playback_channel,
                            capture_channels=2,
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
                            mic_input_channel_index=mic_input_channel_index,
                            electrical_reference_channel_index=None,
                        )
                        self._append_reference_fallback_warning(analysis, reference_warning)
                        final_capture_level_low = self._analysis_has_warning_code(analysis, "capture-level-low")
                        break
                    except Exception:
                        logger.warning("Electrical reference fallback capture also failed for %s", job_id, exc_info=True)
                if attempt_index >= HOST_SWEEP_MAX_ATTEMPTS - 1 or not self._should_retry_host_capture(exc):
                    raise
                time.sleep(HOST_SWEEP_RETRY_DELAY_SECONDS)
        if analysis is None or capture_info is None or playback_info is None:
            raise RuntimeError("Host-local capture did not produce an analysis result")
        if reference_warning and not self._analysis_has_warning_code(analysis, "electrical-reference-fallback"):
            self._append_reference_fallback_warning(analysis, reference_warning)

        measurement = self._build_measurement_from_analysis(
            analysis,
            input_device={
                "id": str(selected_input.get("id") or "capture-input"),
                "label": str(selected_input.get("label") or "Capture input"),
            },
            channel=channel,
            calibration=calibration_result,
            input_channels={
                "mic": mic_input_channel_index + 1,
                "electrical_reference": electrical_reference_channel_index + 1 if use_electrical_reference else None,
                "reference_disabled_reason": str(input_channels.get("reference_disabled_reason") or ""),
            },
        )
        if mic_auto_boosted and isinstance(capture_info, dict):
            capture_info["mic_auto_boosted"] = True
            capture_info["mic_auto_boost_target_percent"] = HOST_SWEEP_AUTO_GAIN_TARGET_PERCENT

        timing_summary = self._format_measurement_timing_summary(analysis)
        completion_message = f"Measurement finished. {timing_summary}" if timing_summary else "Measurement finished. Trusted trace is ready."
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
                "sample_rate": analysis["sample_rate"],
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
                "variable_window": analysis.get("variable_window"),
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
        mic_input_channel_index: int,
        electrical_reference_channel_index: int | None = None,
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
        monitored_channel_index = mic_input_channel_index if electrical_reference_channel_index is not None else 1
        level_monitor_stop = threading.Event()
        level_monitor_thread = threading.Thread(
            target=self._monitor_capture_input_level,
            args=(job_id, capture_path, capture_channels, monitored_channel_index, level_monitor_stop),
            daemon=True,
        )
        level_monitor_thread.start()
        play_process: subprocess.Popen[str] | None = None
        play_stdout = ""
        play_stderr = ""
        play_timed_out = False
        record_stdout = ""
        record_stderr = ""
        detailed_diagnostics_enabled = _detailed_measurement_diagnostics_enabled()
        routing_snapshots: list[dict[str, Any]] = []
        link_diagnostics: dict[str, Any] = {}
        if detailed_diagnostics_enabled:
            routing_snapshots.append(
                self._build_measurement_routing_snapshot(
                    label="before-record-link",
                    playback_target=playback_target,
                    mic_source_node_name=mic_source_node_name,
                    reference_capture=reference_capture,
                    record_node_name=record_node_name,
                    play_node_name=play_node_name,
                )
            )
        try:
            if electrical_reference_channel_index is not None:
                link_diagnostics = self._link_capture_channels_to_record_stream(
                    source_node_name=mic_source_node_name,
                    record_node_name=record_node_name,
                    channel_indices=sorted({mic_input_channel_index, electrical_reference_channel_index}),
                )
            else:
                link_diagnostics = self._link_host_reference_capture(
                    reference_source_node_name=str(reference_capture["source_node_name"]),
                    mic_source_node_name=mic_source_node_name,
                    record_node_name=record_node_name,
                    requested_channel=channel,
                    mic_input_channel_index=mic_input_channel_index,
                )
            if detailed_diagnostics_enabled:
                routing_snapshots.append(
                    self._build_measurement_routing_snapshot(
                        label="after-record-link",
                        playback_target=playback_target,
                        mic_source_node_name=mic_source_node_name,
                        reference_capture=reference_capture,
                        record_node_name=record_node_name,
                        play_node_name=play_node_name,
                    )
                )
            time.sleep(record_preroll_seconds)

            play_process = subprocess.Popen(play_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self._job_processes[job_id] = [record_process, play_process]
            time.sleep(0.2)
            if detailed_diagnostics_enabled:
                routing_snapshots.append(
                    self._build_measurement_routing_snapshot(
                        label="during-playback",
                        playback_target=playback_target,
                        mic_source_node_name=mic_source_node_name,
                        reference_capture=reference_capture,
                        record_node_name=record_node_name,
                        play_node_name=play_node_name,
                    )
                )
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
        finally:
            level_monitor_stop.set()
            if level_monitor_thread.is_alive():
                level_monitor_thread.join(timeout=1.0)
            if detailed_diagnostics_enabled:
                routing_snapshots.append(
                    self._build_measurement_routing_snapshot(
                        label="after-capture",
                        playback_target=playback_target,
                        mic_source_node_name=mic_source_node_name,
                        reference_capture=reference_capture,
                        record_node_name=record_node_name,
                        play_node_name=play_node_name,
                    )
                )

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
        analysis_channel_index = mic_input_channel_index if electrical_reference_channel_index is not None else 1
        reference_channel_index = electrical_reference_channel_index if electrical_reference_channel_index is not None else 0
        analysis = self._analyze_sweep_capture(
            capture_path,
            expected_sample_rate=sample_rate,
            channel=channel,
            reference_sweep=sweep_meta["analysis_sweep"],
            inverse_sweep=sweep_meta["inverse_sweep"],
            calibration_curve=calibration_curve,
            capture_label="Host-local capture",
            reference_channel_index=reference_channel_index,
            analysis_channel_index=analysis_channel_index,
            reference_channel_label=reference_channel_label,
        )
        analysis["method"] = (
            "inverse log-sweep deconvolution with electrical reference input timing"
            if electrical_reference_channel_index is not None
            else "inverse log-sweep deconvolution with host-reference dual-channel capture"
        )
        analysis_clock = analysis.get("clock") if isinstance(analysis.get("clock"), dict) else {}
        analysis_clock.update(
            {
                "timing_channel": reference_channel_label,
                "reference_capture_mode": "electrical-input" if electrical_reference_channel_index is not None else "dual-channel",
                "reference_channel": reference_channel_label,
            }
        )
        analysis["clock"] = analysis_clock
        reference_path = analysis.get("reference_path") if isinstance(analysis.get("reference_path"), dict) else {}
        reference_path.update(
            {
                "timing_applied_to_mic": True,
                "capture_mode": "electrical-input" if electrical_reference_channel_index is not None else "dual-channel",
                "mic_input_channel": mic_input_channel_index + 1,
                "electrical_reference_input_channel": electrical_reference_channel_index + 1 if electrical_reference_channel_index is not None else None,
            }
        )
        if electrical_reference_channel_index is not None:
            impulse_meta = analysis.get("impulse_response") if isinstance(analysis.get("impulse_response"), dict) else {}
            reference_path.update(
                {
                    "timing_status": "electrical-reference-candidate",
                    "timing_label": "Electrical reference candidate",
                    "electrical_reference_used": False,
                    "electrical_reference_delay_samples": impulse_meta.get("reference_peak_index"),
                    "electrical_reference_delay_seconds": impulse_meta.get("reference_peak_seconds"),
                    "electrical_reference_delay_ms": round(float(impulse_meta.get("reference_peak_seconds") or 0.0) * 1000.0, 6),
                    "acoustic_arrival_delay_samples": impulse_meta.get("direct_arrival_index"),
                    "acoustic_arrival_delay_seconds": impulse_meta.get("direct_seconds"),
                    "acoustic_arrival_delay_ms": round(float(impulse_meta.get("direct_seconds") or 0.0) * 1000.0, 6),
                    "acoustic_arrival_corrected_samples": impulse_meta.get("arrival_samples"),
                    "acoustic_arrival_corrected_seconds": impulse_meta.get("arrival_seconds"),
                    "acoustic_arrival_corrected_ms": impulse_meta.get("arrival_ms"),
                    "confidence": impulse_meta.get("direct_confidence"),
                    "stability": "candidate",
                }
            )
        else:
            impulse_meta = analysis.get("impulse_response") if isinstance(analysis.get("impulse_response"), dict) else {}
            reference_path.update(
                {
                    "timing_status": "acoustic-only",
                    "timing_label": "Acoustic-only timing",
                    "electrical_reference_used": False,
                    "acoustic_arrival_delay_samples": impulse_meta.get("direct_arrival_index"),
                    "acoustic_arrival_delay_seconds": impulse_meta.get("direct_seconds"),
                    "acoustic_arrival_delay_ms": round(float(impulse_meta.get("direct_seconds") or 0.0) * 1000.0, 6),
                    "acoustic_arrival_corrected_samples": impulse_meta.get("arrival_samples"),
                    "acoustic_arrival_corrected_seconds": impulse_meta.get("arrival_seconds"),
                    "acoustic_arrival_corrected_ms": impulse_meta.get("arrival_ms"),
                    "confidence": impulse_meta.get("direct_confidence"),
                    "stability": "host-reference",
                }
            )
        analysis["reference_path"] = reference_path
        pipewire_warnings = self._extract_pipewire_warning_lines(
            {
                "pw-play.stdout": play_stdout,
                "pw-play.stderr": play_stderr,
                "pw-record.stdout": record_stdout,
                "pw-record.stderr": record_stderr,
            }
        )
        playback_node = self._lookup_pipewire_audio_node(playback_target["target_name"])
        capture_node = self._lookup_pipewire_audio_node(mic_source_node_name)
        reference_node = self._lookup_pipewire_audio_node(str(reference_capture.get("source_node_name") or ""))
        uses_monitor_source = str(reference_capture.get("source_node_name") or "").endswith(".monitor")
        routing_diagnostics = {
            "schema": "fxroute.measurement-routing-diagnostics.v1",
            "detail_enabled": detailed_diagnostics_enabled,
            "playback_sink": {
                "target_name": playback_target["target_name"],
                "target_label": playback_target["target_label"],
                "active_rate": playback_target.get("active_rate"),
                "node": playback_node,
            },
            "capture_source": {
                "node_name": mic_source_node_name,
                "node": capture_node,
            },
            "reference_capture": {
                "source_node_name": str(reference_capture.get("source_node_name") or ""),
                "sink_node_name": str(reference_capture.get("sink_node_name") or ""),
                "channel": str(reference_capture.get("channel") or ""),
                "channel_label": str(reference_capture.get("channel_label") or ""),
                "uses_monitor_source": uses_monitor_source,
                "node": reference_node,
            },
            "record_node": record_node_name,
            "play_node": play_node_name,
            "requested_channel": channel,
            "sample_rate": sample_rate,
            "sweep": {
                key: sweep_meta.get(key)
                for key in (
                    "sample_rate",
                    "samples",
                    "channels",
                    "peak_linear",
                    "peak_dbfs",
                    "rms_dbfs",
                    "per_channel_peak_dbfs",
                    "would_clip_before_write",
                )
                if key in sweep_meta
            },
            "link_diagnostics": link_diagnostics,
            "snapshots": routing_snapshots,
            "process": {
                "pw_play_returncode": play_process.returncode if play_process is not None else None,
                "pw_record_returncode": record_process.returncode,
                "pw_play_timed_out": bool(play_timed_out),
                "pipewire_warning_lines": pipewire_warnings,
            },
        }
        if pipewire_warnings:
            logger.warning("Measurement PipeWire warnings: %s", pipewire_warnings)
        if not playback_node.get("id"):
            logger.warning("Measurement playback sink not found in PipeWire/PulseAudio node list: %s", playback_target["target_name"])
        if not capture_node.get("id"):
            logger.warning("Measurement capture source not found in PipeWire/PulseAudio node list: %s", mic_source_node_name)
        if not reference_node.get("id"):
            logger.warning("Measurement reference source not found in PipeWire/PulseAudio node list: %s", reference_capture.get("source_node_name"))
        if not uses_monitor_source:
            logger.warning("Measurement reference capture is not using a monitor source: %s", reference_capture.get("source_node_name"))
        if routing_diagnostics["sweep"].get("would_clip_before_write"):
            logger.warning("Measurement sweep would clip before playback: peak_dbfs=%s", routing_diagnostics["sweep"].get("peak_dbfs"))
        logger.info(
            "Measurement summary: playback_target=%s capture_source=%s reference_source=%s monitor=%s sample_rate=%s sweep_peak_dbfs=%s sweep_rms_dbfs=%s pipewire_warnings=%d detail=%s",
            routing_diagnostics["playback_sink"]["target_name"],
            routing_diagnostics["capture_source"]["node_name"],
            routing_diagnostics["reference_capture"]["source_node_name"],
            routing_diagnostics["reference_capture"]["uses_monitor_source"],
            sample_rate,
            routing_diagnostics["sweep"].get("peak_dbfs"),
            routing_diagnostics["sweep"].get("rms_dbfs"),
            len(pipewire_warnings),
            "debug" if detailed_diagnostics_enabled else "off",
        )
        logger.debug(
            "Measurement routing diagnostics: %s",
            json.dumps(routing_diagnostics, sort_keys=True),
        )
        return (
            analysis,
            {
                "path": str(capture_path),
                "duration_seconds": round(duration_seconds, 3),
                "sample_rate": sample_rate,
                "channels": capture_channels,
                "input_node": mic_source_node_name,
                "microphone_node": mic_source_node_name,
                "mic_input_channel": mic_input_channel_index + 1,
                "electrical_reference_input_channel": electrical_reference_channel_index + 1 if electrical_reference_channel_index is not None else None,
                "reference_node": str(reference_capture.get("source_node_name") or ""),
                "reference_channel": str(reference_capture.get("channel_label") or "reference"),
                "reference_path": str(capture_path),
                "record_node": record_node_name,
                "routing_diagnostics": routing_diagnostics,
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
                "routing_diagnostics": routing_diagnostics,
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

    def _evaluate_electrical_reference_status(self, analysis: dict[str, Any]) -> dict[str, Any]:
        reference_path = analysis.get("reference_path") if isinstance(analysis.get("reference_path"), dict) else {}
        clock = analysis.get("clock") if isinstance(analysis.get("clock"), dict) else {}
        peak_dbfs = float(reference_path.get("peak_dbfs") or -120.0)
        clipped = bool(reference_path.get("clipped")) or peak_dbfs >= CAPTURE_CLIP_FAIL_DBFS
        alignment_score = min(float(clock.get("start_score") or 0.0), float(clock.get("end_score") or 0.0))
        sharpness_db = float(reference_path.get("ir_sharpness_db") or 0.0)
        if clipped:
            return {"usable": False, "warning": "Electrical reference clipped; used host monitor timing fallback."}
        if peak_dbfs < ELECTRICAL_REFERENCE_MIN_PEAK_DBFS:
            return {"usable": False, "warning": "Electrical reference level was too low; used host monitor timing fallback."}
        if alignment_score < ELECTRICAL_REFERENCE_MIN_ALIGNMENT_SCORE:
            return {"usable": False, "warning": "Electrical reference timing was not confidently detected; used host monitor timing fallback."}
        if sharpness_db < ELECTRICAL_REFERENCE_MIN_IR_SHARPNESS_DB:
            return {"usable": False, "warning": "Electrical reference impulse was not sharp enough; used host monitor timing fallback."}
        reference_path["usable"] = True
        reference_path["electrical_reference_used"] = True
        reference_path["timing_status"] = "electrical-reference"
        reference_path["timing_label"] = "Electrical reference active"
        reference_path["confidence"] = round(min(alignment_score, max(0.0, sharpness_db / 60.0)), 6)
        reference_path["stability"] = "stable"
        analysis["reference_path"] = reference_path
        return {"usable": True, "warning": ""}

    @staticmethod
    def _append_reference_fallback_warning(analysis: dict[str, Any] | None, warning: str) -> None:
        if not isinstance(analysis, dict) or not warning:
            return
        quality_checks = analysis.setdefault("quality_checks", {"status": "pass", "items": []})
        items = quality_checks.setdefault("items", [])
        items.append({"level": "warning", "code": "electrical-reference-fallback", "message": warning})
        if quality_checks.get("status") == "pass":
            quality_checks["status"] = "warn"
        reference_path = analysis.get("reference_path") if isinstance(analysis.get("reference_path"), dict) else {}
        reference_path.update(
            {
                "electrical_reference_fallback": True,
                "electrical_reference_used": False,
                "timing_status": "electrical-reference-fallback",
                "timing_label": "Electrical reference fallback",
                "warning": warning,
                "usable": False,
                "stability": "fallback",
            }
        )
        analysis["reference_path"] = reference_path

    def _format_capture_input_level_message(self, peak_dbfs: float, clipped: bool) -> str:
        if clipped:
            return "Running sweep… CLIP"
        if peak_dbfs <= CAPTURE_LEVEL_STATUS_MIN_DBFS:
            return "Running sweep… Peak < -90 dBFS"
        return f"Running sweep… Peak {round(peak_dbfs):.0f} dBFS"

    @staticmethod
    def _format_measurement_timing_summary(analysis: dict[str, Any]) -> str:
        reference_path = analysis.get("reference_path") if isinstance(analysis.get("reference_path"), dict) else {}
        impulse = analysis.get("impulse_response") if isinstance(analysis.get("impulse_response"), dict) else {}
        timing_status = str(reference_path.get("timing_status") or "").strip()
        corrected_ms = reference_path.get("acoustic_arrival_corrected_ms", impulse.get("arrival_ms"))
        try:
            delay_ms = float(corrected_ms)
        except (TypeError, ValueError):
            delay_ms = math.nan
        delay_text = f"delay {delay_ms:.2f} ms" if math.isfinite(delay_ms) else "delay unavailable"
        stability = str(reference_path.get("stability") or "").strip().lower()
        confidence = reference_path.get("confidence", impulse.get("direct_confidence"))
        try:
            confidence_value = float(confidence)
        except (TypeError, ValueError):
            confidence_value = math.nan

        if timing_status == "electrical-reference":
            stability_text = "timing stable" if stability in {"stable", "usable"} else "timing active"
            return f"Electrical reference active · {delay_text} · {stability_text}"
        if timing_status == "electrical-reference-fallback":
            return f"Electrical reference fallback · {delay_text} · acoustic-only timing"
        confidence_text = "lower confidence" if not math.isfinite(confidence_value) or confidence_value < 0.75 else "timing stable"
        return f"Acoustic-only timing · {delay_text} · {confidence_text}"

    def _update_capture_input_level_status(self, job_id: str, peak_dbfs: float, clipped: bool) -> None:
        job = self._jobs.get(job_id)
        if not job or str(job.get("status") or "") != "running":
            return
        message = self._format_capture_input_level_message(peak_dbfs, clipped)
        now = self._utc_now()
        job["message"] = message
        job["updated_at"] = now
        job["input_level"] = {
            "peak_dbfs": round(max(CAPTURE_LEVEL_STATUS_MIN_DBFS, peak_dbfs), 1),
            "clipped": bool(clipped),
            "updated_at": now,
        }
        try:
            self._persist_job(job)
        except Exception:
            logger.debug("Failed to persist measurement input level status for %s", job_id, exc_info=True)

    def _monitor_capture_input_level(
        self,
        job_id: str,
        capture_path: Path,
        capture_channels: int,
        input_channel_index: int,
        stop_event: threading.Event,
    ) -> None:
        frame_bytes = max(1, int(capture_channels)) * 2
        read_offset = 44
        clipped = False
        while not stop_event.is_set():
            try:
                if capture_path.exists():
                    size = capture_path.stat().st_size
                    available = max(0, size - read_offset)
                    usable = (available // frame_bytes) * frame_bytes
                    if usable > 0:
                        with capture_path.open("rb") as handle:
                            handle.seek(read_offset)
                            chunk = handle.read(usable)
                        read_offset += len(chunk)
                        samples = np.frombuffer(chunk, dtype="<i2")
                        if samples.size >= capture_channels:
                            frames = samples.reshape(-1, int(capture_channels))
                            channel_index = max(0, min(int(input_channel_index), frames.shape[1] - 1))
                            channel_samples = frames[:, channel_index].astype(np.int32)
                            peak_sample = int(np.max(np.abs(channel_samples))) if channel_samples.size else 0
                            peak_dbfs = 20.0 * math.log10(max(peak_sample / 32768.0, 1e-9))
                            clipped = clipped or peak_sample >= 32760 or peak_dbfs >= CAPTURE_CLIP_FAIL_DBFS
                            self._update_capture_input_level_status(job_id, peak_dbfs, clipped)
            except Exception:
                logger.debug("Measurement input level monitor failed for %s", job_id, exc_info=True)
            stop_event.wait(CAPTURE_LEVEL_STATUS_INTERVAL_SECONDS)

    def _analysis_has_warning_code(self, analysis: dict[str, Any] | None, code: str) -> bool:
        if not isinstance(analysis, dict):
            return False
        items = ((analysis.get("quality_checks") or {}).get("items") or [])
        return any(str(item.get("code") or "").strip() == code and item.get("level") == "warning" for item in items)

    def _build_impulse_response_debug_segment(
        self,
        impulse_response: np.ndarray,
        *,
        sample_rate: int,
        channel: str,
        reference_channel: str,
        alignment_samples: int,
        direct_timing_meta: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not IR_DEBUG_SEGMENT_ENABLED or not _detailed_measurement_diagnostics_enabled():
            return None
        ir64 = impulse_response.astype(np.float64)
        if not ir64.size:
            return None
        ir_abs = np.abs(ir64)
        global_peak_sample = int(np.argmax(ir_abs))
        peak_value = float(ir_abs[global_peak_sample])
        if peak_value <= 0.0:
            return None

        first_threshold_sample = direct_timing_meta.get("first_threshold_index")
        selected_direct_sample = int(direct_timing_meta["direct_arrival_index"])
        reference_peak_sample = int(direct_timing_meta["reference_peak_index"])
        marker_samples = [global_peak_sample, selected_direct_sample]
        if first_threshold_sample is not None:
            marker_samples.append(int(first_threshold_sample))
        radius = int(IR_DEBUG_SEGMENT_RADIUS_SAMPLES)
        start = max(0, min(marker_samples) - radius)
        end = min(ir64.size, max(marker_samples) + radius + 1)

        candidates = [
            {
                "sample": int(item["sample"]),
                "offset_from_peak_samples": int(item.get("offset_from_peak_samples") or 0),
                "score": float(item.get("score") or 0.0),
                "support_score": float(item.get("support_score") or 0.0),
                "local_energy_relative": float(item.get("local_energy_relative") or 0.0),
                "prominence_relative": float(item.get("prominence_relative") or 0.0),
                "weak_threshold_edge": bool(item.get("weak_threshold_edge")),
                "stronger_impulse_region": bool(item.get("stronger_impulse_region")),
                "in_window": start <= int(item["sample"]) < end,
            }
            for item in (direct_timing_meta.get("candidates_chronological") or [])
            if isinstance(item, dict) and item.get("sample") is not None
        ]

        segment = []
        for sample in range(start, end):
            value = float(ir64[sample])
            normalized = value / peak_value
            segment.append(
                {
                    "sample": int(sample),
                    "offset_from_global_peak_samples": int(sample - global_peak_sample),
                    "offset_from_selected_direct_samples": int(sample - selected_direct_sample),
                    "value_normalized": round(float(normalized), 8),
                    "abs_normalized": round(abs(float(normalized)), 8),
                }
            )

        return {
            "schema": "fxroute.ir-debug-segment.v1",
            "channel": channel,
            "reference_channel": reference_channel,
            "sample_rate": int(sample_rate),
            "alignment_samples": int(alignment_samples),
            "window_radius_samples": radius,
            "window_start_sample": int(start),
            "window_end_sample": int(end),
            "window_sample_count": int(end - start),
            "normalization": {
                "mode": "signed impulse response divided by global absolute IR peak",
                "global_peak_abs": peak_value,
            },
            "markers": {
                "first_threshold_sample": int(first_threshold_sample) if first_threshold_sample is not None else None,
                "selected_direct_sample": selected_direct_sample,
                "global_peak_sample": global_peak_sample,
                "reference_peak_sample": reference_peak_sample,
                "arrival_samples": int(direct_timing_meta["relative_samples"]),
                "selection_rule": str(direct_timing_meta.get("selection_rule") or ""),
            },
            "candidate_markers": candidates,
            "segment": segment,
        }

    def _save_impulse_response_debug_segment(
        self,
        measurement_id: str,
        debug_segment: Any,
    ) -> dict[str, Any] | None:
        if not isinstance(debug_segment, dict):
            return None
        try:
            output_dir = self.diagnostics_dir / "impulse-ir"
            output_dir.mkdir(parents=True, exist_ok=True)
            channel = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(debug_segment.get("channel") or "channel")).strip("-") or "channel"
            base_name = f"{measurement_id}-{channel}-ir-segment"
            json_path = output_dir / f"{base_name}.json"
            csv_path = output_dir / f"{base_name}.csv"
            payload = deepcopy(debug_segment)
            payload["measurement_id"] = measurement_id
            payload["generated_at"] = self._utc_now()
            json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

            marker_names_by_sample: dict[int, list[str]] = {}
            for name, value in (payload.get("markers") or {}).items():
                if name.endswith("_sample") and value is not None:
                    marker_names_by_sample.setdefault(int(value), []).append(name)
            for index, candidate in enumerate(payload.get("candidate_markers") or []):
                sample = candidate.get("sample")
                if sample is not None:
                    marker_names_by_sample.setdefault(int(sample), []).append(f"candidate_{index + 1}")

            with csv_path.open("w", encoding="utf-8", newline="") as csv_file:
                writer = csv.DictWriter(
                    csv_file,
                    fieldnames=[
                        "measurement_id",
                        "channel",
                        "sample_rate",
                        "sample",
                        "offset_from_global_peak_samples",
                        "offset_from_selected_direct_samples",
                        "value_normalized",
                        "abs_normalized",
                        "markers",
                    ],
                )
                writer.writeheader()
                for item in payload.get("segment") or []:
                    sample = int(item["sample"])
                    writer.writerow(
                        {
                            "measurement_id": measurement_id,
                            "channel": payload.get("channel"),
                            "sample_rate": payload.get("sample_rate"),
                            "sample": sample,
                            "offset_from_global_peak_samples": item.get("offset_from_global_peak_samples"),
                            "offset_from_selected_direct_samples": item.get("offset_from_selected_direct_samples"),
                            "value_normalized": item.get("value_normalized"),
                            "abs_normalized": item.get("abs_normalized"),
                            "markers": "|".join(marker_names_by_sample.get(sample, [])),
                        }
                    )

            return {
                "enabled": True,
                "schema": payload.get("schema"),
                "json_path": str(json_path),
                "csv_path": str(csv_path),
                "window_radius_samples": payload.get("window_radius_samples"),
                "window_start_sample": payload.get("window_start_sample"),
                "window_end_sample": payload.get("window_end_sample"),
                "window_sample_count": payload.get("window_sample_count"),
                "normalization": payload.get("normalization"),
            }
        except Exception as exc:
            logger.warning("Unable to save measurement IR debug segment for %s: %s", measurement_id, exc)
            return {
                "enabled": True,
                "error": str(exc),
            }





    def _build_measurement_from_analysis(
        self,
        analysis: dict[str, Any],
        *,
        input_device: dict[str, str],
        channel: str,
        calibration: dict[str, Any],
        input_channels: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        timestamp = datetime.now(timezone.utc).replace(microsecond=0)
        created_at = timestamp.isoformat().replace("+00:00", "Z")
        label = f"Current sweep {timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}"
        measurement_id = f"sweep-{timestamp.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"
        impulse_response_debug = self._save_impulse_response_debug_segment(
            measurement_id,
            analysis.get("_impulse_response_debug_segment"),
        )
        payload = {
            "id": measurement_id,
            "name": label,
            "created_at": created_at,
            "input_device": input_device,
            "input_channels": deepcopy(input_channels or {}),
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
                "sample_rate": analysis["sample_rate"],
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
                "reference_path": analysis.get("reference_path"),
                "impulse_response": analysis["impulse_response"],
                "variable_window": analysis.get("variable_window"),
            },
        }
        if impulse_response_debug:
            payload["analysis"]["impulse_response"]["debug_segment"] = impulse_response_debug
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
        direct_timing_meta = self._estimate_impulse_direct_arrival(
            impulse_response,
            reference_impulse_response,
            sample_rate,
        )
        response_frequencies, response_magnitude, variable_window_meta = self._build_variable_window_response(
            impulse_response,
            sample_rate,
        )
        reference_ir_peak = float(np.max(np.abs(reference_impulse_response))) if reference_impulse_response.size else 0.0
        reference_ir_rms = float(np.sqrt(np.mean(np.square(reference_impulse_response, dtype=np.float64)))) if reference_impulse_response.size else 0.0
        reference_ir_peak_db = 20.0 * math.log10(max(reference_ir_peak, 1e-9))
        reference_ir_rms_db = 20.0 * math.log10(max(reference_ir_rms, 1e-9))
        reference_ir_sharpness_db = reference_ir_peak_db - reference_ir_rms_db
        timing_candidates_by_score = direct_timing_meta.get("candidates_by_score") or direct_timing_meta.get("candidates") or []
        timing_candidates_chronological = direct_timing_meta.get("candidates_chronological") or []
        top_timing_candidates_by_score = [
            f"{item.get('offset_from_peak_samples')}spl/{item.get('relative_db')}dB/s={item.get('score')}/e={item.get('local_energy_relative')}/p={item.get('prominence_relative')}/support={item.get('support_score')}"
            for item in timing_candidates_by_score[:5]
        ]
        top_timing_candidates_chronological = [
            f"{item.get('offset_from_peak_samples')}spl/{item.get('relative_db')}dB/s={item.get('score')}/e={item.get('local_energy_relative')}/p={item.get('prominence_relative')}/support={item.get('support_score')}/edge={item.get('weak_threshold_edge')}"
            for item in timing_candidates_chronological[:8]
        ]
        logger.info(
            "Measurement timing summary: channel=%s reference_channel=%s relative_samples=%s relative_ms=%.3f sample_rate=%s selected_score=%.5f selection=%s",
            channel,
            reference_channel_label,
            int(direct_timing_meta["relative_samples"]),
            float(direct_timing_meta["relative_seconds"]) * 1000.0,
            sample_rate,
            float(direct_timing_meta.get("selected_score") or 0.0),
            str(direct_timing_meta.get("selection_rule") or ""),
        )
        logger.debug(
            "Measurement timing detection: channel=%s reference_channel=%s mic_peak_sample=%s direct_sample=%s reference_peak_sample=%s relative_samples=%s relative_ms=%.3f sample_rate=%s alignment_samples=%s selected_score=%.5f selected_db=%.2f selection=%s first_threshold_sample=%s candidates_by_score=%s candidates_chronological=%s",
            channel,
            reference_channel_label,
            int(ir_meta["peak_index"]),
            int(direct_timing_meta["direct_arrival_index"]),
            int(direct_timing_meta["reference_peak_index"]),
            int(direct_timing_meta["relative_samples"]),
            float(direct_timing_meta["relative_seconds"]) * 1000.0,
            sample_rate,
            aligned_start,
            float(direct_timing_meta.get("selected_score") or 0.0),
            float(direct_timing_meta.get("direct_relative_to_peak_db") or -120.0),
            str(direct_timing_meta.get("selection_rule") or ""),
            direct_timing_meta.get("first_threshold_index"),
            top_timing_candidates_by_score,
            top_timing_candidates_chronological,
        )
        impulse_response_debug_segment = self._build_impulse_response_debug_segment(
            impulse_response,
            sample_rate=sample_rate,
            channel=channel,
            reference_channel=reference_channel_label,
            alignment_samples=aligned_start,
            direct_timing_meta=direct_timing_meta,
        )
        display_data = self._build_display_points(
            frequencies=response_frequencies,
            magnitude=response_magnitude,
            calibration_curve=calibration_curve,
        )
        capture_audit = self._build_capture_audit(
            raw_signal=raw_signal,
            sample_rate=sample_rate,
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
            "sample_rate": int(sample_rate),
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
                "direct_arrival_index": int(direct_timing_meta["direct_arrival_index"]),
                "direct_seconds": round(float(direct_timing_meta["direct_seconds"]), 6),
                "direct_relative_to_peak_db": direct_timing_meta["direct_relative_to_peak_db"],
                "direct_threshold_relative": round(float(direct_timing_meta["direct_threshold_relative"]), 4),
                "direct_selection_rule": direct_timing_meta["selection_rule"],
                "direct_selected_score": round(float(direct_timing_meta["selected_score"]), 6),
                "direct_selected_support_score": round(float(direct_timing_meta["selected_support_score"]), 6),
                "direct_confidence": round(float(direct_timing_meta["confidence"]), 6),
                "direct_first_threshold_index": direct_timing_meta["first_threshold_index"],
                "direct_first_threshold_offset_from_peak_samples": direct_timing_meta["first_threshold_offset_from_peak_samples"],
                "direct_candidate_count": int(direct_timing_meta["candidate_count"]),
                "direct_candidates": direct_timing_meta["candidates_by_score"],
                "direct_candidates_by_score": direct_timing_meta["candidates_by_score"],
                "direct_candidates_chronological": direct_timing_meta["candidates_chronological"],
                "reference_peak_index": int(direct_timing_meta["reference_peak_index"]),
                "reference_peak_seconds": round(float(direct_timing_meta["reference_peak_seconds"]), 6),
                "arrival_samples": int(direct_timing_meta["relative_samples"]),
                "arrival_seconds": round(float(direct_timing_meta["relative_seconds"]), 6),
                "arrival_ms": round(float(direct_timing_meta["relative_seconds"]) * 1000.0, 6),
                "timing_source": "direct_arrival_minus_reference_peak",
                "window_start_index": int(ir_meta["window_start_index"]),
                "window_end_index": int(ir_meta["window_end_index"]),
                "window_seconds": round(float(ir_meta["window_seconds"]), 6),
                "pre_window_seconds": round(float(ir_meta["pre_window_seconds"]), 6),
                "post_window_seconds": round(float(ir_meta["post_window_seconds"]), 6),
                "peak_dbfs": round(float(ir_meta["peak_dbfs"]), 2),
            },
            "variable_window": variable_window_meta,
            "_impulse_response_debug_segment": impulse_response_debug_segment,
        }
        hard_failures = [item["message"] for item in quality_checks["items"] if item.get("level") == "error"]
        if hard_failures:
            raise CaptureQualityError(capture_label, quality_checks["items"], analysis=analysis)
        return analysis
















    def _build_display_points(
        self,
        *,
        frequencies: np.ndarray,
        magnitude: np.ndarray,
        calibration_curve: tuple[np.ndarray, np.ndarray] | None,
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

        trimmed = low_index > 0 or high_index < total_points - 1
        selection_reasons = []
        if edge_trimmed:
            selection_reasons.append("edge-stability")
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
        }


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
        playback64 = playback.astype(np.float64)
        peak = float(np.max(np.abs(playback64))) if playback64.size else 0.0
        rms = float(np.sqrt(np.mean(np.square(playback64, dtype=np.float64)))) if playback64.size else 0.0
        per_channel_peak_dbfs = []
        if playback64.ndim > 1:
            for channel_index in range(playback64.shape[1]):
                channel_peak = float(np.max(np.abs(playback64[:, channel_index]))) if playback64.size else 0.0
                per_channel_peak_dbfs.append(round(20.0 * math.log10(max(channel_peak, 1e-9)), 2))
        return {
            "analysis_sweep": sweep,
            "inverse_sweep": inverse_sweep,
            "sample_rate": int(sample_rate),
            "samples": int(mono_program.size),
            "channels": 2,
            "peak_linear": round(peak, 8),
            "peak_dbfs": round(20.0 * math.log10(max(peak, 1e-9)), 2),
            "rms_dbfs": round(20.0 * math.log10(max(rms, 1e-9)), 2),
            "per_channel_peak_dbfs": per_channel_peak_dbfs,
            "would_clip_before_write": bool(peak > 1.0),
        }



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
    ) -> dict[str, Any]:
        if len(matches) < 2:
            raise RuntimeError("Sweep timing fit did not have enough anchors")

        offsets = np.array([float(item["offset_samples"]) for item in matches], dtype=np.float64)
        observed = np.array([float(item["observed_start"]) for item in matches], dtype=np.float64)
        scores = np.array([max(float(item.get("score") or 0.0), 1e-6) for item in matches], dtype=np.float64)
        weights = np.square(scores)
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

        start_score = self._aggregate_anchor_region_score(matches, inlier_mask, region="start")
        end_score = self._aggregate_anchor_region_score(matches, inlier_mask, region="end")
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
        return float(sum(top_scores) / len(top_scores))

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

    def _build_variable_window_response(self, impulse_response: np.ndarray, sample_rate: int) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
        low_windowed, low_meta = self._window_impulse_response(
            impulse_response,
            sample_rate,
            post_seconds=IR_WINDOW_POST_LOW_SECONDS,
        )
        mid_windowed, mid_meta = self._window_impulse_response(
            impulse_response,
            sample_rate,
            post_seconds=IR_WINDOW_POST_SECONDS,
        )
        high_windowed, high_meta = self._window_impulse_response(
            impulse_response,
            sample_rate,
            post_seconds=IR_WINDOW_POST_HIGH_SECONDS,
        )
        fft_size = self._next_pow2(max(sample_rate, low_windowed.size * 2, mid_windowed.size * 2, high_windowed.size * 2))
        frequencies = np.fft.rfftfreq(fft_size, d=1.0 / sample_rate)
        low_magnitude = np.abs(np.fft.rfft(low_windowed, n=fft_size))
        mid_magnitude = np.abs(np.fft.rfft(mid_windowed, n=fft_size))
        high_magnitude = np.abs(np.fft.rfft(high_windowed, n=fft_size))

        blend = np.clip(
            (frequencies - IR_WINDOW_VARIABLE_LOW_HZ) / max(IR_WINDOW_VARIABLE_HIGH_HZ - IR_WINDOW_VARIABLE_LOW_HZ, 1.0),
            0.0,
            1.0,
        )
        blend = blend * blend * (3.0 - (2.0 * blend))
        upper_blend = np.clip(
            (frequencies - IR_WINDOW_VARIABLE_HIGH_HZ) / max(IR_WINDOW_VARIABLE_HIGH_HZ, 1.0),
            0.0,
            1.0,
        )
        upper_blend = upper_blend * upper_blend * (3.0 - (2.0 * upper_blend))
        low_mid = (low_magnitude * (1.0 - blend)) + (mid_magnitude * blend)
        magnitude = (low_mid * (1.0 - upper_blend)) + (high_magnitude * upper_blend)
        return frequencies, magnitude, {
            "method": "frequency-dependent IR window blend",
            "low_post_window_seconds": round(float(low_meta["post_window_seconds"]), 6),
            "mid_post_window_seconds": round(float(mid_meta["post_window_seconds"]), 6),
            "high_post_window_seconds": round(float(high_meta["post_window_seconds"]), 6),
            "low_to_mid_hz": round(float(IR_WINDOW_VARIABLE_LOW_HZ), 3),
            "mid_to_high_hz": round(float(IR_WINDOW_VARIABLE_HIGH_HZ), 3),
        }

    def _window_impulse_response(
        self,
        impulse_response: np.ndarray,
        sample_rate: int,
        *,
        post_seconds: float = IR_WINDOW_POST_SECONDS,
    ) -> tuple[np.ndarray, dict[str, Any]]:
        ir64 = impulse_response.astype(np.float64)
        peak_index = int(np.argmax(np.abs(ir64)))
        pre_samples = max(32, int(round(sample_rate * IR_WINDOW_PRE_SECONDS)))
        post_samples = max(pre_samples * 2, int(round(sample_rate * post_seconds)))
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

    def _estimate_impulse_direct_arrival(
        self,
        impulse_response: np.ndarray,
        reference_impulse_response: np.ndarray,
        sample_rate: int,
    ) -> dict[str, Any]:
        ir_abs = np.abs(impulse_response.astype(np.float64))
        ref_abs = np.abs(reference_impulse_response.astype(np.float64))
        peak_index = int(np.argmax(ir_abs)) if ir_abs.size else 0
        reference_peak_index = int(np.argmax(ref_abs)) if ref_abs.size else 0
        peak = float(ir_abs[peak_index]) if ir_abs.size else 0.0
        threshold = peak * IR_DIRECT_RELATIVE_THRESHOLD
        search_pre_samples = max(1, int(round(sample_rate * IR_DIRECT_SEARCH_PRE_SECONDS)))
        search_start = max(0, peak_index - search_pre_samples)
        search_end = min(ir_abs.size, peak_index + 1)
        search_values = ir_abs[search_start:search_end]
        first_threshold_index: int | None = None
        if threshold > 0:
            threshold_crossings = np.flatnonzero(search_values >= threshold)
            if threshold_crossings.size:
                first_threshold_index = search_start + int(threshold_crossings[0])

        candidate_floor = peak * IR_DIRECT_CANDIDATE_FLOOR_RELATIVE
        candidates: list[dict[str, Any]] = []
        support_radius = max(4, int(round(sample_rate * IR_DIRECT_SUPPORT_WINDOW_SECONDS)))
        nearby_radius = max(support_radius + 4, int(round(sample_rate * IR_DIRECT_NEARBY_WINDOW_SECONDS)))
        if peak > 0 and search_values.size:
            for local_index in range(1, max(1, search_values.size - 1)):
                value = float(search_values[local_index])
                if value < candidate_floor:
                    continue
                if value < float(search_values[local_index - 1]) or value < float(search_values[local_index + 1]):
                    continue
                absolute_index = search_start + local_index
                relative = value / max(peak, 1e-12)
                support_start = max(search_start, absolute_index - support_radius)
                support_end = min(search_end, absolute_index + support_radius + 1)
                support_values = ir_abs[support_start:support_end]
                local_energy = float(np.sum(np.square(support_values, dtype=np.float64))) if support_values.size else 0.0
                earlier_start = max(search_start, absolute_index - nearby_radius)
                earlier_end = max(earlier_start, absolute_index - support_radius)
                later_start = min(search_end, absolute_index + support_radius + 1)
                later_end = min(search_end, absolute_index + nearby_radius + 1)
                earlier_values = ir_abs[earlier_start:earlier_end]
                later_values = ir_abs[later_start:later_end]
                nearby_reference = max(
                    float(np.max(earlier_values)) if earlier_values.size else 0.0,
                    float(np.max(later_values)) if later_values.size else 0.0,
                )
                prominence_relative = max(0.0, (value - nearby_reference) / max(peak, 1e-12))
                prominence_ratio = min(value / max(nearby_reference, 1e-12), 999.0)
                threshold_distance_samples = (
                    int(absolute_index - first_threshold_index) if first_threshold_index is not None else None
                )
                candidates.append(
                    {
                        "sample": int(absolute_index),
                        "seconds": round(float(absolute_index) / float(sample_rate), 6),
                        "offset_from_peak_samples": int(absolute_index - peak_index),
                        "offset_from_peak_ms": round(float(absolute_index - peak_index) / float(sample_rate) * 1000.0, 6),
                        "score": round(float(relative), 6),
                        "_score": float(relative),
                        "_local_energy": local_energy,
                        "_prominence_relative": float(prominence_relative),
                        "relative_db": round(20.0 * math.log10(max(relative, 1e-12)), 2),
                        "local_energy": round(local_energy, 8),
                        "prominence_relative": round(float(prominence_relative), 6),
                        "prominence_ratio": round(float(prominence_ratio), 3),
                        "distance_from_first_threshold_samples": threshold_distance_samples,
                    }
                )

        max_local_energy = max([float(item["_local_energy"]) for item in candidates] + [1e-12])
        for item in candidates:
            local_energy_relative = float(item["_local_energy"]) / max_local_energy
            prominence_score = min(float(item["_prominence_relative"]) / max(IR_DIRECT_PROMINENCE_REFERENCE, 1e-12), 1.0)
            support_score = (
                float(item["_score"]) * 0.45
                + local_energy_relative * 0.35
                + prominence_score * 0.20
            )
            threshold_distance_samples = item.get("distance_from_first_threshold_samples")
            weak_threshold_edge = (
                threshold_distance_samples is not None
                and 0 <= int(threshold_distance_samples) <= IR_DIRECT_THRESHOLD_EDGE_SAMPLES
                and float(item["_score"]) <= max(IR_DIRECT_WEAK_EARLY_RELATIVE, IR_DIRECT_RELATIVE_THRESHOLD)
                and local_energy_relative < 0.22
                and prominence_score < 0.45
            )
            stronger_impulse_region = (
                float(item["_score"]) >= 0.12
                or local_energy_relative >= 0.35
                or support_score >= 0.22
            )
            item["peak_score"] = round(float(item["_score"]), 6)
            item["local_energy_relative"] = round(local_energy_relative, 6)
            item["prominence_score"] = round(prominence_score, 6)
            item["support_score"] = round(float(support_score), 6)
            item["weak_threshold_edge"] = bool(weak_threshold_edge)
            item["stronger_impulse_region"] = bool(stronger_impulse_region)

        eligible_candidates = [
            item
            for item in candidates
            if float(item["_score"]) >= IR_DIRECT_RELATIVE_THRESHOLD
        ]
        if eligible_candidates:
            selected_candidate = eligible_candidates[0]
            skipped_early_candidate = None
            promotion_window_samples = max(
                IR_DIRECT_WEAK_EARLY_MIN_GAP_SAMPLES,
                int(round(sample_rate * IR_DIRECT_PROMOTION_WINDOW_SECONDS)),
            )
            first_sample = int(selected_candidate["sample"])
            early_candidate_is_weak = bool(selected_candidate.get("weak_threshold_edge")) or (
                float(selected_candidate["_score"]) <= IR_DIRECT_WEAK_EARLY_RELATIVE
                and (
                    selected_candidate.get("distance_from_first_threshold_samples") is None
                    or int(selected_candidate.get("distance_from_first_threshold_samples") or 0) <= IR_DIRECT_THRESHOLD_EDGE_SAMPLES
                )
            )
            if early_candidate_is_weak and len(eligible_candidates) > 1:
                selected_support = float(selected_candidate["support_score"])
                selected_score_candidate = float(selected_candidate["_score"])
                selected_energy = float(selected_candidate["local_energy_relative"])
                selected_prominence = float(selected_candidate["prominence_relative"])
                for candidate in eligible_candidates[1:]:
                    sample_gap = int(candidate["sample"]) - first_sample
                    if sample_gap > promotion_window_samples:
                        break
                    if sample_gap < IR_DIRECT_WEAK_EARLY_MIN_GAP_SAMPLES:
                        continue
                    candidate_support = float(candidate["support_score"])
                    candidate_score = float(candidate["_score"])
                    candidate_energy = float(candidate["local_energy_relative"])
                    candidate_prominence = float(candidate["prominence_relative"])
                    clearly_better_support = candidate_support >= selected_support * IR_DIRECT_PROMOTION_SUPPORT_RATIO
                    stronger_shape = (
                        candidate_score >= selected_score_candidate * IR_DIRECT_PROMOTION_SCORE_RATIO
                        or candidate_energy >= selected_energy * IR_DIRECT_PROMOTION_ENERGY_RATIO
                        or candidate_prominence >= selected_prominence * IR_DIRECT_PROMOTION_SCORE_RATIO
                    )
                    if clearly_better_support and stronger_shape and not bool(candidate.get("weak_threshold_edge")):
                        skipped_early_candidate = selected_candidate
                        selected_candidate = candidate
                        break
            direct_arrival_index = int(selected_candidate["sample"])
            selection_rule = (
                "skipped_weak_threshold_edge_for_stronger_impulse_region"
                if skipped_early_candidate is not None
                else "first_local_peak_above_threshold"
            )
        elif first_threshold_index is not None:
            direct_arrival_index = int(first_threshold_index)
            selection_rule = "first_threshold_crossing_fallback"
        else:
            direct_arrival_index = peak_index
            selection_rule = "global_peak_fallback"
        selected_score = float(ir_abs[direct_arrival_index]) / max(peak, 1e-12) if ir_abs.size else 0.0
        strongest_score = max([float(item["_score"]) for item in candidates] + [selected_score, 1e-12])
        selected_support = next(
            (float(item["support_score"]) for item in candidates if int(item["sample"]) == int(direct_arrival_index)),
            selected_score,
        )
        for item in candidates:
            item.pop("_score", None)
            item.pop("_local_energy", None)
            item.pop("_prominence_relative", None)
        candidate_summary_by_score = sorted(
            candidates,
            key=lambda item: (-float(item["score"]), -float(item["prominence_score"]), -float(item["support_score"]), int(item["sample"])),
        )[:IR_DIRECT_CANDIDATE_LIMIT]
        candidate_summary_chronological = sorted(
            candidates,
            key=lambda item: int(item["sample"]),
        )[:IR_DIRECT_CANDIDATE_LIMIT]
        relative_samples = int(direct_arrival_index - reference_peak_index)
        return {
            "direct_arrival_index": int(direct_arrival_index),
            "direct_seconds": float(direct_arrival_index) / float(sample_rate),
            "direct_relative_to_peak_db": round(
                20.0 * math.log10(max(float(ir_abs[direct_arrival_index]) / max(peak, 1e-12), 1e-12)),
                2,
            ),
            "direct_threshold_relative": float(IR_DIRECT_RELATIVE_THRESHOLD),
            "selection_rule": selection_rule,
            "selected_score": selected_score,
            "selected_support_score": selected_support,
            "confidence": selected_score / max(strongest_score, 1e-12),
            "first_threshold_index": first_threshold_index,
            "first_threshold_offset_from_peak_samples": (
                int(first_threshold_index - peak_index) if first_threshold_index is not None else None
            ),
            "weak_early_relative": float(IR_DIRECT_WEAK_EARLY_RELATIVE),
            "weak_early_min_gap_samples": int(IR_DIRECT_WEAK_EARLY_MIN_GAP_SAMPLES),
            "weak_early_next_ratio": float(IR_DIRECT_WEAK_EARLY_NEXT_RATIO),
            "promotion_window_samples": int(max(IR_DIRECT_WEAK_EARLY_MIN_GAP_SAMPLES, int(round(sample_rate * IR_DIRECT_PROMOTION_WINDOW_SECONDS)))),
            "promotion_support_ratio": float(IR_DIRECT_PROMOTION_SUPPORT_RATIO),
            "candidate_count": len(candidates),
            "candidates": candidate_summary_by_score,
            "candidates_by_score": candidate_summary_by_score,
            "candidates_chronological": candidate_summary_chronological,
            "reference_peak_index": int(reference_peak_index),
            "reference_peak_seconds": float(reference_peak_index) / float(sample_rate),
            "relative_samples": relative_samples,
            "relative_seconds": float(relative_samples) / float(sample_rate),
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

    @staticmethod
    def _parse_input_channel_index(
        value: str | int | None,
        *,
        channel_count: int,
        default: int,
        field_name: str,
    ) -> int:
        raw_value = str(value if value is not None else "").strip()
        if not raw_value:
            return max(0, min(default, max(0, channel_count - 1)))
        try:
            parsed = int(raw_value)
        except (TypeError, ValueError):
            raise ValueError(f"{field_name} must be between 1 and {channel_count}")
        if parsed < 1 or parsed > channel_count:
            raise ValueError(f"{field_name} must be between 1 and {channel_count}")
        return parsed - 1

    def _parse_optional_input_channel_index(
        self,
        value: str | int | None,
        *,
        channel_count: int,
        field_name: str,
    ) -> int | None:
        raw_value = str(value if value is not None else "").strip()
        if not raw_value:
            return None
        return self._parse_input_channel_index(raw_value, channel_count=channel_count, default=0, field_name=field_name)

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
    ) -> dict[str, Any]:
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

        capture_subject = capture_label if capture_label else "Capture"
        capture_subject_lower = capture_subject[:1].lower() + capture_subject[1:] if capture_subject else "capture"
        playback_subject = "capture/playback"
        if peak_dbfs >= CAPTURE_CLIP_FAIL_DBFS:
            add("error", "capture-clipped", f"Recorded sweep clipped at {peak_dbfs:.2f} dBFS.")
        elif peak_dbfs >= CAPTURE_CLIP_WARN_DBFS:
            add("warning", "capture-near-clipping", f"Recorded sweep peaked very close to clipping ({peak_dbfs:.2f} dBFS).")

        rms_dbfs = float(capture_audit.get("rms_dbfs") or 0.0)
        drift_ppm = abs(float(timing.get("drift_ppm") or 0.0))
        alignment_fail_threshold = ALIGNMENT_SCORE_FAIL_THRESHOLD
        alignment_warn_threshold = ALIGNMENT_SCORE_WARN_THRESHOLD
        if capture_label == "Host-local capture":
            alignment_fail_threshold = HOST_ALIGNMENT_SCORE_FAIL_THRESHOLD
            alignment_warn_threshold = HOST_ALIGNMENT_SCORE_WARN_THRESHOLD
        start_score = float(timing.get("start_score") or 0.0)
        end_score = float(timing.get("end_score") or 0.0)
        if start_score < alignment_fail_threshold:
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
        if not trusted_band_meta.get("stable_high_edge", True):
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
        mic_input_channel_index: int = 0,
    ) -> dict[str, Any]:
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
        mic_suffixes = self._port_suffixes_for_channel_index(mic_input_channel_index) + [":capture_MONO", ":output_MONO"]
        reference_port = self._pick_port(reference_ports, reference_suffixes)
        mic_port = self._pick_port(mic_ports, mic_suffixes)
        input_left = self._pick_port(record_inputs, [":input_FL", ":input_MONO"])
        input_right = self._pick_port(record_inputs, [":input_FR", ":input_MONO", ":input_FL"])
        if not reference_port or not mic_port or not input_left or not input_right:
            raise RuntimeError("Could not resolve PipeWire ports for host-reference capture")

        subprocess.run(["pw-link", reference_port, input_left], capture_output=True, text=True, timeout=3, check=True)
        subprocess.run(["pw-link", mic_port, input_right], capture_output=True, text=True, timeout=3, check=True)
        time.sleep(0.15)
        return {
            "reference_source_node": reference_source_node_name,
            "microphone_source_node": mic_source_node_name,
            "record_node": record_node_name,
            "links": [
                {"source_port": reference_port, "target_port": input_left, "role": "reference-monitor-to-record-left"},
                {"source_port": mic_port, "target_port": input_right, "role": "microphone-to-record-right"},
            ],
            "record_inputs": record_inputs,
            "reference_ports": reference_ports,
            "microphone_ports": mic_ports,
        }

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

    def _link_capture_channels_to_record_stream(
        self,
        *,
        source_node_name: str,
        record_node_name: str,
        channel_indices: list[int],
    ) -> dict[str, Any]:
        deadline = time.monotonic() + 4.0
        source_ports: list[str] = []
        record_ports: list[str] = []
        while time.monotonic() < deadline:
            source_ports = self._list_source_output_ports(source_node_name)
            record_ports = self._list_pw_ports(record_node_name)
            record_inputs = [port for port in record_ports if ":input_" in port]
            if source_ports and record_inputs:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError(f"Unable to discover PipeWire ports for selected input channels on {source_node_name}")

        record_inputs = [port for port in record_ports if ":input_" in port]
        links: list[dict[str, str | int]] = []
        used_pairs: set[tuple[str, str]] = set()
        for channel_index in channel_indices:
            source_port = self._pick_preferred_port(source_ports, self._port_suffixes_for_channel_index(channel_index))
            input_port = self._pick_preferred_port(record_inputs, self._record_input_suffixes_for_channel_index(channel_index))
            if not source_port or not input_port:
                raise RuntimeError(f"Could not resolve PipeWire ports for Input {channel_index + 1}")
            pair = (source_port, input_port)
            if pair in used_pairs:
                continue
            subprocess.run(["pw-link", source_port, input_port], capture_output=True, text=True, timeout=3, check=True)
            used_pairs.add(pair)
            links.append(
                {
                    "source_port": source_port,
                    "target_port": input_port,
                    "input_channel": channel_index + 1,
                    "role": "selected-capture-channel-to-record",
                }
            )

        time.sleep(0.15)
        return {
            "source_node": source_node_name,
            "record_node": record_node_name,
            "links": links,
            "source_ports": source_ports,
            "record_inputs": record_inputs,
        }

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

    def _build_measurement_routing_snapshot(
        self,
        *,
        label: str,
        playback_target: dict[str, Any],
        mic_source_node_name: str,
        reference_capture: dict[str, Any],
        record_node_name: str,
        play_node_name: str,
    ) -> dict[str, Any]:
        relevant_nodes = [
            str(playback_target.get("target_name") or ""),
            mic_source_node_name,
            str(reference_capture.get("source_node_name") or ""),
            str(reference_capture.get("sink_node_name") or ""),
            record_node_name,
            play_node_name,
            "easyeffects_sink",
            "easyeffects_source",
        ]
        relevant_nodes = [node for node in relevant_nodes if node]
        snapshot = {
            "label": label,
            "captured_at": self._utc_now(),
            "default_sink": self._pactl_info_value("Default Sink"),
            "default_source": self._pactl_info_value("Default Source"),
            "sinks": self._list_pactl_short_nodes("sinks", relevant_nodes),
            "sources": self._list_pactl_short_nodes("sources", relevant_nodes),
            "ports": {node: self._list_pw_ports(node) for node in relevant_nodes},
            "links": self._list_relevant_pw_links(relevant_nodes),
        }
        snapshot["monitor_sources_involved"] = [
            node
            for node in relevant_nodes
            if node.endswith(".monitor") or any(".monitor" in port for port in snapshot["ports"].get(node, []))
        ]
        snapshot["easyeffects_sink_inputs"] = [
            line
            for line in snapshot["links"]
            if "easyeffects_sink:playback_" in line and ("|<-" in line or "|->" in line)
        ]
        return snapshot

    def _lookup_pipewire_audio_node(self, node_name: str) -> dict[str, Any]:
        if not node_name:
            return {}
        for kind in ("sinks", "sources"):
            for item in self._list_pactl_short_nodes(kind, [node_name]):
                if item.get("name") == node_name:
                    item["kind"] = kind[:-1]
                    return item
        return {"name": node_name}

    @staticmethod
    def _extract_pipewire_warning_lines(outputs: dict[str, str]) -> list[dict[str, str]]:
        warning_patterns = ("xrun", "underrun", "overrun", "buffer", "warning", "warn", "error", "failed")
        items: list[dict[str, str]] = []
        for stream_name, text_value in outputs.items():
            for raw_line in (text_value or "").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                lowered = line.lower()
                if any(pattern in lowered for pattern in warning_patterns):
                    items.append({"stream": stream_name, "line": line[:500]})
        return items[:40]

    @staticmethod
    def _pactl_info_value(key: str) -> str | None:
        try:
            completed = subprocess.run(["pactl", "info"], capture_output=True, text=True, timeout=3)
        except Exception:
            return None
        if completed.returncode != 0:
            return None
        prefix = f"{key}:"
        for raw_line in (completed.stdout or "").splitlines():
            if raw_line.startswith(prefix):
                value = raw_line.split(":", 1)[1].strip()
                return value or None
        return None

    @staticmethod
    def _list_pactl_short_nodes(kind: str, relevant_nodes: list[str]) -> list[dict[str, Any]]:
        if kind not in {"sinks", "sources"}:
            return []
        try:
            completed = subprocess.run(["pactl", "list", "short", kind], capture_output=True, text=True, timeout=3)
        except Exception:
            return []
        if completed.returncode != 0:
            return []
        relevant = {node for node in relevant_nodes if node}
        items: list[dict[str, Any]] = []
        for line in (completed.stdout or "").splitlines():
            parts = line.split("\t")
            if len(parts) < 5:
                continue
            name = parts[1].strip()
            if relevant and name not in relevant:
                continue
            sample_spec = parts[3].strip()
            rate_match = re.search(r"(\d+)Hz", sample_spec)
            items.append(
                {
                    "id": parts[0].strip(),
                    "name": name,
                    "driver": parts[2].strip(),
                    "sample_spec": sample_spec,
                    "sample_rate": int(rate_match.group(1)) if rate_match else None,
                    "state": parts[4].strip(),
                }
            )
        return items

    @staticmethod
    def _list_relevant_pw_links(relevant_nodes: list[str]) -> list[str]:
        try:
            completed = subprocess.run(["pw-link", "-l"], capture_output=True, text=True, timeout=3)
        except Exception:
            return []
        if completed.returncode != 0:
            return []
        relevant = [node for node in relevant_nodes if node]
        lines = (completed.stdout or "").splitlines()
        kept: list[str] = []
        current_header = ""
        current_block: list[str] = []

        def flush_block() -> None:
            if not current_block:
                return
            block_text = "\n".join(current_block)
            if any(node in block_text for node in relevant):
                kept.extend(line[:500] for line in current_block)

        for raw_line in lines:
            line = raw_line.rstrip()
            if not line.startswith((" ", "\t", "|")):
                flush_block()
                current_header = line
                current_block = [current_header]
            else:
                current_block.append(line)
        flush_block()
        return kept[:240]

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

    @staticmethod
    def _pick_preferred_port(ports: list[str], preferred_suffixes: list[str]) -> str | None:
        for suffix in preferred_suffixes:
            for port in ports:
                if port.endswith(suffix):
                    return port
        return None

    @staticmethod
    def _port_suffixes_for_channel_index(channel_index: int) -> list[str]:
        surround_names = [
            "FL",
            "FR",
            "RL",
            "RR",
            "FC",
            "LFE",
            "SL",
            "SR",
            "AUX0",
            "AUX1",
            "AUX2",
            "AUX3",
            "AUX4",
            "AUX5",
        ]
        aux_name = f"AUX{channel_index}"
        names = []
        if 0 <= channel_index < len(surround_names):
            names.append(surround_names[channel_index])
        names.append(aux_name)
        suffixes = []
        for name in dict.fromkeys(names):
            suffixes.extend([f":capture_{name}", f":output_{name}", f":monitor_{name}"])
        return suffixes

    @staticmethod
    def _record_input_suffixes_for_channel_index(channel_index: int) -> list[str]:
        surround_names = [
            "FL",
            "FR",
            "RL",
            "RR",
            "FC",
            "LFE",
            "SL",
            "SR",
            "AUX0",
            "AUX1",
            "AUX2",
            "AUX3",
            "AUX4",
            "AUX5",
        ]
        aux_name = f"AUX{channel_index}"
        names = []
        if 0 <= channel_index < len(surround_names):
            names.append(surround_names[channel_index])
        names.append(aux_name)
        return [f":input_{name}" for name in dict.fromkeys(names)]

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
        input_channels = payload.get("input_channels") if isinstance(payload.get("input_channels"), dict) else {}
        calibration = payload.get("calibration") if isinstance(payload.get("calibration"), dict) else {}
        display = deepcopy(DISPLAY_DEFAULTS)
        if isinstance(payload.get("display"), dict):
            display.update(payload["display"])
        try:
            normalized_mic_input_channel = max(1, int(input_channels.get("mic") or 1))
        except (TypeError, ValueError):
            normalized_mic_input_channel = 1
        try:
            normalized_reference_input_channel = int(input_channels["electrical_reference"]) if input_channels.get("electrical_reference") else None
        except (TypeError, ValueError):
            normalized_reference_input_channel = None

        result = {
            "id": measurement_id,
            "name": name,
            "created_at": created_at,
            "input_device": {
                "id": str(input_device.get("id") or "capture-input"),
                "label": str(input_device.get("label") or "Capture input"),
            },
            "input_channels": {
                "mic": normalized_mic_input_channel,
                "electrical_reference": normalized_reference_input_channel,
                "reference_disabled_reason": str(input_channels.get("reference_disabled_reason") or ""),
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
