"""Persistence and saved-result ownership for measurements."""

from __future__ import annotations

import csv
import json
import logging
import math
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np

from measurement.constants import (
    DISPLAY_DEFAULTS,
    IR_DEBUG_SEGMENT_RETENTION_SEGMENTS,
    JOB_RECORD_RETENTION_DAYS,
    MEASUREMENT_SCOPE_NOTE,
    TERMINAL_JOB_STATUSES,
    TRACE_COLORS,
)

logger = logging.getLogger(__name__)


class MeasurementPersistence:
    """Own saved measurements, result normalization, and retention."""

    def __init__(self, store):
        self._store = store

    def list_measurements(self) -> dict[str, Any]:
        measurements = []
        for path in sorted(self._store.measurements_dir.glob("*.json"), reverse=True):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                measurements.append(self._normalize_measurement(payload, source_path=path))
            except Exception:
                continue
        measurements.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        return {
            "status": "ok",
            "storage": {
                "directory": str(self._store.measurements_dir),
                "jobs_directory": str(self._store.jobs_dir),
            },
            "calibrations": self._store._list_calibration_files(),
            "active_calibration_file_id": self._store.get_active_calibration_file_id(),
            "house_curves": self._store._list_house_curve_files(),
            "scope_note": MEASUREMENT_SCOPE_NOTE,
            "measurements": measurements,
        }

    def save_measurement(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize_measurement(payload)
        measurement_id = normalized["id"]
        path = self._store.measurements_dir / f"{measurement_id}.json"
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
        paths = [self._store.measurements_dir / f"{measurement_id}.json" for measurement_id in measurement_ids]
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
            path = self._store.measurements_dir / f"{measurement_id}.json"
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

    def delete_measurement(self, measurement_id: str) -> None:
        measurement_id = str(measurement_id or "").strip()
        if not measurement_id:
            raise ValueError("Measurement id is required")
        path = self._store.measurements_dir / f"{measurement_id}.json"
        if not path.exists():
            raise KeyError(measurement_id)
        path.unlink()

    def _save_impulse_response_debug_segment(
        self,
        measurement_id: str,
        debug_segment: Any,
    ) -> dict[str, Any] | None:
        if not isinstance(debug_segment, dict):
            return None
        try:
            output_dir = self._store.diagnostics_dir / "impulse-ir"
            output_dir.mkdir(parents=True, exist_ok=True)
            channel = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(debug_segment.get("channel") or "channel")).strip("-") or "channel"
            base_name = f"{measurement_id}-{channel}-ir-segment"
            json_path = output_dir / f"{base_name}.json"
            csv_path = output_dir / f"{base_name}.csv"
            payload = deepcopy(debug_segment)
            payload["measurement_id"] = measurement_id
            payload["generated_at"] = self._store._utc_now()
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

    def _normalize_measurement(self, payload: dict[str, Any], source_path: Path | None = None) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("Measurement payload must be an object")

        now = self._store._utc_now()
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
            with self._store._job_process_lock:
                stale_ids: set[str] = set()
                for job_id, job in list(self._store._jobs.items()):
                    if str(job.get("status") or "") not in TERMINAL_JOB_STATUSES:
                        continue
                    updated = self._parse_job_timestamp(job.get("updated_at"))
                    if updated is None or updated >= cutoff:
                        continue
                    stale_ids.add(job_id)
                # Records resurrected from disk after a restart are not in
                # _jobs; scan the owned records dir for them as well.
                for path in self._store.job_records_dir.glob("*.json"):
                    job_id = path.stem
                    if job_id in self._store._jobs:
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
                    self._store._jobs.pop(job_id, None)
                    self._store._job_tasks.pop(job_id, None)
                    self._store._cancelled_jobs.discard(job_id)
            for job_id in stale_ids:
                try:
                    (self._store.job_records_dir / f"{job_id}.json").unlink(missing_ok=True)
                except Exception:
                    logger.warning("Failed to remove retained-out measurement job record %s", job_id)
        except Exception:
            logger.exception("Measurement job history retention failed")

    def _persist_job(self, job: dict[str, Any]) -> None:
        path = self._store.job_records_dir / f"{job['id']}.json"
        path.write_text(json.dumps(job, indent=2) + "\n", encoding="utf-8")

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
