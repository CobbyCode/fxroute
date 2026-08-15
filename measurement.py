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

from measurement_audio import MeasurementAudioAdapter
from measurement_file_store import MeasurementFileStore
from measurement_host_capture import HostCaptureRunner
from measurement_capture_policy import MeasurementCapturePolicyRunner
from measurement_persistence import MeasurementPersistence
from measurement_routing import MeasurementRouting
from measurement_signal import write_sweep_file
from measurement_job_runner import MeasurementJobRunner
from measurement_repeat_runner import MeasurementRepeatRunner
from measurement_analyzer import MeasurementAnalyzer
from measurement_constants import (
    CAPTURE_CLIP_FAIL_DBFS,
    IR_DEBUG_SEGMENT_RETENTION_SEGMENTS,
    JOB_RECORD_RETENTION_DAYS,
    MEASUREMENT_SCOPE_ACTIVE_CHAIN,
    MEASUREMENT_SCOPE_NOTE,
    MEASUREMENT_SCOPE_RAW_HELPER,
    MEASUREMENT_SCOPES,
    SWEEP_END_HZ,
    SWEEP_START_HZ,
    SWEEP_V2_LEAD_IN_SECONDS,
    SWEEP_V2_SECONDS,
    SWEEP_V2_TAIL_SECONDS,
    TERMINAL_JOB_STATUSES,
)
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

MEASUREMENT_DEFAULT_SAMPLE_RATE = 48_000

LR_REPEAT_SWEEP_SECONDS = 6.0
LR_REPEAT_LEAD_IN_SECONDS = 0.2
LR_REPEAT_TAIL_SECONDS = 0.75
LR_REPEAT_RECORD_PREROLL_SECONDS = 0.3
LR_REPEAT_RECORD_POSTROLL_SECONDS = 0.35
HOST_SWEEP_PEAK_SCALE = 0.8
SWEEP_TIMING_RESIDUAL_TOLERANCE_SECONDS = 0.04
IR_DIRECT_CONFIDENCE_FALLBACK_THRESHOLD = 0.30
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
        self._persistence = MeasurementPersistence(self)
        self._job_runner = MeasurementJobRunner(
            get_job=lambda job_id: self._jobs[job_id],
            persist_job=self._persistence._persist_job,
            public_result=self._public_measurement_job_result,
            cleanup_job=self._cleanup_job_wav_files,
            retain_history=self._persistence._retain_job_history,
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
        self._host_capture_runner = HostCaptureRunner(self)
        self._capture_policy = MeasurementCapturePolicyRunner(
            capture_attempt=self._run_capture_policy_attempt,
            is_cancelled=self._is_measurement_cancelled,
            evaluate_electrical_reference=self._evaluate_electrical_reference_status,
            should_keep_electrical_reference=(
                lambda analysis, scope: self._should_keep_active_22_dsp_electrical_reference(
                    analysis,
                    measurement_scope=scope,
                )
            ),
            mark_electrical_reference_usable=self._mark_dsp_tolerated_electrical_reference_usable,
            append_reference_fallback_warning=self._append_reference_fallback_warning,
            analysis_has_warning=self._analysis_has_warning_code,
            try_raise_mic=self._try_raise_mic_for_low_capture,
            cancel_aware_sleep=self._cancel_aware_sleep,
            retry_sleep=time.sleep,
            should_retry_host_capture=self._should_retry_host_capture,
            max_attempts=HOST_SWEEP_MAX_ATTEMPTS,
            retry_delay=HOST_SWEEP_RETRY_DELAY_SECONDS,
        )
        self._routing_command_runner = lambda *args, **kwargs: subprocess.run(*args, **kwargs)
        self._routing_output_overview = lambda: get_audio_output_overview()
        self._routing = MeasurementRouting(self)
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
        return self._persistence.list_measurements()

    def save_measurement(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._persistence.save_measurement(payload)

    def save_measurements(self, payloads: list[Any]) -> list[dict[str, Any]]:
        return self._persistence.save_measurements(payloads)

    def merge_measurements(self, measurement_ids: list[Any], name: str = "") -> dict[str, Any]:
        return self._persistence.merge_measurements(measurement_ids, name)

    def delete_measurement(self, measurement_id: str) -> None:
        self._persistence.delete_measurement(measurement_id)

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

    async def _prepare_measurement_job_setup(
        self,
        *,
        input_id: str,
        input_key: str,
        mic_input_channel: str | int | None,
        reference_input_channel: str | int | None,
        calibration_filename: str | None,
        calibration_bytes: bytes | None,
        calibration_ref: str | None,
        measurement_scope: str,
        job_prefix: str,
        channel: str | None = None,
    ) -> dict[str, Any]:
        if self._shutdown:
            raise RuntimeError("Measurement store is shutting down")
        # Normalize stale jobs before checking the single-job ownership guard.
        self._normalize_stale_jobs()
        active_job = self._find_active_or_cancelling_job()
        if active_job is not None:
            active_id = active_job["id"]
            active_status = active_job.get("status", "unknown")
            logger.warning(
                "MEASUREMENT-CANCEL-DIAG new job blocked: existing_job=%s status=%s",
                active_id,
                active_status,
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

        normalized_channel = None
        if channel is not None:
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
        now = self._utc_now()
        job_id = f"{job_prefix}{uuid4().hex[:12]}"
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
            "calibration": calibration_meta or {"filename": "", "applied": False},
            "scope_note": MEASUREMENT_SCOPE_NOTE,
            "measurement_scope": normalized_scope,
            "result": None,
            "error": None,
        }
        return {"job": job, "channel": normalized_channel}

    def _register_measurement_job(
        self,
        job: dict[str, Any],
        executor: Callable[[dict[str, Any]], Any],
    ) -> dict[str, Any]:
        job_id = str(job["id"])
        self._job_runner._raw_scope_enter = self.raw_scope_enter
        self._job_runner._raw_scope_exit = self.raw_scope_exit
        self._job_runner._effect_bypass_setter = self.effect_bypass_setter
        self._job_runner._active_scope_enter = self.active_scope_enter
        self._job_runner._active_scope_exit = self.active_scope_exit
        self._jobs[job_id] = job
        self._persistence._persist_job(job)
        self._job_runner.start(job_id, job, executor)
        return self.get_job(job_id)

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
        setup = await self._prepare_measurement_job_setup(
            input_id=input_id,
            input_key=input_key,
            mic_input_channel=mic_input_channel,
            reference_input_channel=reference_input_channel,
            calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes,
            calibration_ref=calibration_ref,
            measurement_scope=measurement_scope,
            job_prefix="measurement-job-",
            channel=channel,
        )
        job = setup["job"]
        normalized_channel = setup["channel"]
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
        job.update({
            "channel": normalized_channel,
            "message": "Sweep queued.",
            "playback_gain": normalized_playback_gain,
            "measurement_role": normalized_role,
            "sweep_profile": sweep_profile if isinstance(sweep_profile, dict) and sweep_profile else None,
        })
        return self._register_measurement_job(job, self._execute_capture_job)

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
        setup = await self._prepare_measurement_job_setup(
            input_id=input_id,
            input_key=input_key,
            mic_input_channel=mic_input_channel,
            reference_input_channel=reference_input_channel,
            calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes,
            calibration_ref=calibration_ref,
            measurement_scope=measurement_scope,
            job_prefix="measurement-repeat-job-",
        )
        job = setup["job"]
        now = job["created_at"]
        job.update({
            "job_kind": "lr-repeat",
            "repeat_count": normalized_repeat_count,
            "base_name": str(base_name or "").strip() or f"L/R Repeat {now[:19].replace('T', ' ')}",
            "channel": "stereo",
            "message": "L/R repeat queued.",
        })
        return self._register_measurement_job(job, self._execute_lr_repeat_job)

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
        playback_target = self._routing._resolve_playback_target(measurement_scope=measurement_scope)
        host_reference = self._routing._resolve_host_reference_capture(
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

        reference_warning = str(input_channels.get("reference_disabled_reason") or "").strip()
        mic_target = str(selected_input.get("node_serial") or source_node_name).strip()
        policy_result = self._capture_policy.run(
            job_id=job_id,
            owner_job_id=owner_job_id,
            use_electrical_reference=use_electrical_reference,
            electrical_reference=electrical_reference,
            host_reference=host_reference,
            capture_channels=capture_channels,
            electrical_reference_channel_index=(
                electrical_reference_channel_index if use_electrical_reference else None
            ),
            mic_target=mic_target,
            measurement_scope=measurement_scope,
            reference_warning=reference_warning,
            capture_kwargs={
                "job_id": job_id,
                "owner_job_id": owner_job_id,
                "mic_source_node_name": source_node_name,
                "channel": playback_channel,
                "capture_path": capture_path,
                "playback_path": playback_path,
                "playback_target": playback_target,
                "measurement_scope": measurement_scope,
                "measurement_role": measurement_role,
                "playback_gain": job.get("playback_gain"),
                "sweep_meta": sweep_meta,
                "sample_rate": sample_rate,
                "duration_seconds": duration_seconds,
                "sweep_seconds": sweep_seconds,
                "lead_in_seconds": lead_in_seconds,
                "tail_seconds": tail_seconds,
                "record_preroll_seconds": record_preroll_seconds,
                "record_postroll_seconds": record_postroll_seconds,
                "record_duration_seconds": record_duration_seconds,
                "calibration_curve": calibration_curve,
                "mic_input_channel_index": mic_input_channel_index,
            },
        )
        analysis = policy_result.analysis
        capture_info = policy_result.capture_info
        playback_info = policy_result.playback_info
        attempts_used = policy_result.attempts_used
        final_capture_level_low = policy_result.final_capture_level_low
        mic_auto_boosted = policy_result.mic_auto_boosted
        reference_warning = policy_result.reference_warning
        if analysis is None or capture_info is None or playback_info is None:
            raise RuntimeError("Host-local capture did not produce an analysis result")
        if reference_warning and not self._analysis_has_warning_code(analysis, "electrical-reference-fallback"):
            self._append_reference_fallback_warning(analysis, reference_warning)

        measurement = self._persistence._build_measurement_from_analysis(
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

    def _run_capture_policy_attempt(
        self,
        *,
        capture_path: Path,
        reference_capture: dict[str, Any],
        capture_channels: int,
        electrical_reference_channel_index: int | None,
        **kwargs,
    ):
        if capture_path.exists():
            capture_path.unlink()
        return self._host_capture_runner.execute(
            reference_capture=reference_capture,
            capture_channels=capture_channels,
            electrical_reference_channel_index=electrical_reference_channel_index,
            capture_path=capture_path,
            **kwargs,
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

        playback_route = self._routing._build_measurement_playback_route(play_node, playback_target)
        play_cmd = self._routing._build_measurement_play_command(
            play_node_name=play_node,
            playback_path=playback_path,
            playback_target=playback_target,
            playback_route=playback_route,
        )
        playback_route_diagnostics = self._routing._new_measurement_playback_route_diagnostics(playback_route)

        logger.info(
            "Measurement prime sweep starting: prime_id=%s mic=%s channels=%d sample_rate=%d",
            prime_id, mic_source_node_name, capture_channels, sample_rate,
        )

        record_proc = subprocess.Popen(record_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self._routing._cleanup_fxroute_links(
                source_node_name=mic_source_node_name,
                record_node_name=record_node,
            )
            self._routing._link_host_reference_capture(
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
                playback_route_diagnostics = self._routing._link_measurement_playback_to_direct_sink(
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
            self._routing._cleanup_measurement_playback_links(
                play_node_name=play_node,
                temporary_links=playback_route_diagnostics.get("temporary_playback_links", []),
            )
            self._routing._cleanup_fxroute_links(
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
        self._persistence._persist_job(job)
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
                self._persistence._persist_job(job)
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
                self._persistence._persist_job(job)
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
        return self._routing._resolve_playback_target(measurement_scope=measurement_scope, overview=overview)

    def _build_measurement_playback_route(
        self,
        play_node_name: str,
        playback_target: dict[str, Any],
        *,
        measurement_scope: str = MEASUREMENT_SCOPE_ACTIVE_CHAIN,
        overview: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._routing._build_measurement_playback_route(
            play_node_name,
            playback_target,
            measurement_scope=measurement_scope,
            overview=overview,
        )

    def _resolve_measurement_sample_rate(self) -> int:
        return MEASUREMENT_DEFAULT_SAMPLE_RATE

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

    def _list_pw_ports(self, node_name: str) -> list[str]:
        return self.audio_adapter.list_pw_ports(node_name)

    def _create_pipewire_link(self, source_port: str, target_port: str) -> None:
        self.audio_adapter.create_pipewire_link(source_port, target_port)

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

    def _pw_record_supports_option(self, option: str) -> bool:
        return self.audio_adapter.supports_option(option)

    def _disconnect_link(self, source_port: str, target_port: str) -> bool:
        """Remove a single pw-link. Returns True if removed or already gone."""
        return self.audio_adapter.disconnect_link(source_port, target_port)

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

    def _log_spaced_frequencies(self, start_hz: float, end_hz: float, count: int) -> list[float]:
        if count <= 1:
            return [start_hz]
        start_log = math.log10(start_hz)
        end_log = math.log10(end_hz)
        step = (end_log - start_log) / (count - 1)
        return [round(10 ** (start_log + step * index), 3) for index in range(count)]

    def _next_pow2(self, value: int) -> int:
        return 1 << max(1, int(value - 1)).bit_length()

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
