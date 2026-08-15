"""Managed calibration and house-curve file storage for measurements."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import numpy as np


class MeasurementFileStore:
    """Own calibration and house-curve files and their settings references."""

    def __init__(self, jobs_dir: Path, *, has_active_job: Callable[[], bool]):
        self.jobs_dir = Path(jobs_dir)
        self.calibrations_dir = self.jobs_dir / "calibrations"
        self.house_curves_dir = self.jobs_dir / "house_curves"
        self.settings_path = self.jobs_dir / "settings.json"
        self._has_active_job = has_active_job
        for directory in (self.calibrations_dir, self.house_curves_dir):
            directory.mkdir(parents=True, exist_ok=True)

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

    def upload_house_curve_file(self, filename: str, data: bytes) -> dict[str, Any]:
        if not data:
            raise ValueError("House curve file is empty")
        points = self._parse_house_curve_bytes(data)
        safe_name = self._safe_filename(filename or "house-curve.txt")
        target_path = self.house_curves_dir / f"{uuid4().hex[:10]}-{safe_name}"
        target_path.write_bytes(data)
        return {
            "status": "ok",
            "house_curves": self._list_house_curve_files(),
            "uploaded_house_curve_id": target_path.name,
            "points": points,
        }

    def delete_house_curve_file(self, house_curve_ref: str) -> dict[str, Any]:
        ref = Path(str(house_curve_ref or "")).name.strip()
        if not ref:
            raise ValueError("House curve file id is required")
        path = self.house_curves_dir / ref
        if not path.exists() or not path.is_file():
            raise KeyError(ref)
        path.unlink()
        return {"status": "ok", "house_curves": self._list_house_curve_files()}

    def get_house_curve_state(self) -> dict[str, Any]:
        return {"status": "ok", "house_curves": self._list_house_curve_files()}

    def get_calibration_file_for_export(self, calibration_ref: str) -> tuple[Path, str]:
        return self._get_managed_file_for_export(
            self.calibrations_dir, calibration_ref, self._display_calibration_filename
        )

    def get_house_curve_file_for_export(self, house_curve_ref: str) -> tuple[Path, str]:
        return self._get_managed_file_for_export(
            self.house_curves_dir, house_curve_ref, self._stored_house_curve_filename
        )

    @staticmethod
    def _get_managed_file_for_export(root: Path, raw_ref: str, filename_builder) -> tuple[Path, str]:
        raw_value = str(raw_ref or "").strip()
        ref = Path(raw_value)
        if not raw_value or ref.name != raw_value:
            raise ValueError("Managed file id is required")
        root_path = root.resolve()
        path = (root_path / raw_value).resolve()
        if path.parent != root_path or not path.is_file():
            raise KeyError(raw_value)
        return path, filename_builder(path.name)

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
        self.set_active_calibration_file_id("")
        return ""

    def delete_calibration_file(self, calibration_ref: str) -> dict[str, Any]:
        if self._has_active_job():
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

    def _store_calibration_file(self, filename: str, data: bytes) -> dict[str, Any]:
        safe_name = self._safe_filename(filename or "calibration.txt")
        target_path = self.calibrations_dir / f"{uuid4().hex[:10]}-{safe_name}"
        target_path.write_bytes(data)
        return {"id": target_path.name, "filename": safe_name, "path": str(target_path), "applied": False}

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

    def resolve_calibration_meta(
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
            entries.append({
                "id": path.name,
                "filename": display_name,
                "path": str(path),
                "modified_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
            })
        return entries

    def _lookup_calibration_file(self, calibration_ref: str) -> dict[str, Any] | None:
        ref = Path(str(calibration_ref or "")).name.strip()
        if not ref:
            return None
        path = self.calibrations_dir / ref
        if not path.exists() or not path.is_file():
            return None
        return {"id": path.name, "filename": self._display_calibration_filename(path.name), "path": str(path), "applied": False}

    def _list_house_curve_files(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        seen_filenames: set[str] = set()
        for path in sorted(self.house_curves_dir.glob("*"), key=lambda item: item.stat().st_mtime, reverse=True):
            if not path.is_file():
                continue
            display_name = self._display_house_curve_filename(path.name)
            if display_name in seen_filenames:
                continue
            try:
                points = self._parse_house_curve_bytes(path.read_bytes())
            except Exception:
                continue
            seen_filenames.add(display_name)
            entries.append({
                "id": path.name,
                "filename": display_name,
                "path": str(path),
                "points": points,
                "modified_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
            })
        return entries

    @staticmethod
    def _display_calibration_filename(value: str) -> str:
        name = Path(value or "").name
        return re.sub(r"^[0-9a-f]{10}-", "", name, count=1) or name or "calibration.txt"

    @staticmethod
    def _display_house_curve_filename(value: str) -> str:
        name = Path(value or "").name
        display_name = re.sub(r"^[0-9a-f]{10}-", "", name, count=1) or name or "house-curve.txt"
        return Path(display_name).stem or display_name

    @staticmethod
    def _stored_house_curve_filename(value: str) -> str:
        name = Path(value or "").name
        return re.sub(r"^[0-9a-f]{10}-", "", name, count=1) or name or "house-curve.txt"

    @staticmethod
    def _parse_house_curve_bytes(data: bytes) -> list[list[float]]:
        text = data.decode("utf-8", errors="ignore")
        points: list[list[float]] = []
        previous_frequency = 0.0
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or not re.match(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)", line):
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
            if frequency <= previous_frequency:
                raise ValueError("House curve frequencies must be strictly increasing")
            previous_frequency = frequency
            points.append([frequency, offset])
        if len(points) < 2:
            raise ValueError("House curve needs at least two frequency / dB pairs")
        return points

    def parse_calibration_file(self, path: Path) -> tuple[np.ndarray, np.ndarray] | None:
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

    @staticmethod
    def _safe_filename(value: str) -> str:
        name = Path(value).name.strip() or "calibration.txt"
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
        return cleaned or "calibration.txt"
