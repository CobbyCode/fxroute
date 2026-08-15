"""Separate measurement capture, analysis, and persistence for FXRoute."""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import math
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import wave
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import numpy as np
from dsp_runtime import DSPRuntimeConfig

from hybrid_measurement import analyze_direct_window, build_complex_response, build_gated_response
from measurement_audio import MeasurementAudioAdapter
from measurement_file_store import MeasurementFileStore
from measurement_signal import write_sweep_file
from measurement_job_runner import MeasurementJobRunner
from measurement_repeat_runner import MeasurementRepeatRunner
from measurement_analyzer import MeasurementAnalyzer
from samplerate import (
    OUTPUT_MODE_SUBWOOFER_21,
    OUTPUT_MODE_SUBWOOFER_22,
    OUTPUT_MODE_SUBWOOFER_22_MODES,
    OUTPUT_MODE_SUBWOOFER_22_STEREO,
    OUTPUT_MODE_SUBWOOFER_MODES,
    get_audio_output_overview,
    get_samplerate_status,
)
from system_volume import SystemVolumeError, get_node_volume, get_output_volume, set_node_volume, set_output_volume

logger = logging.getLogger(__name__)

MEASUREMENT_SCOPE_ACTIVE_CHAIN = "active_chain"
MEASUREMENT_SCOPE_RAW_HELPER = "raw_helper"
MEASUREMENT_SCOPES = {
    MEASUREMENT_SCOPE_ACTIVE_CHAIN,
    MEASUREMENT_SCOPE_RAW_HELPER,
}
MEASUREMENT_DEFAULT_SAMPLE_RATE = 48_000


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
LR_REPEAT_PAIRED_DELTA_CLUSTER_MS = 0.35
LR_REPEAT_PAIRED_MIN_CLUSTER_SIZE = 2
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
SWEEP_TIMING_MIN_ANCHOR_SCORE = 0.995
SWEEP_TIMING_CLUSTER_REJECT_SAMPLES = 24
SWEEP_TIMING_CENTRAL_ANCHORS = {"mid-low", "mid-high"}
SWEEP_TIMING_EDGE_ANCHORS = {"start-inner", "end-inner"}
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
IR_DIRECT_WEAK_EARLY_RELATIVE = 0.12
IR_DIRECT_WEAK_EARLY_MIN_GAP_SAMPLES = 20
IR_DIRECT_WEAK_EARLY_NEXT_RATIO = 1.75
IR_DIRECT_SUPPORT_WINDOW_SECONDS = 0.00035
IR_DIRECT_NEARBY_WINDOW_SECONDS = 0.0009
IR_DIRECT_THRESHOLD_EDGE_SAMPLES = 12
IR_DIRECT_PROMOTION_WINDOW_SECONDS = 0.010
IR_DIRECT_PROMOTION_SUPPORT_RATIO = 1.4
IR_DIRECT_PROMOTION_SCORE_RATIO = 1.05
IR_DIRECT_PROMOTION_ENERGY_RATIO = 1.3
IR_DIRECT_CONFIDENCE_FALLBACK_THRESHOLD = 0.30
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
PRIME_TIMEOUT_MARGIN_SECONDS = 10.0
ALIGNMENT_SCORE_FAIL_THRESHOLD = 0.90
ALIGNMENT_SCORE_WARN_THRESHOLD = 0.94
HOST_ALIGNMENT_SCORE_FAIL_THRESHOLD = 0.84
HOST_ALIGNMENT_SCORE_WARN_THRESHOLD = 0.90
CAPTURE_CLIP_FAIL_DBFS = -0.2
CAPTURE_CLIP_WARN_DBFS = -1.0
CAPTURE_LEVEL_LOW_PEAK_DBFS = -45.0
CAPTURE_LEVEL_LOW_RMS_DBFS = -60.0
ELECTRICAL_REFERENCE_MIN_PEAK_DBFS = -70.0
ELECTRICAL_REFERENCE_MIN_ALIGNMENT_SCORE = 0.84
ELECTRICAL_REFERENCE_MIN_IR_SHARPNESS_DB = 18.0
CAPTURE_LEVEL_STATUS_INTERVAL_SECONDS = 0.35
CAPTURE_LEVEL_STATUS_MIN_DBFS = -90.0
CLOCK_DRIFT_WARN_PPM = 3_000.0
CHANNEL_CORRELATION_WARN_THRESHOLD = 0.985

MEASUREMENT_SCOPE_NOTE = (
    "FXRoute measures with a host-local sweep through the active PipeWire output and selected microphone input. "
    "The result is a practical response trace for comparison and PEQ drafting, independent of the active DSP preset."
)

TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled"})

JOB_RECORD_RETENTION_DAYS = 30
IR_DEBUG_SEGMENT_RETENTION_SEGMENTS = 10


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

    def __init__(self, home: Path | None = None, *,
                 runtime_snapshot_provider: Callable[[], dict[str, Any]] | None = None,
                 effect_bypass_setter: Callable[[bool], Any] | None = None,
                 raw_scope_enter: Callable[[], Any] | None = None,
                 raw_scope_exit: Callable[[bool], Any] | None = None,
                 active_scope_enter: Callable[[], Any] | None = None,
                 active_scope_exit: Callable[[bool], Any] | None = None):
        self.home = Path(home or Path.home())
        self.runtime_snapshot_provider = runtime_snapshot_provider
        self.effect_bypass_setter = effect_bypass_setter
        self.raw_scope_enter = raw_scope_enter
        self.raw_scope_exit = raw_scope_exit
        self.active_scope_enter = active_scope_enter
        self.active_scope_exit = active_scope_exit
        self.config_root = Path(os.environ.get("XDG_CONFIG_HOME") or (self.home / ".config"))
        self.state_root = Path(os.environ.get("XDG_STATE_HOME") or (self.home / ".local" / "state"))
        self.measurements_dir = self.config_root / "fxroute" / "measurements"
        self.jobs_dir = self.state_root / "fxroute" / "measurements"
        self.captures_dir = self.jobs_dir / "captures"
        self.calibrations_dir = self.jobs_dir / "calibrations"
        self.house_curves_dir = self.jobs_dir / "house_curves"
        self.settings_path = self.jobs_dir / "settings.json"
        self.job_records_dir = self.jobs_dir / "jobs"
        self.playbacks_dir = self.jobs_dir / "playbacks"
        self.diagnostics_dir = self.jobs_dir / "diagnostics"
        for directory in [
            self.measurements_dir,
            self.jobs_dir,
            self.captures_dir,
            self.calibrations_dir,
            self.house_curves_dir,
            self.job_records_dir,
            self.playbacks_dir,
            self.diagnostics_dir,
        ]:
            directory.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, dict[str, Any]] = {}
        self._shutdown = False
        self._last_successful_lag: int | None = None
        self.audio_adapter = MeasurementAudioAdapter()
        self._job_runner = MeasurementJobRunner(
            get_job=lambda job_id: self._jobs[job_id],
            persist_job=self._persist_job,
            public_result=self._public_measurement_job_result,
            cleanup_job=self._cleanup_job_wav_files,
            retain_history=self._retain_job_history,
            utc_now=self._utc_now,
            is_terminal=self._is_terminal_job_status,
            raw_scope_enter=self.raw_scope_enter,
            raw_scope_exit=self.raw_scope_exit,
            effect_bypass_setter=self.effect_bypass_setter,
            active_scope_enter=self.active_scope_enter,
            active_scope_exit=self.active_scope_exit,
        )
        self._repeat_runner = MeasurementRepeatRunner(self)
        self._analyzer = MeasurementAnalyzer(self, CaptureQualityError)
        self._file_store = MeasurementFileStore(self.jobs_dir, has_active_job=self.has_active_measurement_job)
        self.calibrations_dir = self._file_store.calibrations_dir
        self.house_curves_dir = self._file_store.house_curves_dir
        self.settings_path = self._file_store.settings_path

    @property
    def _job_tasks(self):
        return self._job_runner.tasks

    @property
    def _job_processes(self):
        return self._job_runner.processes

    @property
    def _job_process_lock(self):
        return self._job_runner.process_lock

    @property
    def _cancelled_jobs(self):
        return self._job_runner.cancelled_jobs

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
            "house_curves": self._list_house_curve_files(),
            "scope_note": MEASUREMENT_SCOPE_NOTE,
            "measurements": measurements,
        }

    def list_inputs(self) -> dict[str, Any]:
        inputs = self._measurement_inputs_with_sample_rate(self._discover_capture_inputs())
        settings = self._read_settings()
        measure_settings = settings.get("measure") if isinstance(settings.get("measure"), dict) else {}
        selection = resolve_measurement_input_selection(inputs, measure_settings)
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
            "selection": selection,
            "capture_available": any(item.get("available") for item in inputs),
            "discovery": {
                "method": "wpctl status -n + pactl list short sources",
                "source_count": len(inputs),
            },
        }

    def _measurement_inputs_with_sample_rate(self, inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        measurement_rate = self._resolve_measurement_sample_rate()
        annotated: list[dict[str, Any]] = []
        for item in inputs:
            source_sample_rate = item.get("sample_rate")
            annotated.append({
                **item,
                "persistent_id": measurement_input_persistent_id(item),
                "source_sample_rate": source_sample_rate,
                "sample_rate": measurement_rate,
                "measurement_sample_rate": measurement_rate,
            })
        return annotated

    async def start_measurement(
        self,
        *,
        input_id: str,
        input_key: str = "",
        channel: str,
        mic_input_channel: str | int | None = "1",
        reference_input_channel: str | int | None = "",
        calibration_filename: str | None = None,
        calibration_bytes: bytes | None = None,
        calibration_ref: str | None = None,
        sweep_profile: dict[str, float] | None = None,
        measurement_scope: str = MEASUREMENT_SCOPE_ACTIVE_CHAIN,
        playback_gain: float | None = None,
        measurement_role: str = "",
    ) -> dict[str, Any]:
        if self._shutdown:
            raise RuntimeError("Measurement store is shutting down")
        # Promote stale non-terminal jobs without a live worker before the
        # active-job guard, otherwise a stale cancelling/running record could
        # block every future measurement forever.
        self._normalize_stale_jobs()
        # Guard against concurrent measurement jobs
        active_job = self._find_active_or_cancelling_job()
        if active_job is not None:
            active_id = active_job["id"]
            active_status = active_job.get("status", "unknown")
            logger.warning(
                "MEASUREMENT-CANCEL-DIAG new job blocked: existing_job=%s status=%s",
                active_id, active_status,
            )
            raise RuntimeError(
                f"Another measurement is still active ({active_id}, status={active_status}). "
                "Wait for it to finish or cancel it first."
            )
        inputs = self._measurement_inputs_with_sample_rate(
            await asyncio.to_thread(self._discover_capture_inputs)
        )
        selected_input = self._resolve_capture_input(inputs, input_id=input_id, input_key=input_key)
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

        calibration_meta = self._file_store.resolve_calibration_meta(
            calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes,
            calibration_ref=calibration_ref,
        )
        normalized_scope = self._normalize_measurement_scope(measurement_scope)
        normalized_role = str(measurement_role or "").strip().lower()
        if normalized_role not in {"", "direct", "mlp", "secondary", "integration"}:
            raise ValueError("measurement_role must be direct, mlp, secondary, or integration")
        normalized_playback_gain = None
        if playback_gain is not None:
            try:
                normalized_playback_gain = float(playback_gain)
            except (TypeError, ValueError) as exc:
                raise ValueError("playback_gain must be a finite non-negative number") from exc
            if not math.isfinite(normalized_playback_gain) or normalized_playback_gain < 0.0:
                raise ValueError("playback_gain must be a finite non-negative number")

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
            "measurement_scope": normalized_scope,
            "playback_gain": normalized_playback_gain,
            "measurement_role": normalized_role,
            "result": None,
            "error": None,
            "sweep_profile": sweep_profile if isinstance(sweep_profile, dict) and sweep_profile else None,
        }
        self._jobs[job_id] = job
        self._persist_job(job)
        task = self._job_runner.start(
            job_id,
            job,
            self._execute_lr_repeat_job if job.get("job_kind") == "lr-repeat" else self._execute_capture_job,
        )
        return self.get_job(job_id)

    async def start_lr_repeat_measurement(
        self,
        *,
        input_id: str,
        input_key: str = "",
        base_name: str = "",
        mic_input_channel: str | int | None = "1",
        reference_input_channel: str | int | None = "",
        calibration_filename: str | None = None,
        calibration_bytes: bytes | None = None,
        calibration_ref: str | None = None,
        measurement_scope: str = MEASUREMENT_SCOPE_ACTIVE_CHAIN,
    ) -> dict[str, Any]:
        normalized_repeat_count = 3
        # Guard against concurrent measurement jobs
        if self._shutdown:
            raise RuntimeError("Measurement store is shutting down")
        # Promote stale non-terminal jobs without a live worker before the
        # active-job guard, otherwise a stale cancelling/running record could
        # block every future measurement forever.
        self._normalize_stale_jobs()
        active_job = self._find_active_or_cancelling_job()
        if active_job is not None:
            active_id = active_job["id"]
            active_status = active_job.get("status", "unknown")
            logger.warning(
                "MEASUREMENT-CANCEL-DIAG new job blocked: existing_job=%s status=%s",
                active_id, active_status,
            )
            raise RuntimeError(
                f"Another measurement is still active ({active_id}, status={active_status}). "
                "Wait for it to finish or cancel it first."
            )
        inputs = self._measurement_inputs_with_sample_rate(
            await asyncio.to_thread(self._discover_capture_inputs)
        )
        selected_input = self._resolve_capture_input(inputs, input_id=input_id, input_key=input_key)
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
        calibration_meta = self._file_store.resolve_calibration_meta(
            calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes,
            calibration_ref=calibration_ref,
        )
        normalized_scope = self._normalize_measurement_scope(measurement_scope)
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
            "measurement_scope": normalized_scope,
            "result": None,
            "error": None,
        }
        self._jobs[job_id] = job
        self._persist_job(job)
        task = self._job_runner.start(job_id, job, self._execute_lr_repeat_job)
        return self.get_job(job_id)

    @staticmethod
    def _resolve_capture_input(
        inputs: list[dict[str, Any]],
        *,
        input_id: str,
        input_key: str = "",
    ) -> dict[str, Any]:
        stable_key = str(input_key or "").strip()
        if stable_key:
            matches = [
                item for item in inputs
                if str(item.get("persistent_id") or measurement_input_persistent_id(item)) == stable_key
            ]
            if len(matches) > 1:
                raise ValueError("Selected capture input identity is ambiguous")
            selected = matches[0] if matches else None
        else:
            selected = next((item for item in inputs if item.get("id") == input_id), None)
        if not selected:
            raise ValueError("Selected capture input is no longer available")
        return selected

    def resolve_capture_input_id(self, *, input_id: str, input_key: str = "") -> str:
        inputs = self._measurement_inputs_with_sample_rate(self._discover_capture_inputs())
        selected = self._resolve_capture_input(inputs, input_id=input_id, input_key=input_key)
        if not selected.get("available"):
            raise ValueError("Selected capture input is not available")
        return str(selected["id"])


    def get_job(self, job_id: str) -> dict[str, Any]:
        job = self._jobs.get(job_id)
        if job is None:
            path = self.job_records_dir / f"{job_id}.json"
            if not path.exists():
                raise KeyError(job_id)
            job = json.loads(path.read_text(encoding="utf-8"))
            if str(job.get("status") or "") not in {"completed", "failed", "cancelled"}:
                task = self._job_tasks.get(job_id)
                if task is None or task.done():
                    self._promote_stale_job_to_terminal(job_id, job)
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
        self.get_job(job_id)
        live_job = self._jobs[job_id]
        cancelled = self._job_runner.cancel_job(job_id, live_job)
        if self._job_processes.get(job_id):
            logger.warning(
                "MEASUREMENT-CANCEL-DIAG subprocesses terminated: job_id=%s process_count=%d",
                job_id, len(self._job_processes[job_id]),
            )
        logger.warning(
            "MEASUREMENT-CANCEL-DIAG cancel requested: job_id=%s status=cancelling",
            job_id,
        )
        return cancelled

    def delete_measurement(self, measurement_id: str) -> None:
        measurement_id = str(measurement_id or "").strip()
        if not measurement_id:
            raise ValueError("Measurement id is required")
        path = self.measurements_dir / f"{measurement_id}.json"
        if not path.exists():
            raise KeyError(measurement_id)
        path.unlink()

    def has_active_measurement_job(self) -> bool:
        self._normalize_stale_jobs()
        return any(
            str(job.get("status") or "") in {"queued", "running", "cancelling"}
            for job in self._jobs.values()
        )

    async def shutdown(self) -> None:
        """Cancel and drain every measurement owned by this store."""
        self._shutdown = True
        active_job_ids = [
            job_id
            for job_id, job in self._jobs.items()
            if str(job.get("status") or "") in {"queued", "running", "cancelling"}
        ]
        await self._job_runner.shutdown(active_job_ids, self._jobs)

    def _start_job_process(self, job_id: str, command: list[str]) -> subprocess.Popen[str]:
        return self._job_runner.start_process(job_id, command)

    def _measurement_job_task_done(self, job_id: str, task: asyncio.Task[Any]) -> None:
        self._job_runner._task_done(job_id, task)

    def upload_calibration_file(self, filename: str, data: bytes) -> dict[str, Any]:
        return self._file_store.upload_calibration_file(filename, data)

    def get_calibration_state(self) -> dict[str, Any]:
        return self._file_store.get_calibration_state()

    def upload_house_curve_file(self, filename: str, data: bytes) -> dict[str, Any]:
        return self._file_store.upload_house_curve_file(filename, data)

    def delete_house_curve_file(self, house_curve_ref: str) -> dict[str, Any]:
        return self._file_store.delete_house_curve_file(house_curve_ref)

    def get_house_curve_state(self) -> dict[str, Any]:
        return self._file_store.get_house_curve_state()

    def get_calibration_file_for_export(self, calibration_ref: str) -> tuple[Path, str]:
        return self._file_store.get_calibration_file_for_export(calibration_ref)

    def get_house_curve_file_for_export(self, house_curve_ref: str) -> tuple[Path, str]:
        return self._file_store.get_house_curve_file_for_export(house_curve_ref)

    @staticmethod
    def _get_managed_file_for_export(root: Path, raw_ref: str, filename_builder) -> tuple[Path, str]:
        return MeasurementFileStore._get_managed_file_for_export(root, raw_ref, filename_builder)

    def set_active_calibration_file_id(self, calibration_ref: str | None) -> dict[str, Any]:
        return self._file_store.set_active_calibration_file_id(calibration_ref)

    def get_active_calibration_file_id(self, files: list[dict[str, Any]] | None = None) -> str:
        return self._file_store.get_active_calibration_file_id(files)

    def delete_calibration_file(self, calibration_ref: str) -> dict[str, Any]:
        return self._file_store.delete_calibration_file(calibration_ref)

    async def _run_measurement_job(self, job_id: str) -> None:
        job = self._jobs[job_id]
        executor = self._execute_lr_repeat_job if job.get("job_kind") == "lr-repeat" else self._execute_capture_job
        self._job_runner._raw_scope_enter = self.raw_scope_enter
        self._job_runner._raw_scope_exit = self.raw_scope_exit
        self._job_runner._effect_bypass_setter = self.effect_bypass_setter
        self._job_runner._active_scope_enter = self.active_scope_enter
        self._job_runner._active_scope_exit = self.active_scope_exit
        await self._job_runner.run(job_id, job, executor)

    def _execute_lr_repeat_job(self, job: dict[str, Any]) -> dict[str, Any]:
        return self._repeat_runner.execute(job)

    def _pre_average_er_captures(self, *args, **kwargs):
        return self._repeat_runner._pre_average_er_captures(*args, **kwargs)

    def summarize_repeat_measurements(self, *args, **kwargs):
        return self._repeat_runner.summarize_repeat_measurements(*args, **kwargs)

    def summarize_lr_repeat_paired(self, *args, **kwargs):
        return self._repeat_runner.summarize_lr_repeat_paired(*args, **kwargs)

    def _build_pre_averaged_lr_summary(self, *args, **kwargs):
        return self._repeat_runner._build_pre_averaged_lr_summary(*args, **kwargs)

    def _cleanup_lr_repeat_sweep_wavs(self, *args, **kwargs):
        return self._repeat_runner._cleanup_lr_repeat_sweep_wavs(*args, **kwargs)

    def _select_repeat_timing_cluster(self, *args, **kwargs):
        return self._repeat_runner._select_repeat_timing_cluster(*args, **kwargs)

    def _analyze_sweep_capture(self, *args, **kwargs):
        return self._analyzer.analyze_sweep_capture(*args, **kwargs)

    def _build_impulse_response_debug_segment(self, *args, **kwargs):
        return self._analyzer._build_impulse_response_debug_segment(*args, **kwargs)

    def _build_ir_preview(self, *args, **kwargs):
        return self._analyzer._build_ir_preview(*args, **kwargs)

    @staticmethod
    def _uses_electrical_reference_timing(*args, **kwargs):
        return MeasurementAnalyzer._uses_electrical_reference_timing(*args, **kwargs)

    @staticmethod
    def _hybrid_analysis_requirements(*args, **kwargs):
        return MeasurementAnalyzer._hybrid_analysis_requirements(*args, **kwargs)

    def _build_display_points(self, *args, **kwargs):
        return self._analyzer._build_display_points(*args, **kwargs)

    def _select_trusted_band(self, *args, **kwargs):
        return self._analyzer._select_trusted_band(*args, **kwargs)

    @staticmethod
    def _find_response_outliers(*args, **kwargs):
        return MeasurementAnalyzer._find_response_outliers(*args, **kwargs)

    @staticmethod
    def _edge_window_is_stable(*args, **kwargs):
        return MeasurementAnalyzer._edge_window_is_stable(*args, **kwargs)

    def _estimate_sweep_timing(self, *args, **kwargs):
        return self._analyzer._estimate_sweep_timing(*args, **kwargs)

    def _build_sweep_timing_anchors(self, *args, **kwargs):
        return self._analyzer._build_sweep_timing_anchors(*args, **kwargs)

    def _fit_sweep_timing_from_matches(self, *args, **kwargs):
        return self._analyzer._fit_sweep_timing_from_matches(*args, **kwargs)

    @staticmethod
    def _weighted_anchor_line_fit(*args, **kwargs):
        return MeasurementAnalyzer._weighted_anchor_line_fit(*args, **kwargs)

    @staticmethod
    def _sweep_timing_anchor_region(*args, **kwargs):
        return MeasurementAnalyzer._sweep_timing_anchor_region(*args, **kwargs)

    def _aggregate_anchor_region_score(self, *args, **kwargs):
        return self._analyzer._aggregate_anchor_region_score(*args, **kwargs)

    def _find_best_alignment_in_region(self, *args, **kwargs):
        return self._analyzer._find_best_alignment_in_region(*args, **kwargs)

    def _find_top_n_alignments_in_region(self, *args, **kwargs):
        return self._analyzer._find_top_n_alignments_in_region(*args, **kwargs)

    def _select_global_lag_from_peaks(self, *args, **kwargs):
        return self._analyzer._select_global_lag_from_peaks(*args, **kwargs)

    def _build_variable_window_response(self, *args, **kwargs):
        return self._analyzer._build_variable_window_response(*args, **kwargs)

    def _window_impulse_response(self, *args, **kwargs):
        return self._analyzer._window_impulse_response(*args, **kwargs)

    def _estimate_impulse_direct_arrival(self, *args, **kwargs):
        return self._analyzer._estimate_impulse_direct_arrival(*args, **kwargs)

    def _resample_signal(self, *args, **kwargs):
        return self._analyzer._resample_signal(*args, **kwargs)

    def _find_sweep_start(self, *args, **kwargs):
        return self._analyzer._find_sweep_start(*args, **kwargs)

    def _fft_correlate(self, *args, **kwargs):
        return self._analyzer._fft_correlate(*args, **kwargs)

    def _fft_convolve(self, *args, **kwargs):
        return self._analyzer._fft_convolve(*args, **kwargs)

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
        """Keep sweep timing decisions here while delegating pure generation."""
        return write_sweep_file(
            path,
            sample_rate=sample_rate,
            sweep_seconds=sweep_seconds,
            lead_in_seconds=lead_in_seconds,
            tail_seconds=tail_seconds,
            channel=channel,
            peak_scale=HOST_SWEEP_PEAK_SCALE,
            start_hz=start_hz,
            end_hz=end_hz,
        )

    @staticmethod
    def _compute_alignment_shift(*args, **kwargs):
        return MeasurementRepeatRunner._compute_alignment_shift(*args, **kwargs)

    @staticmethod
    def _shift_signal(*args, **kwargs):
        return MeasurementRepeatRunner._shift_signal(*args, **kwargs)

    @staticmethod
    def _er_pre_average_sample_spread_limit(*args, **kwargs):
        return MeasurementRepeatRunner._er_pre_average_sample_spread_limit(*args, **kwargs)

    def _extract_measurement_timing_ms(self, *args, **kwargs):
        return self._repeat_runner._extract_measurement_timing_ms(*args, **kwargs)

    def _select_paired_delta_cluster(self, *args, **kwargs):
        return self._repeat_runner._select_paired_delta_cluster(*args, **kwargs)

    def _build_repeat_side_summary(self, *args, **kwargs):
        return self._repeat_runner._build_repeat_side_summary(*args, **kwargs)

    @staticmethod
    def _write_stereo_wav(*args, **kwargs):
        return MeasurementRepeatRunner._write_stereo_wav(*args, **kwargs)

    @classmethod
    def _public_job_result(cls, value: Any) -> Any:
        """Remove private capture helpers before persisting or returning a job result."""
        if isinstance(value, dict):
            return {
                key: cls._public_job_result(item)
                for key, item in value.items()
                if not str(key).startswith("_")
            }
        if isinstance(value, list):
            return [cls._public_job_result(item) for item in value]
        if isinstance(value, tuple):
            return [cls._public_job_result(item) for item in value]
        return value

    @classmethod
    def _public_measurement_job_result(cls, value: Any) -> Any:
        """Remove temporary capture WAV paths from a published job result."""
        public = cls._public_job_result(value)
        if isinstance(public, dict):
            for section_name in ("capture", "playback"):
                section = public.get(section_name)
                if isinstance(section, dict):
                    for path_key in ("path", "reference_path"):
                        section.pop(path_key, None)
        return public


    @staticmethod
    def _default_measurement_sweep_profile() -> dict[str, float]:
        return {
            "sweep_start_hz": SWEEP_START_HZ,
            "sweep_end_hz": SWEEP_END_HZ,
            "sweep_seconds": SWEEP_V2_SECONDS,
            "lead_in_seconds": SWEEP_V2_LEAD_IN_SECONDS,
            "tail_seconds": SWEEP_V2_TAIL_SECONDS,
            "record_preroll_seconds": HOST_SWEEP_RECORD_PREROLL_SECONDS,
            "record_postroll_seconds": HOST_SWEEP_RECORD_POSTROLL_SECONDS,
        }

    def _execute_capture_job(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = str(job["id"])
        owner_job_id = str(job.get("_owner_job_id") or job_id)
        selected_input = job.get("input") or {}
        input_channels = job.get("input_channels") if isinstance(job.get("input_channels"), dict) else {}
        channel = str(job.get("channel") or "left")
        measurement_scope = self._normalize_measurement_scope(job.get("measurement_scope"))
        measurement_role = str(job.get("measurement_role") or "").strip().lower()
        calibration_meta = job.get("calibration") if isinstance(job.get("calibration"), dict) else {"filename": "", "applied": False}

        sample_rate = self._resolve_measurement_sample_rate()
        repeat_profile = job.get("capture_profile") == "lr-repeat"
        er_preavg_requested = (
            repeat_profile
            and input_channels.get("electrical_reference") is not None
            and input_channels.get("reference_disabled_reason", "") in ("", None)
        )
        # Default L/R Repeat intentionally uses the same full sweep profile as
        # Single Sweep. This keeps Acoustic-only Repeat, ER Repeat, and Single
        # Sweep directly comparable in low-frequency magnitude. A shorter
        # repeat should be a future explicit "Fast L/R Repeat" mode.
        sweep_profile = self._default_measurement_sweep_profile()
        custom = job.get("sweep_profile")
        if isinstance(custom, dict) and custom:
            for key in ("sweep_start_hz", "sweep_end_hz", "sweep_seconds", "lead_in_seconds",
                         "tail_seconds", "record_preroll_seconds", "record_postroll_seconds"):
                val = custom.get(key)
                if isinstance(val, (int, float)) and float(val) > 0:
                    sweep_profile[key] = float(val)
        sweep_start_hz = float(sweep_profile.get("sweep_start_hz", SWEEP_START_HZ))
        sweep_end_hz = float(sweep_profile.get("sweep_end_hz", SWEEP_END_HZ))
        sweep_seconds = float(sweep_profile["sweep_seconds"])
        lead_in_seconds = float(sweep_profile["lead_in_seconds"])
        tail_seconds = float(sweep_profile["tail_seconds"])
        record_preroll_seconds = float(sweep_profile["record_preroll_seconds"])
        record_postroll_seconds = float(sweep_profile["record_postroll_seconds"])
        duration_seconds = lead_in_seconds + sweep_seconds + tail_seconds
        record_duration_seconds = duration_seconds + record_preroll_seconds + record_postroll_seconds
        logger.debug(
            "Capture job %s: er_preavg=%s, sweep=%.2fs, lead=%.2fs, tail=%.2fs, "
            "preroll=%.2fs, postroll=%.2fs, record_dur=%.2fs",
            job_id, er_preavg_requested, sweep_seconds, lead_in_seconds,
            tail_seconds, record_preroll_seconds, record_postroll_seconds, record_duration_seconds,
        )
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
        if source_node_name.endswith(".monitor"):
            raise RuntimeError("Refusing to measure through a non-microphone source; select a real PipeWire input")

        playback_channel = channel
        playback_target = self._resolve_playback_target(measurement_scope=measurement_scope)
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
            start_hz=sweep_start_hz,
            end_hz=sweep_end_hz,
        )

        calibration_curve = None
        calibration_applied = False
        if calibration_meta.get("path"):
            calibration_curve = self._file_store.parse_calibration_file(Path(calibration_meta["path"]))
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

        # Cancel guard: check before any attempt
        if owner_job_id in self._cancelled_jobs:
            logger.warning(
                "MEASUREMENT-CANCEL-DIAG retry aborted before first attempt: job_id=%s",
                job_id,
            )
            raise RuntimeError("Measurement cancelled.")
        for attempt_index in range(HOST_SWEEP_MAX_ATTEMPTS):
            attempts_used = attempt_index + 1
            logger.info(
                "Measurement attempt %d/%d starting: job_id=%s reference=%s",
                attempts_used, HOST_SWEEP_MAX_ATTEMPTS, job_id,
                "electrical" if use_electrical_reference else "acoustic",
            )
            try:
                # Cancel guard: check before each capture attempt
                if owner_job_id in self._cancelled_jobs:
                    logger.warning(
                        "MEASUREMENT-CANCEL-DIAG retry aborted before capture attempt %d/%d: job_id=%s",
                        attempts_used, HOST_SWEEP_MAX_ATTEMPTS, job_id,
                    )
                    raise RuntimeError("Measurement cancelled.")

                if capture_path.exists():
                    capture_path.unlink()
                attempt_reference = electrical_reference if use_electrical_reference else host_reference
                analysis, capture_info, playback_info = self._run_host_capture_attempt(
                    job_id=job_id,
                    owner_job_id=owner_job_id,
                    mic_source_node_name=source_node_name,
                    reference_capture=attempt_reference,
                    channel=playback_channel,
                    capture_channels=capture_channels,
                    capture_path=capture_path,
                    playback_path=playback_path,
                    playback_target=playback_target,
                    measurement_scope=measurement_scope,
                    measurement_role=measurement_role,
                    playback_gain=job.get("playback_gain"),
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

                    # Cancel guard: check before QC-based ER fallback
                    if owner_job_id in self._cancelled_jobs:
                        logger.warning(
                            "MEASUREMENT-CANCEL-DIAG retry aborted before ER fallback: job_id=%s",
                            job_id,
                        )
                        raise RuntimeError("Measurement cancelled.")

                    if not reference_status["usable"]:
                        reference_warning = reference_status["warning"]
                        if self._should_keep_active_22_dsp_electrical_reference(
                            analysis,
                            measurement_scope=measurement_scope,
                        ):
                            self._mark_dsp_tolerated_electrical_reference_usable(
                                analysis,
                                warning=reference_warning,
                            )
                            logger.warning(
                                "Electrical measurement reference marginal for %s but kept for active 2.2 DSP path: %s",
                                job_id,
                                reference_warning,
                            )
                            reference_warning = ""
                        else:
                            logger.warning("Electrical measurement reference rejected for %s: %s", job_id, reference_warning)
                            logger.info(
                                "ER fallback capture within attempt %d/%d: reference quality rejected, retrying with host timing",
                                attempts_used, HOST_SWEEP_MAX_ATTEMPTS,
                            )
                            if capture_path.exists():
                                capture_path.unlink()
                            analysis, capture_info, playback_info = self._run_host_capture_attempt(
                                job_id=job_id,
                                owner_job_id=owner_job_id,
                                mic_source_node_name=source_node_name,
                                reference_capture=host_reference,
                                channel=playback_channel,
                                capture_channels=2,
                                capture_path=capture_path,
                                playback_path=playback_path,
                                playback_target=playback_target,
                                measurement_scope=measurement_scope,
                                measurement_role=measurement_role,
                                playback_gain=job.get("playback_gain"),
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
                mic_target = str(selected_input.get("node_serial") or source_node_name).strip()
                if self._try_raise_mic_for_low_capture(
                    analysis,
                    mic_target=mic_target,
                    attempt_index=attempt_index,
                    mic_auto_boosted=mic_auto_boosted,
                ):
                    mic_auto_boosted = True
                    time.sleep(HOST_SWEEP_RETRY_DELAY_SECONDS)
                    continue
                break
            except Exception as exc:
                # Cancel safety: cancellation is ALWAYS terminal
                if self._is_measurement_cancelled(owner_job_id, exc):
                    logger.warning(
                        "MEASUREMENT-CANCEL-DIAG retry aborted because job cancelled: job_id=%s attempt=%d/%d exc=%s",
                        job_id, attempts_used, HOST_SWEEP_MAX_ATTEMPTS, exc,
                    )
                    raise RuntimeError("Measurement cancelled.") from exc

                if use_electrical_reference:
                    reference_warning = f"Electrical reference unavailable; used host monitor timing fallback ({exc})."
                    logger.warning("Electrical measurement reference failed for %s; falling back to host monitor timing: %s", job_id, exc)
                    logger.info(
                        "ER fallback capture within attempt %d/%d: capture exception, retrying with host timing",
                        attempts_used, HOST_SWEEP_MAX_ATTEMPTS,
                    )

                    # Cancel guard: check before ER exception fallback
                    if owner_job_id in self._cancelled_jobs:
                        logger.warning(
                            "MEASUREMENT-CANCEL-DIAG retry aborted before ER exception fallback: job_id=%s",
                            job_id,
                        )
                        raise RuntimeError("Measurement cancelled.")

                    try:
                        if capture_path.exists():
                            capture_path.unlink()
                        analysis, capture_info, playback_info = self._run_host_capture_attempt(
                            job_id=job_id,
                            owner_job_id=owner_job_id,
                            mic_source_node_name=source_node_name,
                            reference_capture=host_reference,
                            channel=playback_channel,
                            capture_channels=2,
                            capture_path=capture_path,
                            playback_path=playback_path,
                            playback_target=playback_target,
                            measurement_scope=measurement_scope,
                            measurement_role=measurement_role,
                            playback_gain=job.get("playback_gain"),
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
                        mic_target = str(selected_input.get("node_serial") or source_node_name).strip()
                        if self._try_raise_mic_for_low_capture(
                            analysis,
                            mic_target=mic_target,
                            attempt_index=attempt_index,
                            mic_auto_boosted=mic_auto_boosted,
                        ):
                            mic_auto_boosted = True
                            time.sleep(HOST_SWEEP_RETRY_DELAY_SECONDS)
                            continue
                        break
                    except Exception:
                        logger.warning("Electrical reference fallback capture also failed for %s", job_id, exc_info=True)

                        # Cancel check after ER fallback also failed
                        if owner_job_id in self._cancelled_jobs:
                            logger.warning(
                                "MEASUREMENT-CANCEL-DIAG retry aborted after ER fallback failed (job cancelled): job_id=%s",
                                job_id,
                            )
                            raise RuntimeError("Measurement cancelled.")

                # Cancel guard before retry decision
                if owner_job_id in self._cancelled_jobs:
                    logger.warning(
                        "MEASUREMENT-CANCEL-DIAG retry aborted: job_id=%s",
                        job_id,
                    )
                    raise RuntimeError("Measurement cancelled.")

                if attempt_index >= HOST_SWEEP_MAX_ATTEMPTS - 1 or not self._should_retry_host_capture(exc):
                    raise RuntimeError(
                        f"Measurement failed after {attempts_used}/{HOST_SWEEP_MAX_ATTEMPTS} attempts: {exc}"
                    ) from exc

                # Cancel-aware sleep
                logger.info(
                    "MEASUREMENT-CANCEL-DIAG retry sleep: job_id=%s attempt=%d/%d delay=%.2f",
                    job_id, attempts_used, HOST_SWEEP_MAX_ATTEMPTS, HOST_SWEEP_RETRY_DELAY_SECONDS,
                )
                self._cancel_aware_sleep(owner_job_id, HOST_SWEEP_RETRY_DELAY_SECONDS)
                # Re-check after sleep before next loop iteration
                if owner_job_id in self._cancelled_jobs:
                    logger.warning(
                        "MEASUREMENT-CANCEL-DIAG retry aborted after sleep (job cancelled): job_id=%s",
                        job_id,
                    )
                    raise RuntimeError("Measurement cancelled.")
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
            measurement_role=str(job.get("measurement_role") or ""),
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
                "direct_response": analysis.get("direct_response"),
                "complex_response": analysis.get("complex_response"),
            },
            "limitations": [
                "This path is a real host-local sweep playback and capture flow, but it is still a conservative sweep-v3 implementation.",
                "Host-local timing now follows the separate sink-monitor reference capture and applies that offset/drift correction to the mic path; it is still not a full REW feature set.",
                "The displayed trace is intentionally trimmed to the conservative trusted band when the low or high edges remain unstable.",
                "Measurements stay separate from DSP presets and active PEQ state. No Auto-PEQ or Copy-to-PEQ is included here.",
            ],
            "message": completion_message,
            "scope_note": MEASUREMENT_SCOPE_NOTE,
            "measurement_scope": measurement_scope,
            "_capture_path": str(capture_path),
            "_playback_path": str(playback_path),
            "_sample_rate": sample_rate,
            "_mic_input_channel_index": mic_input_channel_index,
            "_electrical_reference_channel_index": electrical_reference_channel_index,
            "_use_electrical_reference": use_electrical_reference,
            "_calibration_curve": calibration_curve,
            "_sweep_seconds": sweep_seconds,
            "_lead_in_seconds": lead_in_seconds,
            "_tail_seconds": tail_seconds,
            "_record_preroll_seconds": record_preroll_seconds,
            "_record_postroll_seconds": record_postroll_seconds,
            "_record_duration_seconds": record_duration_seconds,
        }

    def _run_host_capture_attempt(
        self,
        *,
        job_id: str,
        owner_job_id: str,
        mic_source_node_name: str,
        reference_capture: dict[str, Any],
        channel: str,
        capture_channels: int,
        capture_path: Path,
        playback_path: Path,
        playback_target: dict[str, Any],
        measurement_scope: str,
        measurement_role: str,
        playback_gain: float | None,
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
        playback_route = self._build_measurement_playback_route(
            play_node_name,
            playback_target,
            measurement_scope=measurement_scope,
        )
        play_command = self._build_measurement_play_command(
            play_node_name=play_node_name,
            playback_path=playback_path,
            playback_target=playback_target,
            playback_route=playback_route,
            playback_gain=playback_gain,
        )

        record_process = self._start_job_process(owner_job_id, record_command)
        monitored_channel_index = self._recorded_mic_channel_index(
            mic_input_channel_index,
            has_electrical_reference=electrical_reference_channel_index is not None,
        )
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
        helper_process_snapshots: list[dict[str, Any]] = []
        detailed_diagnostics_enabled = _detailed_measurement_diagnostics_enabled()
        routing_snapshots: list[dict[str, Any]] = []
        link_diagnostics: dict[str, Any] = {}
        playback_route_diagnostics = self._new_measurement_playback_route_diagnostics(playback_route)
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
            self._cleanup_fxroute_links(
                source_node_name=mic_source_node_name,
                record_node_name=record_node_name,
            )
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
                    record_process=record_process,
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

            helper_process_snapshots.append(self._snapshot_fxroute_21_helper_processes("before-capture-start"))
            logger.info(
                "Measurement 2.1 helper pgrep before capture start: job_id=%s sample_rate=%s helper_processes=%s",
                job_id,
                sample_rate,
                helper_process_snapshots[-1].get("processes"),
            )

            # Pre-Sweep State Validation
            pre_sweep_state = self._build_pre_sweep_state_snapshot(
                job_id=job_id,
                sample_rate=sample_rate,
                playback_route=playback_route,
            )
            logger.warning(
                "MEASUREMENT-STATE-CHECK pre-sweep state: %s",
                json.dumps(pre_sweep_state, sort_keys=True, default=str),
            )
            pre_sweep_failure = pre_sweep_state.get("validation_failure")
            if pre_sweep_failure:
                raise RuntimeError(
                    f"Measurement pre-sweep state check failed: {pre_sweep_failure}. "
                    "Helper/config not in consistent state - sweep refused."
                )

            play_process = self._start_job_process(owner_job_id, play_command)
            if playback_route["route"] == "direct-sink":
                playback_route_diagnostics = self._link_measurement_playback_to_direct_sink(
                    play_node_name=play_node_name,
                    playback_target=playback_target,
                    playback_route=playback_route,
                )
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
            self._cleanup_measurement_playback_links(
                play_node_name=play_node_name,
                temporary_links=playback_route_diagnostics.get("temporary_playback_links", []),
            )
            self._cleanup_fxroute_links(
                source_node_name=mic_source_node_name,
                record_node_name=record_node_name,
            )
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

        if owner_job_id in self._cancelled_jobs:
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
        analysis_channel_index = self._recorded_mic_channel_index(
            mic_input_channel_index,
            has_electrical_reference=electrical_reference_channel_index is not None,
        )
        reference_channel_index = electrical_reference_channel_index if electrical_reference_channel_index is not None else 0
        self._update_measurement_job_message(owner_job_id, "Processing measurement…")
        try:
            is_21_active = any(
                bool(snap.get("processes")) for snap in helper_process_snapshots
            )
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
                is_21_dsp_active=is_21_active,
                measurement_role=measurement_role,
            )
        except Exception:
            helper_process_snapshots.append(self._snapshot_fxroute_21_helper_processes("after-capture-analysis-failure"))
            logger.exception(
                "Measurement 2.1 helper pgrep after capture analysis failure: job_id=%s sample_rate=%s helper_processes=%s",
                job_id,
                sample_rate,
                helper_process_snapshots[-1].get("processes"),
            )
            raise
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
            "measurement_scope": measurement_scope,
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
            "measurement_playback_route": playback_route_diagnostics.get("measurement_playback_route"),
            "temporary_playback_links": playback_route_diagnostics.get("temporary_playback_links", []),
            "play_node_helper_links": playback_route_diagnostics.get("play_node_helper_links", []),
            "direct_hardware_links_removed": playback_route_diagnostics.get("direct_hardware_links_removed", []),
            "direct_hardware_links_remaining": playback_route_diagnostics.get("direct_hardware_links_remaining", []),
            "play_node_links_after_manual_link": playback_route_diagnostics.get("play_node_links_after_manual_link", []),
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
            "fxroute_21_helper_processes": helper_process_snapshots,
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
            "Measurement summary: playback_route=%s playback_target=%s play_node=%s capture_source=%s reference_source=%s monitor=%s sample_rate=%s sweep_peak_dbfs=%s sweep_rms_dbfs=%s pipewire_warnings=%d detail=%s",
            routing_diagnostics["measurement_playback_route"],
            routing_diagnostics["playback_sink"]["target_name"],
            routing_diagnostics["play_node"],
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

    def _run_measurement_prime(
        self,
        *,
        prime_id: str,
        mic_source_node_name: str,
        channel: str,
        capture_channels: int,
        playback_path: Path,
        playback_target: dict[str, Any],
        sample_rate: int,
        sweep_seconds: float,
        lead_in_seconds: float,
        tail_seconds: float,
        record_preroll_seconds: float,
        record_postroll_seconds: float,
        record_duration_seconds: float,
    ) -> None:
        """Run a throwaway prime sweep after 2.1 DSP reconfig.

        Exercises the full play-through-helper → output → mic-capture
        pipeline so the first real capture starts with settled PipeWire
        buffer state and helper DSP history.
        """
        prime_capture = self.captures_dir / f"{prime_id}-prime.wav"
        record_node = f"fxroute-measure-record-{prime_id}"
        play_node = f"fxroute-measure-play-{prime_id}"
        sample_count = int(round(sample_rate * record_duration_seconds))

        record_cmd = [
            "pw-record",
            "-P", "node.autoconnect=false",
            "-P", f"node.name={record_node}",
            "--target", "0",
            "--rate", str(sample_rate),
            "--channels", str(capture_channels),
            "--format", "s16",
        ]
        if self._pw_record_supports_option("--container"):
            record_cmd.extend(["--container", "wav"])
        if self._pw_record_supports_option("--sample-count"):
            record_cmd.extend(["--sample-count", str(sample_count)])
        record_cmd.append(str(prime_capture))

        playback_route = self._build_measurement_playback_route(play_node, playback_target)
        play_cmd = self._build_measurement_play_command(
            play_node_name=play_node,
            playback_path=playback_path,
            playback_target=playback_target,
            playback_route=playback_route,
        )
        playback_route_diagnostics = self._new_measurement_playback_route_diagnostics(playback_route)

        logger.info(
            "Measurement prime sweep starting: prime_id=%s mic=%s channels=%d sample_rate=%d",
            prime_id, mic_source_node_name, capture_channels, sample_rate,
        )

        record_proc = subprocess.Popen(record_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self._cleanup_fxroute_links(
                source_node_name=mic_source_node_name,
                record_node_name=record_node,
            )
            self._link_host_reference_capture(
                reference_source_node_name=mic_source_node_name,
                mic_source_node_name=mic_source_node_name,
                record_node_name=record_node,
                requested_channel=channel,
                mic_input_channel_index=0,
                record_process=record_proc,
            )
            time.sleep(record_preroll_seconds)
            play_proc = subprocess.Popen(play_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if playback_route["route"] == "direct-sink":
                playback_route_diagnostics = self._link_measurement_playback_to_direct_sink(
                    play_node_name=play_node,
                    playback_target=playback_target,
                    playback_route=playback_route,
                )
            try:
                play_stdout, play_stderr = play_proc.communicate(
                    timeout=record_duration_seconds + PRIME_TIMEOUT_MARGIN_SECONDS
                )
            except subprocess.TimeoutExpired:
                logger.warning("Prime sweep pw-play timed out for %s, killing", prime_id)
                try:
                    play_proc.kill()
                except Exception:
                    pass
                play_proc.wait(timeout=5)
            record_proc.terminate()
            try:
                record_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("Prime sweep pw-record did not exit for %s, killing", prime_id)
                record_proc.kill()
                record_proc.wait(timeout=5)
        finally:
            try:
                record_proc.kill()
            except Exception:
                pass
            try:
                record_proc.wait(timeout=3)
            except Exception:
                pass
            try:
                prime_capture.unlink(missing_ok=True)
            except OSError:
                pass
            self._cleanup_measurement_playback_links(
                play_node_name=play_node,
                temporary_links=playback_route_diagnostics.get("temporary_playback_links", []),
            )
            self._cleanup_fxroute_links(
                source_node_name=mic_source_node_name,
                record_node_name=record_node,
            )

        logger.info(
            "Measurement prime sweep completed: prime_id=%s",
            prime_id,
        )

    def _is_measurement_cancelled(self, job_id: str, exc = None):
        """Return True if the job is cancelled or the exception is a cancellation signal."""
        if job_id in self._cancelled_jobs:
            return True
        if exc is None:
            return False
        if isinstance(exc, asyncio.CancelledError):
            return True
        msg = str(exc)
        if "Measurement cancelled" in msg or "Measurement canceled" in msg:
            return True
        return False

    def _cancel_aware_sleep(self, job_id: str, delay_seconds: float) -> None:
        """Sleep in short increments, aborting immediately if the job is cancelled."""
        check_interval = 0.05
        deadline = time.monotonic() + delay_seconds
        while time.monotonic() < deadline:
            if job_id in self._cancelled_jobs:
                return
            remaining = deadline - time.monotonic()
            time.sleep(min(check_interval, max(0.0, remaining)))

    def _find_active_or_cancelling_job(self):
        """Return any job that is still active (running/queued/cancelling)."""
        for _job_id, job in list(self._jobs.items()):
            status = str(job.get("status") or "")
            if status not in TERMINAL_JOB_STATUSES:
                return job
        return None

    def _normalize_stale_jobs(self) -> None:
        """Promote non-terminal jobs without a live worker to a terminal state.

        A job whose runner task is missing or already finished can never make
        further progress (e.g. a persisted record resurrected after a service
        restart, or a runner task that was lost).  Such jobs must not block
        future measurements forever; genuinely running jobs are untouched.
        """
        for job_id, job in list(self._jobs.items()):
            if str(job.get("status") or "") in TERMINAL_JOB_STATUSES:
                continue
            task = self._job_tasks.get(job_id)
            if task is not None and not task.done():
                continue
            self._promote_stale_job_to_terminal(job_id, job)

    def _promote_stale_job_to_terminal(self, job_id: str, job: dict[str, Any]) -> None:
        """Normalize a stale non-terminal job to cancelled and persist it."""
        if self._is_terminal_job_status(job.get("status")):
            return
        previous_status = str(job.get("status") or "")
        self._cancelled_jobs.add(job_id)
        job["status"] = "cancelled"
        job["updated_at"] = self._utc_now()
        job["message"] = "Measurement interrupted (no live worker)."
        job["result"] = None
        job["error"] = None
        self._persist_job(job)
        logger.warning(
            "MEASUREMENT-CANCEL-DIAG stale job without live worker promoted to terminal state: "
            "job_id=%s previous_status=%s",
            job_id, previous_status,
        )

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

    def _should_keep_active_22_dsp_electrical_reference(
        self,
        analysis: dict[str, Any],
        *,
        measurement_scope: str,
    ) -> bool:
        if self._normalize_measurement_scope(measurement_scope) != MEASUREMENT_SCOPE_ACTIVE_CHAIN:
            return False
        try:
            output_mode = get_audio_output_overview().get("output_mode") or {}
        except Exception:
            return False
        if str(output_mode.get("mode") or "") not in OUTPUT_MODE_SUBWOOFER_22_MODES:
            return False

        reference_path = analysis.get("reference_path") if isinstance(analysis.get("reference_path"), dict) else {}
        peak_dbfs = float(reference_path.get("peak_dbfs") or -120.0)
        clipped = bool(reference_path.get("clipped")) or peak_dbfs >= CAPTURE_CLIP_FAIL_DBFS
        if clipped or peak_dbfs < ELECTRICAL_REFERENCE_MIN_PEAK_DBFS:
            return False

        items = ((analysis.get("quality_checks") or {}).get("items") or [])
        if any(str(item.get("level") or "") == "error" for item in items):
            return False
        return self._analysis_has_warning_code(analysis, "soft-end-alignment-21")

    @staticmethod
    def _mark_dsp_tolerated_electrical_reference_usable(analysis: dict[str, Any], *, warning: str) -> None:
        reference_path = analysis.get("reference_path") if isinstance(analysis.get("reference_path"), dict) else {}
        clock = analysis.get("clock") if isinstance(analysis.get("clock"), dict) else {}
        alignment_score = min(float(clock.get("start_score") or 0.0), float(clock.get("end_score") or 0.0))
        sharpness_db = float(reference_path.get("ir_sharpness_db") or 0.0)
        reference_path["usable"] = True
        reference_path["electrical_reference_used"] = True
        reference_path["timing_status"] = "electrical-reference"
        reference_path["timing_label"] = "Electrical reference active"
        reference_path["confidence"] = round(min(alignment_score, max(0.0, sharpness_db / 60.0)), 6)
        reference_path["stability"] = "dsp-end-anchor-tolerated"
        if warning:
            reference_path["warning"] = warning.replace("; used host monitor timing fallback.", "")
        analysis["reference_path"] = reference_path

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

    def _format_capture_input_level_label(self, peak_dbfs: float, clipped: bool) -> str:
        if clipped:
            return "CLIP"
        if peak_dbfs <= CAPTURE_LEVEL_STATUS_MIN_DBFS:
            return "Peak < -90 dBFS"
        return f"Peak {round(peak_dbfs):.0f} dBFS"

    def _format_capture_input_level_message(self, peak_dbfs: float, clipped: bool) -> str:
        return f"Running sweep… {self._format_capture_input_level_label(peak_dbfs, clipped)}"

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

    def _update_capture_input_level_status(
        self,
        job_id: str,
        peak_dbfs: float,
        clipped: bool,
        *,
        channel_index: int | None = None,
    ) -> None:
        with self._job_process_lock:
            job = self._jobs.get(job_id)
            parent_repeat_job = None
            if not job:
                match = re.match(r"^(measurement-repeat-job-[0-9a-f]+)-repeat\d+-(left|right)$", str(job_id or ""))
                parent_repeat_job = self._jobs.get(match.group(1)) if match else None
                job = parent_repeat_job
            if not job or str(job.get("status") or "") != "running":
                return
            now = self._utc_now()
            if parent_repeat_job is None:
                job["message"] = self._format_capture_input_level_message(peak_dbfs, clipped)
            job["updated_at"] = now
            job["input_level"] = {
                "peak_dbfs": round(max(CAPTURE_LEVEL_STATUS_MIN_DBFS, peak_dbfs), 1),
                "clipped": bool(clipped),
                "channel_index": int(channel_index) if channel_index is not None else None,
                "channel_number": int(channel_index) + 1 if channel_index is not None else None,
                "updated_at": now,
            }
            try:
                self._persist_job(job)
            except Exception:
                logger.debug("Failed to persist measurement input level status for %s", job_id, exc_info=True)

    def _update_measurement_job_message(self, job_id: str, message: str) -> None:
        with self._job_process_lock:
            job = self._jobs.get(job_id)
            if not job or str(job.get("status") or "") != "running":
                return
            job["message"] = message
            job["updated_at"] = self._utc_now()
            try:
                self._persist_job(job)
            except Exception:
                logger.debug("Failed to persist measurement job message for %s", job_id, exc_info=True)

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
                            self._update_capture_input_level_status(
                                job_id,
                                peak_dbfs,
                                clipped,
                                channel_index=channel_index,
                            )
            except Exception:
                logger.debug("Measurement input level monitor failed for %s", job_id, exc_info=True)
            stop_event.wait(CAPTURE_LEVEL_STATUS_INTERVAL_SECONDS)

    def _analysis_has_warning_code(self, analysis: dict[str, Any] | None, code: str) -> bool:
        if not isinstance(analysis, dict):
            return False
        items = ((analysis.get("quality_checks") or {}).get("items") or [])
        return any(str(item.get("code") or "").strip() == code and item.get("level") == "warning" for item in items)

    @staticmethod
    def _recorded_mic_channel_index(
        mic_input_channel_index: int,
        *,
        has_electrical_reference: bool,
    ) -> int:
        # Host-reference capture is repacked as reference=FL, selected mic=FR.
        return int(mic_input_channel_index) if has_electrical_reference else 1

    def _try_raise_mic_for_low_capture(
        self,
        analysis: dict[str, Any] | None,
        *,
        mic_target: str,
        attempt_index: int,
        mic_auto_boosted: bool,
    ) -> bool:
        if (
            not self._analysis_has_warning_code(analysis, "capture-level-low")
            or mic_auto_boosted
            or attempt_index != HOST_SWEEP_AUTO_GAIN_RETRY_ATTEMPT - 1
            or not mic_target
        ):
            return False
        try:
            current_mic_volume = get_node_volume(mic_target)
        except SystemVolumeError:
            return False
        if not isinstance(current_mic_volume, int) or current_mic_volume >= HOST_SWEEP_AUTO_GAIN_TARGET_PERCENT:
            return False
        try:
            set_node_volume(mic_target, HOST_SWEEP_AUTO_GAIN_TARGET_PERCENT)
        except SystemVolumeError:
            return False
        return True


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

            self._prune_ir_debug_segments(output_dir)

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

    def _prune_ir_debug_segments(self, output_dir: Path) -> None:
        """Bound impulse-response debug segments to the newest segments.

        Segments are kept as {base}.json/{base}.csv pairs; the pair beyond
        the newest N segments is removed together.  Only the FXRoute-owned
        diagnostics directory is touched; failures are logged and never
        break measurement processing.  Symlinks are skipped (never statted
        or read), and a failed CSV removal leaves the pair intact so a
        later run can retry it instead of leaving an invisible orphan.
        """
        try:
            jsons = sorted(
                (path for path in output_dir.glob("*.json") if not path.is_symlink()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            logger.warning("Unable to list IR debug segments in %s", output_dir)
            return
        try:
            for json_path in jsons[IR_DEBUG_SEGMENT_RETENTION_SEGMENTS:]:
                csv_path = json_path.with_suffix(".csv")
                # A symlinked CSV is not an owned artifact: keep the whole
                # pair untouched and never unlink the link.
                if csv_path.is_symlink():
                    continue
                # Remove the CSV first: if its removal fails, the JSON is
                # still present and a later retention run retries the pair.
                try:
                    csv_path.unlink(missing_ok=True)
                except Exception:
                    logger.warning("Failed to prune IR debug segment CSV %s", csv_path)
                    continue
                try:
                    json_path.unlink(missing_ok=True)
                except Exception:
                    logger.warning("Failed to prune IR debug segment %s", json_path)
            # Orphan CSVs (their JSON pair is gone or was never written) are
            # garbage from a previously interrupted prune; remove them too.
            for csv_path in output_dir.glob("*.csv"):
                if csv_path.is_symlink():
                    continue
                json_partner = csv_path.with_suffix(".json")
                # A symlinked JSON partner is never statted or followed.
                if json_partner.is_symlink():
                    continue
                try:
                    orphan = not json_partner.exists()
                except OSError:
                    continue
                if not orphan:
                    continue
                try:
                    csv_path.unlink(missing_ok=True)
                except Exception:
                    logger.warning("Failed to prune orphan IR debug CSV %s", csv_path)
        except Exception:
            logger.exception("IR debug segment pruning failed")










    def _build_measurement_from_analysis(
        self,
        analysis: dict[str, Any],
        *,
        input_device: dict[str, str],
        channel: str,
        calibration: dict[str, Any],
        input_channels: dict[str, Any] | None = None,
        measurement_role: str = "",
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
        if measurement_role:
            payload["measurement_role"] = measurement_role
        if measurement_role == "direct" and analysis.get("direct_response") is not None:
            payload["analysis"]["direct_response"] = deepcopy(analysis["direct_response"])
        if measurement_role in {"direct", "mlp", "integration"} and analysis.get("complex_response") is not None:
            payload["analysis"]["complex_response"] = deepcopy(analysis["complex_response"])
        if impulse_response_debug:
            payload["analysis"]["impulse_response"]["debug_segment"] = impulse_response_debug
        return self._normalize_measurement(payload)

    @staticmethod
    def _normalize_measurement_scope(scope: Any) -> str:
        normalized = str(scope or MEASUREMENT_SCOPE_ACTIVE_CHAIN).strip().lower().replace("-", "_")
        if normalized not in MEASUREMENT_SCOPES:
            raise ValueError(f"measurement_scope must be one of: {', '.join(sorted(MEASUREMENT_SCOPES))}")
        return normalized

    def _resolve_playback_target(
        self,
        *,
        measurement_scope: str = MEASUREMENT_SCOPE_ACTIVE_CHAIN,
        overview: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        overview = overview or get_audio_output_overview()
        self._normalize_measurement_scope(measurement_scope)
        return self._resolve_active_chain_playback_target(overview)

    def _resolve_active_chain_playback_target(self, overview: dict[str, Any]) -> dict[str, Any]:
        target_name = "fxroute_dsp_sink"
        ports = self._list_pw_ports(target_name)
        if f"{target_name}:playback_FL" not in ports or f"{target_name}:playback_FR" not in ports:
            raise RuntimeError(
                "Active-chain measurement route unavailable: DSP input sink ports are missing "
                f"for {target_name} (ports={ports})"
            )
        current_output = overview.get("current_output") or {}
        selected_output = overview.get("selected_output") or {}
        default_output = overview.get("default_output") or {}
        return {
            "target_name": target_name,
            "target_label": f"Active listening chain ({target_name})",
            "active_rate": current_output.get("active_rate") or selected_output.get("active_rate") or default_output.get("active_rate"),
        }

    def _resolve_measurement_sample_rate(self) -> int:
        return MEASUREMENT_DEFAULT_SAMPLE_RATE

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
        rms_dbfs: float,
        trusted_band_meta: dict[str, Any],
        trusted_max_hz: float,
        response_outliers: list[dict[str, float | str]] | None = None,
        capture_label: str = "Capture",
        expect_dual_mono_channels: bool = True,
        is_21_dsp_active: bool = False,
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

        if peak_dbfs <= CAPTURE_LEVEL_LOW_PEAK_DBFS or rms_dbfs <= CAPTURE_LEVEL_LOW_RMS_DBFS:
            add(
                "warning",
                "capture-level-low",
                f"Microphone capture level was low (peak {peak_dbfs:.2f} dBFS, RMS {rms_dbfs:.2f} dBFS).",
            )
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
            if (
                is_21_dsp_active
                and start_score >= alignment_fail_threshold
                and (timing.get("selected_lag") or 0) > 0
                and not any(
                    item["code"] in {"weak-start-alignment", "capture-clipped"}
                    for item in items
                )
            ):
                logger.info(
                    "end_anchor_weak=true end_score=%.3f start_score=%.3f selected_lag=%s",
                    end_score,
                    start_score,
                    timing.get("selected_lag"),
                )
                add(
                    "warning",
                    "soft-end-alignment-21",
                    (
                        f"Sweep end alignment was weak ({end_score:.3f}) under active 2.1 DSP, "
                        + "tolerated because lag selection and start/mid anchors are stable."
                    ),
                )
            else:
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
        return self._file_store._store_calibration_file(filename, data)

    def _read_settings(self) -> dict[str, Any]:
        return self._file_store._read_settings()

    def _write_settings(self, settings: dict[str, Any]) -> None:
        self._file_store._write_settings(settings)

    def _resolve_calibration_meta(
        self,
        *,
        calibration_filename: str | None = None,
        calibration_bytes: bytes | None = None,
        calibration_ref: str | None = None,
    ) -> dict[str, Any] | None:
        return self._file_store.resolve_calibration_meta(
            calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes,
            calibration_ref=calibration_ref,
        )

    def _list_calibration_files(self) -> list[dict[str, Any]]:
        return self._file_store._list_calibration_files()

    def _lookup_calibration_file(self, calibration_ref: str) -> dict[str, Any] | None:
        return self._file_store._lookup_calibration_file(calibration_ref)

    def _list_house_curve_files(self) -> list[dict[str, Any]]:
        return self._file_store._list_house_curve_files()

    @staticmethod
    def _display_calibration_filename(value: str) -> str:
        return MeasurementFileStore._display_calibration_filename(value)

    @staticmethod
    def _display_house_curve_filename(value: str) -> str:
        return MeasurementFileStore._display_house_curve_filename(value)

    @staticmethod
    def _stored_house_curve_filename(value: str) -> str:
        return MeasurementFileStore._stored_house_curve_filename(value)

    @staticmethod
    def _parse_house_curve_bytes(data: bytes) -> list[list[float]]:
        return MeasurementFileStore._parse_house_curve_bytes(data)

    def _parse_calibration_file(self, path: Path) -> tuple[np.ndarray, np.ndarray] | None:
        return self._file_store.parse_calibration_file(path)

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
            if not node_name or node_name.endswith(".monitor"):
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
                    "node_description": details.get("node_description"),
                    "device_name": details.get("device_name"),
                    "device_description": details.get("device_description"),
                    "device_vendor_id": details.get("device_vendor_id"),
                    "device_product_id": details.get("device_product_id"),
                    "device_serial": details.get("device_serial"),
                    "alsa_card": details.get("alsa_card"),
                    "alsa_device": details.get("alsa_device"),
                    "alsa_card_name": details.get("alsa_card_name"),
                    "alsa_long_card_name": details.get("alsa_long_card_name"),
                    "capture_volume_percent": details.get("capture_volume_percent"),
                    "capture_gain_db": details.get("capture_gain_db"),
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
            if not node_name or node_name.endswith(".monitor"):
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
            match = re.match(r"\s*\*?\s*([A-Za-z0-9_.-]+)\s*=\s*\"([^\"]*)\"", line)
            if match:
                details[match.group(1).replace(".", "_")] = match.group(2)
        if str(details.get("audio_channels") or "").isdigit():
            details["channels"] = int(details["audio_channels"])
        if str(details.get("audio_rate") or "").isdigit():
            details["sample_rate"] = int(details["audio_rate"])
        details["node_name"] = details.get("node_name")

        device_id = str(details.get("device_id") or "").strip()
        if device_id:
            try:
                device = subprocess.run(
                    ["wpctl", "inspect", device_id],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
            except Exception:
                device = None
            if device and device.returncode == 0:
                for line in (device.stdout or "").splitlines():
                    match = re.match(r"\s*\*?\s*([A-Za-z0-9_.-]+)\s*=\s*\"([^\"]*)\"", line)
                    if match:
                        key = match.group(1).replace(".", "_")
                        details.setdefault(key, match.group(2))

        node_name = str(details.get("node_name") or "").strip()
        if node_name:
            try:
                volume = subprocess.run(
                    ["pactl", "get-source-volume", node_name],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
            except Exception:
                volume = None
            if volume and volume.returncode == 0:
                percent = re.search(r"/\s*([0-9.]+)%\s*/\s*([-+0-9.]+)\s*dB", volume.stdout or "")
                if percent:
                    details["capture_volume_percent"] = float(percent.group(1))
                    details["capture_gain_db"] = float(percent.group(2))
        return details

    def _link_host_reference_capture(
        self,
        *,
        reference_source_node_name: str,
        mic_source_node_name: str,
        record_node_name: str,
        requested_channel: str,
        mic_input_channel_index: int = 0,
        record_process: subprocess.Popen[str],
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
            returncode = record_process.poll()
            if returncode is not None:
                record_stdout, record_stderr = record_process.communicate()
                missing = [
                    name
                    for name, ports in (
                        ("reference_ports", reference_ports),
                        ("mic_ports", mic_ports),
                        ("record_inputs", record_inputs),
                    )
                    if not ports
                ]
                detail = (record_stderr or record_stdout or "no process output").strip()
                raise RuntimeError(
                    f"pw-record exited during PipeWire port discovery with returncode {returncode}: {detail}; "
                    f"missing port groups: {', '.join(missing) or 'none'}"
                )
            time.sleep(0.1)
        else:
            missing = [
                name
                for name, ports in (
                    ("reference_ports", reference_ports),
                    ("mic_ports", mic_ports),
                    ("record_inputs", record_inputs),
                )
                if not ports
            ]
            returncode = record_process.poll()
            process_status = "running" if returncode is None else f"exited with returncode {returncode}"
            process_detail = ""
            if returncode is not None:
                record_stdout, record_stderr = record_process.communicate()
                detail = (record_stderr or record_stdout or "no process output").strip()
                process_detail = f", output={detail}"
            raise RuntimeError(
                f"Unable to discover PipeWire ports for host-reference capture into {record_node_name}; "
                f"missing port groups: {', '.join(missing) or 'none'}; pw-record={process_status}{process_detail}; "
                f"reference_ports={reference_ports}; mic_ports={mic_ports}; record_inputs={record_inputs}"
            )

        reference_suffixes = [":monitor_FR", ":output_FR", ":capture_FR", ":capture_MONO", ":output_MONO", ":monitor_FL", ":output_FL", ":capture_FL"] if requested_channel == "right" else [":monitor_FL", ":output_FL", ":capture_FL", ":capture_MONO", ":output_MONO", ":monitor_FR", ":output_FR", ":capture_FR"]
        mic_suffixes = self._port_suffixes_for_channel_index(mic_input_channel_index) + [":capture_MONO", ":output_MONO"]
        reference_port = self._pick_port(reference_ports, reference_suffixes)
        mic_port = self._pick_port(mic_ports, mic_suffixes)
        input_left = self._pick_port(record_inputs, [":input_FL", ":input_MONO"])
        input_right = self._pick_port(record_inputs, [":input_FR", ":input_MONO", ":input_FL"])
        if not reference_port or not mic_port or not input_left or not input_right:
            raise RuntimeError("Could not resolve PipeWire ports for host-reference capture")

        self._cleanup_fxroute_links(
            source_node_name=mic_source_node_name,
            record_node_name=record_node_name,
        )
        link_errors: list[str] = []
        for src, dst, label in [
            (reference_port, input_left, "reference-to-record-left"),
            (mic_port, input_right, "microphone-to-record-right"),
        ]:
            try:
                subprocess.run(["pw-link", src, dst], capture_output=True, text=True, timeout=3, check=True)
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or "").strip()
                stdout = (exc.stdout or "").strip()
                if "already exists" in stderr.lower() or "already exists" in stdout.lower():
                    logger.info("Link already exists for %s (%s -> %s), skipping", label, src, dst)
                else:
                    link_errors.append(f"{label}: {stderr or stdout or exc}")
            except Exception as exc:
                link_errors.append(f"{label}: {exc}")
        if link_errors:
            logger.warning("Host-reference link issues: %s", link_errors)
            # If mic link failed, abort measurement — audio path would be incomplete
            if any("microphone-to-record" in err for err in link_errors):
                raise RuntimeError(
                    "Measurement audio path could not be prepared. Please retry."
                )
        time.sleep(0.15)
        result = {
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
        if link_errors and not any("microphone-to-record" in err for err in link_errors):
            result["link_warning"] = " ".join(link_errors)
        return result

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
            try:
                subprocess.run(["pw-link", source_port, input_port], capture_output=True, text=True, timeout=3, check=True)
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or "").strip()
                if "already exists" in stderr.lower():
                    logger.info("Link already exists (%s -> %s), skipping", source_port, input_port)
                else:
                    raise RuntimeError(
                        f"Could not create measurement audio link ({source_port} -> {input_port}): {stderr or exc}"
                    ) from exc

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
            link_error = None
            try:
                subprocess.run(["pw-link", source_port, input_port], capture_output=True, text=True, timeout=3, check=True)
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or "").strip()
                if "already exists" in stderr.lower():
                    logger.info("Link already exists for channel %d (%s -> %s), skipping", channel_index + 1, source_port, input_port)
                else:
                    link_error = stderr or str(exc)
            except Exception as exc:
                link_error = str(exc)
            if link_error is not None:
                raise RuntimeError(
                    f"Could not create measurement audio link for input channel {channel_index + 1}: {link_error}"
                )
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
        return self.audio_adapter.list_pw_ports(node_name)

    def _build_measurement_playback_route(
        self,
        play_node_name: str,
        playback_target: dict[str, Any],
        *,
        measurement_scope: str = MEASUREMENT_SCOPE_ACTIVE_CHAIN,
        overview: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        measurement_scope = self._normalize_measurement_scope(measurement_scope)
        overview = overview or get_audio_output_overview()
        output_mode = overview.get("output_mode") if isinstance(overview.get("output_mode"), dict) else {}
        mode = str(output_mode.get("mode") or "")
        return {
            "route": "direct-sink",
            "measurement_scope": measurement_scope,
            "output_mode": mode or "stereo",
            "play_node_name": play_node_name,
            "playback_target_name": str(playback_target.get("target_name") or ""),
            "expected_native_layout": [dict(channel) for channel in DSPRuntimeConfig.from_overview(overview).layout],
        }

    @staticmethod
    def _build_measurement_play_command(
        *,
        play_node_name: str,
        playback_path: Path,
        playback_target: dict[str, Any],
        playback_route: dict[str, Any],
        playback_gain: float | None = None,
    ) -> list[str]:
        # Measurement playback must not rely on pw-play --target
        # autoconnect: PipeWire resolves it non-deterministically (observed
        # live: the sweep node linked output_FL to the sink FR input and
        # output_FR to a DSP internal output port, producing a
        # silent/wrong sweep).  Mirror the subwoofer routes: disable
        # autoconnect and link the play node explicitly after its ports
        # exist.
        command = [
            "pw-play",
            "-P",
            "node.autoconnect=false",
            "-P",
            f"node.name={play_node_name}",
            "--target",
            "0",
            str(playback_path),
        ]
        if playback_route.get("measurement_scope") == MEASUREMENT_SCOPE_RAW_HELPER and playback_gain is not None:
            try:
                normalized_gain = float(playback_gain)
            except (TypeError, ValueError) as exc:
                raise ValueError("playback_gain must be a finite non-negative number") from exc
            if not math.isfinite(normalized_gain) or normalized_gain < 0.0:
                raise ValueError("playback_gain must be a finite non-negative number")
            command.insert(-1, f"--volume={normalized_gain:.9g}")
        return command

    @staticmethod
    def _new_measurement_playback_route_diagnostics(playback_route: dict[str, Any]) -> dict[str, Any]:
        return {
            "measurement_playback_route": playback_route.get("route") or "direct-sink",
            "measurement_scope": playback_route.get("measurement_scope") or MEASUREMENT_SCOPE_ACTIVE_CHAIN,
            "output_mode": playback_route.get("output_mode") or "",
            "temporary_playback_links": [],
            "play_node_helper_links": [],
            "active_chain_input_links": [],
            "active_chain_output_links": [],
            "direct_hardware_links_removed": [],
            "direct_hardware_links_remaining": [],
            "play_node_links_after_manual_link": [],
        }

    def _wait_for_measurement_play_ports(self, play_node_name: str) -> dict[str, str]:
        deadline = time.monotonic() + 4.0
        play_ports: list[str] = []
        while time.monotonic() < deadline:
            play_ports = self._list_pw_ports(play_node_name)
            output_l = f"{play_node_name}:output_FL"
            output_r = f"{play_node_name}:output_FR"
            if output_l in play_ports and output_r in play_ports:
                return {"left": output_l, "right": output_r}
            time.sleep(0.05)
        raise RuntimeError(
            "Subwoofer measurement playback route unavailable: measurement play FL/FR outputs missing "
            f"for {play_node_name} (ports={play_ports})"
        )

    def _link_measurement_playback_to_direct_sink(
        self,
        *,
        play_node_name: str,
        playback_target: dict[str, Any],
        playback_route: dict[str, Any],
    ) -> dict[str, Any]:
        """Deterministically link the sweep playback to the resolved sink.

        The direct-sink route previously relied on pw-play --target
        autoconnect, which PipeWire resolves non-deterministically (observed
        live: the sweep node linked output_FL to the sink FR input and
        output_FR to a DSP internal output port).  Mirror the
        subwoofer routes: disable autoconnect, wait for the play node ports
        and link explicitly to the resolved playback target.  With
        ACTIVE_CHAIN that target is fxroute_dsp_sink, so the sweep passes
        through the full FXRoute DSP gain chain.
        """
        diagnostics = self._new_measurement_playback_route_diagnostics(playback_route)
        play_ports = self._wait_for_measurement_play_ports(play_node_name)
        sink_name = str(playback_route.get("playback_target_name") or playback_target.get("target_name") or "").strip()
        sink_ports = self._list_pw_ports(sink_name)
        input_left = f"{sink_name}:playback_FL"
        input_right = f"{sink_name}:playback_FR"
        if input_left not in sink_ports or input_right not in sink_ports:
            raise RuntimeError(
                "Direct-sink measurement playback route unavailable: sink playback ports missing "
                f"for {sink_name} (ports={sink_ports})"
            )

        temporary_links = [
            {"source_port": play_ports["left"], "target_port": input_left, "role": "measurement-play-left-to-direct-sink"},
            {"source_port": play_ports["right"], "target_port": input_right, "role": "measurement-play-right-to-direct-sink"},
        ]
        created_links: list[dict[str, str]] = []
        try:
            for link in temporary_links:
                self._create_pipewire_link(str(link["source_port"]), str(link["target_port"]))
                created_links.append(link)
        except Exception:
            self._cleanup_measurement_playback_links(
                play_node_name=play_node_name,
                temporary_links=created_links,
            )
            raise

        diagnostics["temporary_playback_links"] = temporary_links
        diagnostics["active_chain_input_links"] = list(temporary_links)
        diagnostics["play_node_links_after_manual_link"] = self._list_relevant_pw_links([play_node_name, sink_name])
        logger.info(
            "Direct-sink measurement playback manually linked: play_node=%s input_links=%s",
            play_node_name,
            diagnostics["active_chain_input_links"],
        )
        return diagnostics

    def _create_pipewire_link(self, source_port: str, target_port: str) -> None:
        self.audio_adapter.create_pipewire_link(source_port, target_port)

    def _cleanup_measurement_playback_links(
        self,
        *,
        play_node_name: str,
        temporary_links: list[Any] | None = None,
    ) -> list[str]:
        removed: list[str] = []
        for link in temporary_links or []:
            if not isinstance(link, dict):
                continue
            source_port = str(link.get("source_port") or "")
            target_port = str(link.get("target_port") or "")
            if source_port and target_port and self._disconnect_link(source_port, target_port):
                removed.append(f"{source_port} -> {target_port}")

        try:
            completed = subprocess.run(["pw-link", "-l"], capture_output=True, text=True, timeout=3)
        except Exception:
            return removed
        if completed.returncode != 0:
            return removed
        for line in (completed.stdout or "").splitlines():
            line = line.strip()
            if "->" not in line or play_node_name not in line:
                continue
            left, right = line.split("->", 1)
            source_port = left.strip()
            target_port = right.strip()
            if "(id:" in target_port:
                target_port = target_port.partition("(id:")[0].strip()
            if not source_port or not target_port:
                continue
            if self._disconnect_link(source_port, target_port):
                removed.append(f"{source_port} -> {target_port}")
        if removed:
            logger.info("Cleaned up %d measurement playback link(s) for %s", len(removed), play_node_name)
            time.sleep(0.1)
        return removed

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
            "fxroute_dsp",
            "fxroute_dsp_sink",
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
        snapshot["fxroute_dsp_sink_inputs"] = [
            line
            for line in snapshot["links"]
            if "fxroute_dsp_sink:playback_" in line and ("|<-" in line or "|->" in line)
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

    def _build_pre_sweep_state_snapshot(
        self,
        *,
        job_id: str,
        sample_rate: int,
        playback_route: dict,
    ):
        """Capture comprehensive helper/config state before sweep.

        Returns a dict with validation_failure key set when the state is
        inconsistent and the sweep must be refused.

        Mode-aware: stereo/direct-sink routes do not require a 2.x helper.
        """
        route_name = str(playback_route.get("route") or "")
        output_mode = str(playback_route.get("output_mode") or "unknown")
        route_needs_helper = True

        now = time.monotonic()
        snapshot = {
            "job_id": job_id,
            "measurement_rate": sample_rate,
            "playback_route": route_name,
            "output_mode": output_mode,
            "helper_required": route_needs_helper,
            "timestamp": now,
            "validation_failure": None,
        }

        runtime = self.runtime_snapshot_provider() if callable(self.runtime_snapshot_provider) else {}
        snapshot["native_runtime"] = runtime
        config = runtime.get("config") if isinstance(runtime, dict) else None
        failures = []
        if not runtime.get("active"):
            failures.append("native DSP runtime is inactive")
        if not isinstance(config, dict):
            failures.append("native DSP runtime config is unavailable")
        else:
            if int(config.get("sample_rate") or 0) != sample_rate:
                failures.append(f"native DSP rate {config.get('sample_rate')} != measurement rate {sample_rate}")
            if str(config.get("output_mode") or "") != output_mode:
                failures.append(f"native DSP output mode {config.get('output_mode')} != measurement mode {output_mode}")
            expected_outputs = 4 if output_mode.startswith("subwoofer-2.") else 2
            runtime_layout = config.get("layout") or []
            expected_layout = playback_route.get("expected_native_layout") or []
            if len(runtime_layout) != expected_outputs:
                failures.append(f"native DSP layout does not expose {expected_outputs} outputs")
            if expected_layout and runtime_layout != expected_layout:
                failures.append("native DSP routing/crossover/alignment layout does not match measurement output mode")
        if playback_route.get("measurement_scope") == MEASUREMENT_SCOPE_RAW_HELPER and not runtime.get("effect_bypass"):
            failures.append("native DSP effects are not bypassed for raw-helper measurement")
        if failures:
            snapshot["validation_failure"] = "; ".join(failures)

        # Sink suspend/resume history
        try:
            last_suspend = subprocess.run(
                ["journalctl", "--user", "-u", "fxroute.service", "--no-pager",
                 "--since", "60 seconds ago", "-o", "cat"],
                capture_output=True, text=True, timeout=3,
            )
            suspend_lines = [l for l in (last_suspend.stdout or "").splitlines()
                           if "suspend" in l.lower() or "Suspend" in l]
            snapshot["recent_suspend_count"] = len(suspend_lines)
            if suspend_lines:
                snapshot["last_suspend_line"] = suspend_lines[-1][:300]
            else:
                snapshot["last_suspend_line"] = None
        except Exception as exc:
            snapshot["suspend_log_error"] = str(exc)

        # PipeWire rate check
        try:
            pw_rate = subprocess.run(
                ["pw-metadata", "-n", "settings", "0", "clock.force-rate"],
                capture_output=True, text=True, timeout=2,
            )
            pw_rate_val = (pw_rate.stdout or "").strip()
            snapshot["pipewire_force_rate"] = pw_rate_val
        except Exception as exc:
            snapshot["pipewire_force_rate"] = f"error: {exc}"

        # Pactl sink info
        try:
            pactl = subprocess.run(
                ["pactl", "list", "sinks", "short"],
                capture_output=True, text=True, timeout=3,
            )
            sink_lines = [l for l in (pactl.stdout or "").splitlines() if l.strip()]
            for line in sink_lines:
                if "RUNNING" in line or "IDLE" in line:
                    parts_line = line.split()
                    if len(parts_line) >= 2:
                        snapshot["pactl_sink_name"] = parts_line[1]
                        if len(parts_line) >= 5:
                            snapshot["pactl_sink_sample_rate"] = parts_line[4]
                    break
        except Exception as exc:
            snapshot["pactl_error"] = str(exc)

        # Bassgain-relevant: master volume / mute via pactl
        try:
            volume = subprocess.run(
                ["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
                capture_output=True, text=True, timeout=2,
            )
            vol_line = (volume.stdout or "").strip()
            snapshot["pactl_master_volume"] = vol_line[:200] if vol_line else None

            mute = subprocess.run(
                ["pactl", "get-sink-mute", "@DEFAULT_SINK@"],
                capture_output=True, text=True, timeout=2,
            )
            mute_line = (mute.stdout or "").strip()
            snapshot["pactl_master_mute"] = mute_line[:200] if mute_line else None
        except Exception as exc:
            snapshot["pactl_volume_error"] = str(exc)

        return snapshot
    @staticmethod
    def _snapshot_fxroute_21_helper_processes(label: str) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                ["pgrep", "-af", "native_dsp/build/fxroute-dsp"],
                capture_output=True,
                text=True,
                timeout=2,
            )
        except Exception as exc:
            return {"label": label, "error": str(exc), "processes": []}
        processes = [
            line.strip()
            for line in (completed.stdout or "").splitlines()
            if line.strip()
        ]
        return {
            "label": label,
            "returncode": completed.returncode,
            "processes": processes[:12],
        }

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

    def _pw_record_supports_option(self, option: str) -> bool:
        return self.audio_adapter.supports_option(option)

    def _disconnect_link(self, source_port: str, target_port: str) -> bool:
        """Remove a single pw-link. Returns True if removed or already gone."""
        return self.audio_adapter.disconnect_link(source_port, target_port)

    def _cleanup_fxroute_links(
        self,
        *,
        source_node_name: str,
        record_node_name: str,
    ) -> list[str]:
        """Remove any measurement links involving the given source/record nodes.
        Returns list of removed link descriptions for diagnostics."""
        removed: list[str] = []
        try:
            completed = subprocess.run(["pw-link", "-l"], capture_output=True, text=True, timeout=3)
        except Exception:
            return removed
        if completed.returncode != 0:
            return removed
        nodes_of_interest = {source_node_name, record_node_name}
        for node in list(nodes_of_interest):
            if node.endswith(".monitor"):
                nodes_of_interest.add(node[: -len(".monitor")])
            else:
                nodes_of_interest.add(f"{node}.monitor")
        for line in (completed.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("->")
            if len(parts) != 2:
                continue
            in_port = parts[0].strip()
            out_port = parts[1].strip()
            # strip optional (id: ...)
            link_id = None
            if "(id:" in out_port:
                out_port, link_id_part, _ = out_port.partition("(id:")
                out_port = out_port.strip()
                link_id = link_id_part.strip().rstrip(")").strip()
            in_node = in_port.rsplit(":", 1)[0] if ":" in in_port else ""
            out_node = out_port.rsplit(":", 1)[0] if ":" in out_port else ""
            if in_node not in nodes_of_interest and out_node not in nodes_of_interest:
                continue
            unlinked = False
            if link_id and link_id.isdigit():
                try:
                    subprocess.run(
                        ["pw-link", "-d", link_id],
                        capture_output=True, text=True, timeout=3,
                    )
                    unlinked = True
                except Exception:
                    pass
            if not unlinked:
                self._disconnect_link(out_port, in_port)
            removed.append(f"{out_port} -> {in_port}")
        if removed:
            logger.info("Cleaned up %d stale fxroute link(s)", len(removed))
            time.sleep(0.1)
        return removed

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
        if payload.get("measurement_role"):
            role = str(payload.get("measurement_role") or "").strip().lower()
            if role in {"direct", "mlp", "secondary", "integration", "hybrid-model"}:
                result["measurement_role"] = role
        if payload.get("notes"):
            result["notes"] = [str(item) for item in payload.get("notes") if str(item).strip()]
        if payload.get("analysis") and isinstance(payload.get("analysis"), dict):
            result["analysis"] = payload["analysis"]
        if payload.get("audio_output_context") and isinstance(payload.get("audio_output_context"), dict):
            result["audio_output_context"] = payload["audio_output_context"]
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

    @staticmethod
    def _is_terminal_job_status(status: Any) -> bool:
        return str(status or "") in TERMINAL_JOB_STATUSES

    def _cleanup_job_wav_files(self, job_id: str) -> None:
        """Delete the temporary single-sweep capture/playback WAVs of a job.

        The files are only consumed while the capture worker runs; the public
        job result strips their paths (_public_job_result), so nothing reads
        them after the job reaches a terminal state.  L/R repeat sweeps use
        per-sweep file names and are cleaned by _cleanup_lr_repeat_sweep_wavs.
        """
        for path in (
            self.captures_dir / f"{job_id}.wav",
            self.playbacks_dir / f"{job_id}.wav",
        ):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                logger.warning("Failed to remove measurement temporary WAV %s", path)

    @staticmethod
    def _parse_job_timestamp(value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            # Naive timestamps are never produced by this store; treat them
            # as UTC so the age comparison below never raises.
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    def _retain_job_history(self) -> None:
        """Bound runtime job history: drop terminal job records that are
        older than the retention window from memory and from the jobs dir.

        Only FXRoute-owned runtime records in jobs_dir are touched.  Active
        jobs, saved measurements, calibrations and house curves are never
        removed, and no files outside the jobs directory are deleted.
        Symlinks are never followed, and malformed records/timestamps are
        skipped conservatively.  A failure here is logged and must never
        break job finalization or a measurement start.
        """
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=JOB_RECORD_RETENTION_DAYS)
            with self._job_process_lock:
                stale_ids: set[str] = set()
                for job_id, job in list(self._jobs.items()):
                    if str(job.get("status") or "") not in TERMINAL_JOB_STATUSES:
                        continue
                    updated = self._parse_job_timestamp(job.get("updated_at"))
                    if updated is None or updated >= cutoff:
                        continue
                    stale_ids.add(job_id)
                # Records resurrected from disk after a restart are not in
                # _jobs; scan the owned records dir for them as well.
                for path in self.job_records_dir.glob("*.json"):
                    job_id = path.stem
                    if job_id in self._jobs:
                        continue
                    if path.is_symlink():
                        continue
                    try:
                        record = json.loads(path.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    if str(record.get("status") or "") not in TERMINAL_JOB_STATUSES:
                        continue
                    updated = self._parse_job_timestamp(record.get("updated_at"))
                    if updated is None or updated >= cutoff:
                        continue
                    stale_ids.add(job_id)
                for job_id in stale_ids:
                    self._jobs.pop(job_id, None)
                    self._job_tasks.pop(job_id, None)
                    self._cancelled_jobs.discard(job_id)
            for job_id in stale_ids:
                try:
                    (self.job_records_dir / f"{job_id}.json").unlink(missing_ok=True)
                except Exception:
                    logger.warning("Failed to remove retained-out measurement job record %s", job_id)
        except Exception:
            logger.exception("Measurement job history retention failed")

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
        return self._file_store._safe_filename(value)

    def _utc_now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Auto Sub Optimize — scoring helper
# ---------------------------------------------------------------------------

def score_sub_alignment_candidates(
    candidates: list[dict[str, Any]],
    crossover_hz: int,
    low_guard_reference_points: list[list[float]] | None = None,
    low_guard_reference_delay_ms: float | None = None,
) -> dict[str, Any]:
    """Score a list of measured delay candidates around a crossover frequency.

    Each candidate dict must contain:
        delay_ms: float
        points: list of [freq_hz, db_spl]
        name (optional): str

    Returns dict with keys:
        winner: dict — best candidate
        runner_up: dict — second-best candidate
        results: list[dict] — all scored candidates (delay_ms asc)
        confidence: str — "clear" | "close" | "uncertain"
        crossover_hz: int
    """
    if not candidates:
        raise ValueError("No candidates to score")
    if len(candidates) < 2:
        return {
            "winner": candidates[0],
            "runner_up": None,
            "results": candidates,
            "confidence": "uncertain",
            "crossover_hz": crossover_hz,
        }

    fc = float(crossover_hz)

    def _band_metrics(points, fmin, fmax):
        band = [(f, db) for f, db in points if fmin <= f < fmax]
        if not band:
            return {"mean": 0.0, "min": 0.0, "max": 0.0, "swing": 0.0, "roughness": 0.0, "p20": 0.0}
        dbs = [p[1] for p in band]
        mean = sum(dbs) / len(dbs)
        mn = min(dbs)
        mx = max(dbs)
        sorted_dbs = sorted(dbs)
        p20_index = min(len(sorted_dbs) - 1, max(0, int(round((len(sorted_dbs) - 1) * 0.20))))
        p20 = sorted_dbs[p20_index]
        swing = mx - mn
        if len(dbs) > 1:
            diffs = [abs(dbs[i + 1] - dbs[i]) for i in range(len(dbs) - 1)]
            rough = sum(diffs) / len(diffs)
        else:
            rough = 0.0
        return {"mean": mean, "min": mn, "max": mx, "swing": swing, "roughness": rough, "p20": p20}

    def _low_guard_penalty(loss_db: float) -> float:
        """Softly penalize new low-bass dips below crossover without vetoing a candidate."""
        if loss_db <= 4.0:
            return 0.0
        if loss_db <= 6.0:
            return (loss_db - 4.0) * 0.03
        if loss_db <= 8.0:
            return 0.06 + ((loss_db - 6.0) * 0.06)
        return min(0.45, 0.18 + ((loss_db - 8.0) * 0.08))

    primary = []
    secondary = []
    low_guard = []
    low_guard_min_hz = fc * 0.35
    low_guard_max_hz = fc * 0.75
    for c in candidates:
        pts = c.get("points") or []
        pri = _band_metrics(pts, fc * 0.5, fc * 2.0)
        sec = _band_metrics(pts, fc * 0.75, fc * 1.5)
        low = _band_metrics(pts, low_guard_min_hz, low_guard_max_hz)
        primary.append(pri)
        secondary.append(sec)
        low_guard.append(low)

    reference_low_guard = None
    low_guard_reference = "best_low_guard"
    if low_guard_reference_points:
        reference_low_guard = _band_metrics(low_guard_reference_points, low_guard_min_hz, low_guard_max_hz)
        low_guard_reference = "provided_points"
    elif low_guard_reference_delay_ms is not None:
        try:
            reference_delay = float(low_guard_reference_delay_ms)
            reference_index = min(
                range(len(candidates)),
                key=lambda index: abs(float(candidates[index].get("delay_ms", 0.0) or 0.0) - reference_delay),
            )
            reference_low_guard = low_guard[reference_index]
            reference_name = candidates[reference_index].get("low_guard_reference_label") or candidates[reference_index].get("name")
            low_guard_reference = str(reference_name or f"delay_ms={reference_delay:.2f}")
        except (TypeError, ValueError):
            reference_low_guard = None
    else:
        explicit_reference_index = next(
            (index for index, candidate in enumerate(candidates) if bool(candidate.get("low_guard_reference"))),
            None,
        )
        if explicit_reference_index is not None:
            reference_low_guard = low_guard[explicit_reference_index]
            reference_name = (
                candidates[explicit_reference_index].get("low_guard_reference_label")
                or candidates[explicit_reference_index].get("name")
            )
            low_guard_reference = str(reference_name or "explicit_candidate")
        else:
            zero_index = min(
                range(len(candidates)),
                key=lambda index: abs(float(candidates[index].get("delay_ms", 0.0) or 0.0)),
            )
            if abs(float(candidates[zero_index].get("delay_ms", 0.0) or 0.0)) <= 0.05:
                reference_low_guard = low_guard[zero_index]
                reference_name = candidates[zero_index].get("low_guard_reference_label") or candidates[zero_index].get("name")
                low_guard_reference = str(reference_name or "zero_delay")

    if reference_low_guard is None:
        reference_index = max(range(len(low_guard)), key=lambda index: low_guard[index]["p20"])
        reference_low_guard = low_guard[reference_index]
        reference_name = candidates[reference_index].get("low_guard_reference_label") or candidates[reference_index].get("name")
        low_guard_reference = str(reference_name or "best_low_guard")

    def _norm(vals, higher_better):
        vals = list(vals)
        mn = min(vals)
        mx = max(vals)
        if mx == mn:
            return [0.5] * len(vals)
        if higher_better:
            return [(v - mn) / (mx - mn) for v in vals]
        return [(mx - v) / (mx - mn) for v in vals]

    # Weights: primary band 60 %, secondary 40 %
    # Within each band: mean 40 %, dip severity 25 %, swing 20 %, roughness 15 %
    n_pri_mean = _norm([p["mean"] for p in primary], True)
    n_pri_dip = _norm([p["mean"] - p["min"] for p in primary], False)
    n_pri_swing = _norm([p["swing"] for p in primary], False)
    n_pri_rough = _norm([p["roughness"] for p in primary], False)

    n_sec_mean = _norm([p["mean"] for p in secondary], True)
    n_sec_dip = _norm([p["mean"] - p["min"] for p in secondary], False)
    n_sec_swing = _norm([p["swing"] for p in secondary], False)
    n_sec_rough = _norm([p["roughness"] for p in secondary], False)

    results: list[dict[str, Any]] = []
    for i, c in enumerate(candidates):
        pri = primary[i]
        sec = secondary[i]

        # --- hard penalty for deep notches ---
        dip_severity = pri["mean"] - pri["min"]
        deep_notch_penalty = 0.0
        if dip_severity > 15.0:
            deep_notch_penalty = 0.5
        elif dip_severity > 10.0:
            deep_notch_penalty = 0.3
        elif dip_severity > 7.0:
            deep_notch_penalty = 0.15

        score_pri = (
            n_pri_mean[i] * 0.40
            + n_pri_dip[i] * 0.25
            + n_pri_swing[i] * 0.20
            + n_pri_rough[i] * 0.15
        )
        score_sec = (
            n_sec_mean[i] * 0.40
            + n_sec_dip[i] * 0.25
            + n_sec_swing[i] * 0.20
            + n_sec_rough[i] * 0.15
        )
        timing_band_score = (score_pri * 0.60 + score_sec * 0.40)
        low_guard_loss_db = max(0.0, float(reference_low_guard["p20"]) - float(low_guard[i]["p20"]))
        low_guard_penalty = _low_guard_penalty(low_guard_loss_db)
        score = max(0.0, timing_band_score * (1.0 - deep_notch_penalty) - low_guard_penalty)

        results.append({
            "delay_ms": c["delay_ms"],
            "name": c.get("name", str(c["delay_ms"])),
            "score": round(score, 4),
            "score_pct": round(score * 100, 1),
            "xo_score": round(score_sec, 4),
            "timing_band_score": round(timing_band_score, 4),
            "low_guard_loss_db": round(low_guard_loss_db, 2),
            "low_guard_penalty": round(low_guard_penalty, 4),
            "final_score": round(score, 4),
            "low_guard_min_hz": round(low_guard_min_hz, 1),
            "low_guard_max_hz": round(low_guard_max_hz, 1),
            "low_guard_p20_db": round(low_guard[i]["p20"], 1),
            "low_guard_reference_p20_db": round(reference_low_guard["p20"], 1),
            "low_guard_reference": low_guard_reference,
            "mean_primary_db": round(pri["mean"], 1),
            "min_primary_db": round(pri["min"], 1),
            "swing_primary_db": round(pri["swing"], 1),
            "dip_severity_db": round(pri["mean"] - pri["min"], 1),
            "roughness_primary": round(pri["roughness"], 3),
            "mean_secondary_db": round(sec["mean"], 1),
            "min_secondary_db": round(sec["min"], 1),
            "swing_secondary_db": round(sec["swing"], 1),
            "deep_notch_penalty": deep_notch_penalty,
        })

    # Sort by score descending
    results.sort(key=lambda r: r["score"], reverse=True)
    winner = results[0]
    runner_up = results[1] if len(results) > 1 else None

    # Confidence
    if runner_up and winner["score"] > 0:
        margin = (winner["score"] - runner_up["score"]) / winner["score"]
    else:
        margin = 1.0

    if margin > 0.15:
        confidence = "clear"
    elif margin > 0.05:
        confidence = "close"
    else:
        confidence = "uncertain"

    return {
        "winner": winner,
        "runner_up": runner_up,
        "results": results,
        "confidence": confidence,
        "crossover_hz": crossover_hz,
    }

def normalize_measurement_optional_input_channel(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        channel = int(str(value).strip())
    except (TypeError, ValueError):
        return ""
    return str(channel) if channel >= 1 else ""


def measurement_input_persistent_id(input_item: dict[str, Any]) -> str:
    device_serial = str(input_item.get("device_serial") or "").strip()
    node_name = str(input_item.get("node_name") or "").strip()
    if device_serial:
        node_suffix = f"|node-name:{node_name}" if node_name else ""
        return f"device-serial:{device_serial}{node_suffix}"
    if node_name:
        return f"node-name:{node_name}"
    hardware_parts = [
        str(input_item.get(key) or "").strip()
        for key in (
            "device_vendor_id", "device_product_id", "alsa_long_card_name",
            "alsa_card_name", "alsa_device",
        )
    ]
    hardware_parts = [part for part in hardware_parts if part]
    return f"hardware:{'|'.join(hardware_parts)}" if hardware_parts else ""


def resolve_measurement_input_selection(
    inputs: list[dict[str, Any]],
    measure_settings: dict[str, Any],
) -> dict[str, Any]:
    persistent_id = str(measure_settings.get("selectedInputKey") or "").strip()
    legacy_id = str(measure_settings.get("selectedInputId") or "").strip()
    configured = bool(persistent_id or legacy_id)
    selected = None
    if persistent_id:
        matches = [
            item for item in inputs
            if str(item.get("persistent_id") or measurement_input_persistent_id(item)) == persistent_id
        ]
        selected = matches[0] if len(matches) == 1 else None
    elif legacy_id:
        selected = next((item for item in inputs if str(item.get("id") or "") == legacy_id), None)
    elif inputs:
        selected = inputs[0]

    resolved_persistent_id = (
        str(selected.get("persistent_id") or measurement_input_persistent_id(selected))
        if selected else persistent_id
    )
    return {
        "configured": configured,
        "input_id": str(selected.get("id") or "") if selected else "",
        "persistent_id": resolved_persistent_id,
        "unavailable": configured and selected is None,
        "legacy_input_id": legacy_id,
    }


def measurement_setup_settings_from_payload(settings: dict[str, Any]) -> dict[str, Any]:
    measure_settings = settings.get("measure") if isinstance(settings.get("measure"), dict) else {}
    reference_input_channel = measure_settings.get("selectedReferenceInputChannel")
    if reference_input_channel is None:
        reference_input_channel = measure_settings.get("reference_input_channel")
    return {
        "selectedInputId": str(measure_settings.get("selectedInputId") or ""),
        "selectedInputKey": str(measure_settings.get("selectedInputKey") or ""),
        "selectedInputConfigured": bool(
            measure_settings.get("selectedInputKey") or measure_settings.get("selectedInputId")
        ),
        "selectedMicInputChannel": normalize_measurement_optional_input_channel(
            measure_settings.get("selectedMicInputChannel")
        ) or "1",
        "selectedReferenceInputChannel": normalize_measurement_optional_input_channel(reference_input_channel),
    }
