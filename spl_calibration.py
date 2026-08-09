# SPDX-License-Identifier: AGPL-3.0-only

"""SPL calibration: UMIK profiles, C-weighted capture, calibration noise."""

import asyncio
import logging
import math
import re
import subprocess
import tempfile
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request

from samplerate import get_audio_output_overview

logger = logging.getLogger(__name__)
SPL_STOP_TIMEOUT_SECONDS = 40.0


@dataclass
class _SplCalibrationOperation:
    id: str
    kind: str
    session_job_id: str
    noise_process: subprocess.Popen[Any] | None = None
    noise_file: Path | None = None
    recorder: subprocess.Popen[Any] | None = None
    restore_state: dict[str, Any] | None = None
    capture_path: Path | None = None
    source_node: str = ""
    source_volume_percent: float | None = None
    session: Any = None
    registration_attempted: bool = False
    cancel_requested: bool = False
    worker_task: asyncio.Task[Any] | None = None
    cleanup_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    completed: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True)
class SplCalibrationDependencies:
    get_measurement_store: Callable[[], Any]
    get_measurement_session: Callable[[], Any]
    get_easyeffects_manager: Callable[[], Any]
    require_easyeffects_manager: Callable[[], Any]
    get_output_volume: Callable[[], float]
    set_output_volume: Callable[[float], None]
    read_measurement_settings: Callable[[], dict[str, Any]]
    measurement_entry_preflight: Callable[[int], Any]


@dataclass
class _SplCalibrationRuntime:
    dependencies: SplCalibrationDependencies | None = None
    operation: _SplCalibrationOperation | None = None
    operation_lock: asyncio.Lock | None = None


_runtime = _SplCalibrationRuntime()


def configure_runtime(dependencies: SplCalibrationDependencies) -> None:
    _runtime.dependencies = dependencies


def _dependencies() -> SplCalibrationDependencies:
    if _runtime.dependencies is None:
        raise RuntimeError("SPL calibration runtime is not configured")
    return _runtime.dependencies

router = APIRouter()
def _spl_output_profile() -> dict[str, str]:
    overview = get_audio_output_overview()
    selected = overview.get("selected_output") if isinstance(overview, dict) else {}
    selected = selected if isinstance(selected, dict) else {}
    key = str(selected.get("key") or selected.get("target") or "default")
    label = str(selected.get("target_label") or selected.get("label") or key)
    return {"id": key, "label": label}


class _Umik1Profile:
    model = "UMIK-1"
    vendor_id = "2752"
    product_id = "0007"
    internal_gain_db = 18
    reference_capture_gain_db = 0.0
    reference_capture_volume_percent = 100.0

    @staticmethod
    def parse_calibration_header(path: Path) -> dict[str, Any]:
        try:
            first_lines = "\n".join(path.read_text(encoding="utf-8", errors="ignore").splitlines()[:8])
        except Exception:
            return {}
        sensitivity = re.search(
            r"Sens\s*Factor\s*=\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*dB",
            first_lines,
            flags=re.IGNORECASE,
        )
        serial = re.search(r"SERNO\s*:\s*([0-9][0-9 -]*)", first_lines, flags=re.IGNORECASE)
        return {
            "sensitivity_factor_db": float(sensitivity.group(1)) if sensitivity else None,
            "serial_number": re.sub(r"\D", "", serial.group(1)) if serial else "",
        }

    @classmethod
    def calibration_reference(cls, path: Path, filename: str) -> dict[str, Any]:
        header = cls.parse_calibration_header(path)
        sensitivity = header.get("sensitivity_factor_db")
        serial_number = str(header.get("serial_number") or "")
        filename_serial = "".join(re.findall(r"\d", Path(filename).stem))
        return {
            "sensitivity_factor_db": sensitivity,
            "serial_number": serial_number,
            "serial_matches_filename": bool(
                serial_number and filename_serial and serial_number == filename_serial
            ),
        }

    @classmethod
    def matches_input(cls, item: dict[str, Any]) -> bool:
        vendor = str(item.get("device_vendor_id") or "").lower().replace("0x", "").zfill(4)
        product = str(item.get("device_product_id") or "").lower().replace("0x", "").zfill(4)
        metadata = " ".join(
            str(item.get(key) or "")
            for key in (
                "node_name", "node_description", "device_name", "device_description",
                "alsa_card_name", "alsa_long_card_name",
            )
        ).lower()
        return vendor == cls.vendor_id and product == cls.product_id and "umik-1" in metadata

    @classmethod
    def has_reference_internal_gain(cls, item: dict[str, Any]) -> bool:
        metadata = " ".join(
            str(item.get(key) or "")
            for key in ("node_description", "device_description", "alsa_card_name", "alsa_long_card_name")
        )
        return bool(
            re.search(
                rf"gain\s*[: ]+\s*{cls.internal_gain_db}\s*dB",
                metadata,
                flags=re.IGNORECASE,
            )
        )


_UMIK1_PROFILE = _Umik1Profile()


class _Umik2Profile:
    model = "UMIK-2"
    vendor_id = "2752"
    product_id = "002b"
    factory_analog_gain_db = 18.0
    reference_capture_gain_db = 0.0
    reference_capture_volume_percent = 100.0

    @staticmethod
    def parse_calibration_header(path: Path) -> dict[str, Any]:
        try:
            first_lines = "\n".join(path.read_text(encoding="utf-8", errors="ignore").splitlines()[:8])
        except Exception:
            return {}
        sensitivity = re.search(
            r"Sens\s*Factor\s*=\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*dB",
            first_lines,
            flags=re.IGNORECASE,
        )
        analog_gain = re.search(
            r"AGain\s*=\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*dB",
            first_lines,
            flags=re.IGNORECASE,
        )
        serial = re.search(r"SERNO\s*:\s*([0-9][0-9 -]*)", first_lines, flags=re.IGNORECASE)
        return {
            "sensitivity_factor_db": float(sensitivity.group(1)) if sensitivity else None,
            "analog_gain_db": float(analog_gain.group(1)) if analog_gain else None,
            "serial_number": re.sub(r"\D", "", serial.group(1)) if serial else "",
        }

    @classmethod
    def calibration_reference(cls, path: Path, filename: str) -> dict[str, Any]:
        header = cls.parse_calibration_header(path)
        serial_number = str(header.get("serial_number") or "")
        filename_serial = "".join(re.findall(r"\d", Path(filename).stem))
        return {
            **header,
            "serial_matches_filename": bool(
                serial_number and filename_serial and serial_number == filename_serial
            ),
        }

    @classmethod
    def matches_input(cls, item: dict[str, Any]) -> bool:
        vendor = str(item.get("device_vendor_id") or "").lower().replace("0x", "").zfill(4)
        product = str(item.get("device_product_id") or "").lower().replace("0x", "").zfill(4)
        metadata = " ".join(
            str(item.get(key) or "")
            for key in (
                "node_name", "node_description", "device_name", "device_description",
                "alsa_card_name", "alsa_long_card_name",
            )
        ).lower()
        return vendor == cls.vendor_id and product == cls.product_id and "umik-2" in metadata


_UMIK2_PROFILE = _Umik2Profile()


class _Umm6Profile:
    model = "Dayton UMM-6"
    vendor_id = "0d8c"
    product_id = "0147"
    reference_sensitivity_dbfs_per_pa = -19.0
    reference_ipga_db = 30.0
    reference_capture_gain_db = 0.0
    reference_capture_volume_percent = 100.0

    @staticmethod
    def parse_calibration_header(path: Path) -> dict[str, Any]:
        try:
            first_lines = "\n".join(path.read_text(encoding="utf-8", errors="ignore").splitlines()[:8])
        except Exception:
            return {}
        sensitivity = re.search(
            r"Sens\s*Factor\s*=\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*dB",
            first_lines,
            flags=re.IGNORECASE,
        )
        serial = re.search(r"SERNO\s*:\s*([0-9][0-9 -]*)", first_lines, flags=re.IGNORECASE)
        return {
            "sensitivity_factor_db": float(sensitivity.group(1)) if sensitivity else None,
            "serial_number": re.sub(r"\D", "", serial.group(1)) if serial else "",
        }

    @classmethod
    def calibration_reference(cls, path: Path, filename: str) -> dict[str, Any]:
        header = cls.parse_calibration_header(path)
        serial_number = str(header.get("serial_number") or "")
        filename_serial = "".join(re.findall(r"\d", Path(filename).stem))
        return {
            **header,
            "serial_matches_filename": bool(
                serial_number and filename_serial and serial_number == filename_serial
            ),
        }

    @classmethod
    def matches_input(cls, item: dict[str, Any]) -> bool:
        vendor = str(item.get("device_vendor_id") or "").lower().replace("0x", "").zfill(4)
        product = str(item.get("device_product_id") or "").lower().replace("0x", "").zfill(4)
        metadata = " ".join(
            str(item.get(key) or "")
            for key in (
                "node_name", "node_description", "device_name", "device_description",
                "device_product_name", "alsa_card_name", "alsa_long_card_name",
            )
        ).lower()
        model_match = bool(re.search(r"\bumm[\s_-]*6\b", metadata))
        return vendor == cls.vendor_id and product == cls.product_id and model_match

    @staticmethod
    def parse_calibration_curve(path: Path) -> tuple[Any, Any, Any] | None:
        import numpy as calibration_np

        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            return None
        points: list[tuple[float, float, float]] = []
        for raw_line in lines:
            parts = re.split(r"[\s,;]+", raw_line.strip())
            if len(parts) != 3:
                continue
            try:
                frequency, correction, phase = (float(value) for value in parts)
            except ValueError:
                continue
            if (
                frequency > 0.0
                and math.isfinite(frequency)
                and math.isfinite(correction)
                and math.isfinite(phase)
            ):
                points.append((frequency, correction, phase))
        if len(points) < 2:
            return None
        points.sort(key=lambda point: point[0])
        if any(points[index][0] == points[index - 1][0] for index in range(1, len(points))):
            return None
        return tuple(
            calibration_np.asarray(values, dtype=calibration_np.float64)
            for values in zip(*points)
        )


_UMM6_PROFILE = _Umm6Profile()


def _parse_umik_calibration_header(path: Path) -> dict[str, Any]:
    return _UMIK1_PROFILE.parse_calibration_header(path)


def _is_umik1_input(item: dict[str, Any]) -> bool:
    return _UMIK1_PROFILE.matches_input(item)


def _is_umik2_input(item: dict[str, Any]) -> bool:
    return _UMIK2_PROFILE.matches_input(item)


def _is_umm6_input(item: dict[str, Any]) -> bool:
    return _UMM6_PROFILE.matches_input(item)


def _spl_auto_capability() -> dict[str, Any]:
    dependencies = _dependencies()
    measurement_store = dependencies.get_measurement_store()
    calibration_state = measurement_store.get_calibration_state() if measurement_store else {}
    active_id = str(calibration_state.get("active_calibration_file_id") or "")
    entries = calibration_state.get("calibrations") if isinstance(calibration_state, dict) else []
    active = next((item for item in (entries or []) if str(item.get("id") or "") == active_id), {})
    name = str(active.get("filename") or "")
    path = Path(str(active.get("path") or "")) if active else Path()
    settings = dependencies.read_measurement_settings()
    selected_id = str(settings.get("selectedInputId") or "")
    inputs_payload = measurement_store.list_inputs() if measurement_store else {}
    inputs = inputs_payload.get("inputs") if isinstance(inputs_payload, dict) else []
    selected = next((item for item in (inputs or []) if str(item.get("id") or "") == selected_id), {})
    profile = (
        _UMIK1_PROFILE if selected and _is_umik1_input(selected)
        else _UMIK2_PROFILE if selected and _is_umik2_input(selected)
        else _UMM6_PROFILE if selected and _is_umm6_input(selected)
        else None
    )
    calibration = (
        profile.calibration_reference(path, name)
        if profile and active and path.is_file()
        else {}
    )
    calibration_curve_valid = (
        _UMM6_PROFILE.parse_calibration_curve(path) is not None
        if profile is _UMM6_PROFILE and active and path.is_file()
        else True
    )
    umik_inputs = [item for item in (inputs or []) if profile and profile.matches_input(item)]

    supported_model = profile.model if profile else None
    sensitivity = calibration.get("sensitivity_factor_db")
    cal_serial = str(calibration.get("serial_number") or "")
    serial_matches = bool(calibration.get("serial_matches_filename"))
    analog_gain = calibration.get("analog_gain_db")
    sensitivity_plausible = (
        sensitivity is not None
        and math.isfinite(float(sensitivity))
        and (
            profile is not _UMM6_PROFILE
            or -40.0 <= float(sensitivity) <= 0.0
        )
    )
    internal_gain_match = (
        _UMIK1_PROFILE.has_reference_internal_gain(selected)
        if profile is _UMIK1_PROFILE
        else True
        if profile is _UMM6_PROFILE
        else analog_gain is not None
        and abs(float(analog_gain) - _UMIK2_PROFILE.factory_analog_gain_db) <= 0.05
    )
    capture_gain = selected.get("capture_gain_db")
    capture_volume = selected.get("capture_volume_percent")
    gain_known = capture_gain is not None and capture_volume is not None
    capture_reference_match = gain_known and (
        abs(float(capture_gain) - float(profile.reference_capture_gain_db)) <= 0.05
        and abs(float(capture_volume) - float(profile.reference_capture_volume_percent)) <= 0.05
    ) if profile else False
    unique_umik = len(umik_inputs) == 1 and bool(selected) and str(umik_inputs[0].get("id") or "") == selected_id

    checks = {
        "selected_input_is_umik1": profile is _UMIK1_PROFILE,
        "unique_connected_umik1": unique_umik if profile is _UMIK1_PROFILE else False,
        "selected_input_is_umik2": profile is _UMIK2_PROFILE,
        "unique_connected_umik2": unique_umik if profile is _UMIK2_PROFILE else False,
        "selected_input_is_umm6": profile is _UMM6_PROFILE,
        "unique_connected_umm6": unique_umik if profile is _UMM6_PROFILE else False,
        "calibration_selected": bool(active_id and active),
        "sensitivity_factor_parsed": sensitivity_plausible,
        "calibration_serial_matches_filename": serial_matches,
        "calibration_curve_valid": calibration_curve_valid,
        "internal_gain_18_db": internal_gain_match,
        "capture_gain_known": gain_known,
        "capture_reference_state": capture_reference_match,
    }
    required_checks = (
        "selected_input_is_umik1", "unique_connected_umik1",
        "calibration_selected", "sensitivity_factor_parsed",
        "calibration_serial_matches_filename", "internal_gain_18_db",
        "capture_gain_known",
    ) if profile is _UMIK1_PROFILE else (
        "selected_input_is_umik2", "unique_connected_umik2",
        "calibration_selected", "sensitivity_factor_parsed",
        "calibration_serial_matches_filename", "internal_gain_18_db",
        "capture_gain_known", "capture_reference_state",
    ) if profile is _UMIK2_PROFILE else (
        "selected_input_is_umm6", "unique_connected_umm6",
        "calibration_selected", "sensitivity_factor_parsed",
        "calibration_serial_matches_filename", "calibration_curve_valid", "internal_gain_18_db",
        "capture_gain_known", "capture_reference_state",
    )
    available = bool(profile) and all(checks[key] for key in required_checks)
    if not selected_id:
        reason = "Select a supported UMIK measurement input first; manual C/Slow entry remains available."
    elif not selected:
        reason = "The selected measurement input is no longer available."
    elif not supported_model or not unique_umik:
        reason = ""
    elif not active:
        reason = f"Select the matching {supported_model} calibration file."
    elif not sensitivity_plausible or not cal_serial:
        reason = "The selected calibration file has no valid Sens Factor and SERNO."
    elif not serial_matches:
        reason = "The calibration SERNO does not match the selected calibration filename."
    elif profile is _UMM6_PROFILE and not calibration_curve_valid:
        reason = "The Dayton UMM-6 calibration file has no valid frequency/correction/phase curve."
    elif profile is _UMIK2_PROFILE and analog_gain is None:
        reason = "The selected UMIK-2 calibration file has no valid AGain."
    elif profile is _UMIK2_PROFILE and not internal_gain_match:
        reason = "The UMIK-2 calibration AGain does not match the 18 dB factory reference."
    elif not internal_gain_match:
        reason = "The UMIK-1 internal 18 dB reference gain cannot be verified."
    elif not gain_known:
        reason = f"The selected {supported_model} capture gain cannot be verified."
    elif profile is _UMIK2_PROFILE and not capture_reference_match:
        reason = "The UMIK-2 capture gain does not match the required 100% / 0 dB reference."
    elif profile is _UMM6_PROFILE and not capture_reference_match:
        reason = "The Dayton UMM-6 capture gain does not match the required +30 dB IPGA / 100% / 0 dB reference."
    else:
        reason = ""
    return {
        "available": available,
        "microphone_model": supported_model,
        "calibration_file_id": active_id,
        "calibration_filename": name,
        "calibration_path": str(path) if active else "",
        "sensitivity_factor_db": sensitivity,
        "analog_gain_db": analog_gain,
        "reference_sensitivity_dbfs_per_pa": (
            profile.reference_sensitivity_dbfs_per_pa
            if profile is _UMM6_PROFILE else None
        ),
        "reference_ipga_db": profile.reference_ipga_db if profile is _UMM6_PROFILE else None,
        "serial_number": cal_serial,
        "selected_input_id": selected_id,
        "selected_input": selected,
        "capture_gain_db": capture_gain,
        "capture_volume_percent": capture_volume,
        "reference_capture_gain_db": profile.reference_capture_gain_db if profile else None,
        "reference_capture_volume_percent": profile.reference_capture_volume_percent if profile else None,
        "checks": checks,
        "reason": reason,
    }


def _read_pcm16_channel(path: Path, channel_index: int) -> tuple[int, Any]:
    import numpy as spl_np

    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise RuntimeError("Automatic SPL capture did not produce 16-bit PCM")
        channels = wav.getnchannels()
        sample_rate = wav.getframerate()
        data = spl_np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(spl_np.float64)
    if channels < 1 or data.size < channels:
        raise RuntimeError("Automatic SPL capture is empty")
    selected = max(0, min(channels - 1, int(channel_index)))
    return sample_rate, data.reshape(-1, channels)[:, selected] / 32768.0


def _c_weighted_spl_from_capture(
    samples: Any,
    sample_rate: int,
    sensitivity_factor_db: float,
    calibration_path: Path,
    profile: Any = None,
) -> float:
    import numpy as spl_np

    measurement_store = _dependencies().get_measurement_store()
    signal = spl_np.asarray(samples, dtype=spl_np.float64)
    signal = signal - float(spl_np.mean(signal))
    if signal.size < sample_rate:
        raise RuntimeError("Automatic SPL capture is too short for stable averaging")
    window = spl_np.hanning(signal.size)
    coherent_power = float(spl_np.mean(spl_np.square(window)))
    spectrum = spl_np.fft.rfft(signal * window)
    frequencies = spl_np.fft.rfftfreq(signal.size, 1.0 / sample_rate)
    f2 = spl_np.square(frequencies)
    c_amplitude = (
        (12200.0 ** 2) * f2
        / spl_np.maximum((f2 + 20.6 ** 2) * (f2 + 12200.0 ** 2), 1e-30)
    ) * (10.0 ** (0.06 / 20.0))

    calibration_curve = (
        _UMM6_PROFILE.parse_calibration_curve(calibration_path)
        if profile is _UMM6_PROFILE
        else measurement_store._parse_calibration_file(calibration_path) if measurement_store else None
    )
    if calibration_curve is None:
        raise RuntimeError("The selected microphone calibration curve could not be parsed")
    cal_phases = None
    if profile is _UMM6_PROFILE:
        cal_freqs, cal_offsets, cal_phases = calibration_curve
    else:
        cal_freqs, cal_offsets = calibration_curve
    interpolated = spl_np.interp(
        spl_np.log(spl_np.maximum(frequencies, 1e-9)),
        spl_np.log(cal_freqs),
        cal_offsets,
        left=float(cal_offsets[0]),
        right=float(cal_offsets[-1]),
    )
    correction = spl_np.power(10.0, -interpolated / 20.0)
    if cal_phases is not None:
        interpolated_phase = spl_np.interp(
            spl_np.log(spl_np.maximum(frequencies, 1e-9)),
            spl_np.log(cal_freqs),
            cal_phases,
            left=float(cal_phases[0]),
            right=float(cal_phases[-1]),
        )
        correction = correction * spl_np.exp(-1j * spl_np.deg2rad(interpolated_phase))
    weighted = spectrum * c_amplitude * correction
    weighted_time = spl_np.fft.irfft(weighted, n=signal.size)
    rms = math.sqrt(float(spl_np.mean(spl_np.square(weighted_time))) / max(coherent_power, 1e-12))
    dbfs = 20.0 * math.log10(max(rms, 1e-12))
    # miniDSP/REW Sens Factor is referenced to 100 dB SPL with the UMIK
    # capture level at 100% (24 dB digital reference gain), hence +124 dB.
    if profile is _UMM6_PROFILE:
        # Dayton/REW Sens Factor is the individual dBFS reading at 94 dB SPL
        # with the UMM-6 at its maximum (+30 dB IPGA) capture reference.
        return dbfs + 94.0 - float(sensitivity_factor_db)
    return dbfs + 124.0 - float(sensitivity_factor_db)


def _calculate_spl_required_adjustment(measured_spl_db: float) -> float:
    measured = float(measured_spl_db)
    if not 40.0 <= measured <= 130.0:
        raise ValueError("Measured SPL must be between 40 and 130 dB")
    return 83.0 - measured


def _terminate_and_reap(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        try:
            process.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            pass
    except Exception:
        logger.exception("Failed to terminate SPL calibration process; escalating to kill")
    process.kill()
    process.wait(timeout=2)


def _restore_spl_calibration_audio(operation: _SplCalibrationOperation) -> None:
    dependencies = _dependencies()
    easyeffects_manager = dependencies.get_easyeffects_manager()
    restore = operation.restore_state
    if not restore:
        return
    operation.restore_state = None

    # Restore attenuation before re-enabling gain-bearing plugins.
    try:
        current_volume = dependencies.get_output_volume()
        if abs(
            float(current_volume) - float(restore["system_volume_percent"])
        ) <= 0.05:
            pass
        elif abs(float(current_volume) - 100.0) <= 0.05:
            dependencies.set_output_volume(restore["system_volume_percent"])
        else:
            logger.info(
                "Preserving newer system volume during SPL cleanup: current=%s owned=100",
                current_volume,
            )
    except Exception:
        logger.exception("Failed to restore system volume after SPL calibration")

    if easyeffects_manager is None:
        return

    def restore_property(
        plugin: str,
        name: str,
        value: Any,
        owned_value: Any,
    ) -> None:
        try:
            current = easyeffects_manager.get_active_plugin_property(plugin, 0, name)
            if isinstance(owned_value, bool):
                current_matches = (
                    str(current).lower() in {"true", "1", "on"}
                ) is owned_value
            else:
                current_matches = math.isclose(
                    float(current),
                    float(owned_value),
                    rel_tol=0.0,
                    abs_tol=0.05,
                )
            if not current_matches:
                logger.info(
                    "Preserving newer EasyEffects value during SPL cleanup: "
                    "plugin=%s property=%s current=%s owned=%s",
                    plugin,
                    name,
                    current,
                    owned_value,
                )
                return
            easyeffects_manager.set_active_plugin_property(plugin, 0, name, value)
        except Exception:
            logger.exception(
                "Failed to restore SPL calibration property: plugin=%s property=%s",
                plugin,
                name,
            )

    restore_property("autogain", "bypass", restore["autogain_bypass"], True)
    restore_property(
        "loudness",
        "outputGain",
        restore["loudness_output_gain"],
        0.0,
    )
    if not restore["loudness_bypass"]:
        try:
            time.sleep(0.10)
        except Exception:
            logger.exception("SPL calibration Loudness restore settle failed")
    restore_property("loudness", "bypass", restore["loudness_bypass"], True)


def _start_spl_calibration_noise(operation: _SplCalibrationOperation) -> dict[str, Any]:
    dependencies = _dependencies()
    ee_manager = dependencies.require_easyeffects_manager()
    operation.restore_state = {
        "autogain_bypass": (
            ee_manager.get_active_plugin_property("autogain", 0, "bypass").lower()
            in {"true", "1", "on"}
        ),
        "loudness_bypass": (
            ee_manager.get_active_plugin_property("loudness", 0, "bypass").lower()
            in {"true", "1", "on"}
        ),
        "loudness_output_gain": float(
            ee_manager.get_active_plugin_property("loudness", 0, "outputGain")
        ),
        "system_volume_percent": dependencies.get_output_volume(),
    }
    try:
        ee_manager.set_active_plugin_property("autogain", 0, "bypass", True)
        ee_manager.set_active_plugin_property("loudness", 0, "bypass", True)
        ee_manager.set_active_plugin_property("loudness", 0, "outputGain", 0.0)
        time.sleep(0.10)
        dependencies.set_output_volume(100)
        if operation.cancel_requested:
            raise RuntimeError("SPL calibration was stopped")

        noise_path = Path(tempfile.gettempdir()) / "fxroute-spl-calibration-pink-noise-v2.wav"
        if not noise_path.exists():
            generated = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "anoisesrc=color=pink:duration=60:sample_rate=48000",
                    "-af", "loudnorm=I=-23:TP=-3:LRA=7,afade=t=in:st=0:d=1,afade=t=out:st=59.5:d=0.5",
                    "-ac", "2", "-ar", "48000", str(noise_path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if generated.returncode != 0:
                raise HTTPException(
                    status_code=500,
                    detail=(generated.stderr or "Pink-noise generation failed").strip(),
                )
        operation.noise_file = noise_path
        if operation.cancel_requested:
            raise RuntimeError("SPL calibration was stopped")
        operation.noise_process = subprocess.Popen(
            ["pw-play", "--volume=1.0", str(noise_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if operation.cancel_requested:
            _terminate_and_reap(operation.noise_process)
            raise RuntimeError("SPL calibration was stopped")
    except BaseException:
        _terminate_and_reap(operation.noise_process)
        _restore_spl_calibration_audio(operation)
        raise
    return {"status": "playing", "settle_seconds": 1.0, "average_seconds": 3.0}


def _operation_lock() -> asyncio.Lock:
    if _runtime.operation_lock is None:
        _runtime.operation_lock = asyncio.Lock()
    return _runtime.operation_lock


async def _acquire_operation(kind: str) -> _SplCalibrationOperation:
    async with _operation_lock():
        if _runtime.operation is not None:
            raise HTTPException(status_code=409, detail="SPL calibration is already active")
        operation_id = uuid4().hex
        operation = _SplCalibrationOperation(
            id=operation_id,
            kind=kind,
            session_job_id=f"spl-calibration:{operation_id}",
        )
        operation.worker_task = asyncio.current_task()
        _runtime.operation = operation
        return operation


async def _register_operation(operation: _SplCalibrationOperation) -> None:
    dependencies = _dependencies()
    measurement_sr_session = dependencies.get_measurement_session()
    if measurement_sr_session is None:
        return
    operation.session = measurement_sr_session
    operation.registration_attempted = True
    await operation.session.register_spl_job(operation.session_job_id)
    if operation.cancel_requested:
        raise RuntimeError("SPL calibration was stopped")
    await dependencies.measurement_entry_preflight(48_000)


def _request_operation_process_stop(operation: _SplCalibrationOperation) -> None:
    operation.cancel_requested = True
    for process in (operation.recorder, operation.noise_process):
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except Exception:
                logger.exception("Failed to request SPL calibration process stop")


async def _run_operation_thread(
    operation: _SplCalibrationOperation,
    func: Any,
    /,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Drain owner-mutating thread work before propagating request cancellation."""
    thread_task = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    cancelled = False
    while not thread_task.done():
        try:
            await asyncio.shield(thread_task)
        except asyncio.CancelledError:
            cancelled = True
            _request_operation_process_stop(operation)
    if cancelled:
        try:
            thread_task.result()
        except BaseException:
            logger.exception("SPL calibration thread failed while request was cancelled")
        raise asyncio.CancelledError
    return thread_task.result()


async def _cleanup_operation(operation: _SplCalibrationOperation) -> None:
    async with operation.cleanup_lock:
        if operation.completed.is_set() and _runtime.operation is not operation:
            return
        operation.completed.clear()
        cleanup_failed = False
        for label, process in (
            ("capture", operation.recorder),
            ("noise", operation.noise_process),
        ):
            try:
                await asyncio.to_thread(_terminate_and_reap, process)
            except BaseException:
                logger.exception("Failed to stop SPL calibration %s process", label)
            if process is not None and process.poll() is None:
                cleanup_failed = True
            elif label == "capture":
                operation.recorder = None
            else:
                operation.noise_process = None

        try:
            await asyncio.to_thread(_restore_spl_calibration_audio, operation)
        except BaseException:
            logger.exception("Failed to restore SPL calibration audio state")

        if operation.registration_attempted:
            released = operation.session is None
            for _ in range(2):
                if released:
                    break
                try:
                    await operation.session.unregister_spl_job(operation.session_job_id)
                    released = True
                except BaseException:
                    logger.exception("Failed to release SPL calibration sample-rate ownership")
            if not released:
                active_ids = getattr(operation.session, "active_spl_job_ids", None)
                session_lock = getattr(operation.session, "lock", None)
                if isinstance(active_ids, set) and session_lock is not None:
                    try:
                        async with session_lock:
                            active_ids.discard(operation.session_job_id)
                            check_release = getattr(operation.session, "_check_release", None)
                            if callable(check_release):
                                await check_release()
                        released = operation.session_job_id not in active_ids
                    except BaseException:
                        logger.exception("Failed fallback SPL session ownership cleanup")
            cleanup_failed = cleanup_failed or not released

        if (
            operation.source_node
            and operation.source_volume_percent is not None
            and abs(operation.source_volume_percent - 100.0) > 0.05
        ):
            try:
                current = await asyncio.to_thread(
                    subprocess.run,
                    ["pactl", "get-source-volume", operation.source_node],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=True,
                )
                gain = re.search(
                    r"/\s*([0-9.]+)%\s*/\s*[-+0-9.]+\s*dB",
                    current.stdout or "",
                )
                if gain is None:
                    logger.warning(
                        "Preserving capture volume because SPL cleanup could not parse "
                        "the current gain: source=%s",
                        operation.source_node,
                    )
                elif abs(float(gain.group(1)) - 100.0) > 0.05:
                    logger.info(
                        "Preserving newer capture volume during SPL cleanup: "
                        "source=%s current=%s owned=100",
                        operation.source_node,
                        gain.group(1),
                    )
                else:
                    await asyncio.to_thread(
                        subprocess.run,
                        [
                            "pactl", "set-source-volume", operation.source_node,
                            f"{operation.source_volume_percent:.4f}%",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=3,
                        check=False,
                    )
            except Exception:
                logger.exception("Failed to inspect SPL calibration capture volume")
        if operation.capture_path is not None:
            try:
                operation.capture_path.unlink(missing_ok=True)
            except Exception:
                logger.exception("Failed to remove SPL calibration capture file")

        if not cleanup_failed:
            async with _operation_lock():
                if _runtime.operation is operation:
                    _runtime.operation = None
        operation.completed.set()
        if cleanup_failed:
            raise RuntimeError("SPL calibration cleanup did not release every owned resource")


async def _cleanup_operation_shielded(operation: _SplCalibrationOperation) -> None:
    cleanup_task = asyncio.create_task(_cleanup_operation(operation))
    cancelled = False
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            cancelled = True
    cleanup_task.result()
    if cancelled:
        raise asyncio.CancelledError


async def _watch_manual_noise(operation: _SplCalibrationOperation) -> None:
    try:
        if operation.noise_process is not None:
            await _run_operation_thread(operation, operation.noise_process.wait)
    finally:
        await _cleanup_operation_shielded(operation)


async def _stop_active_operation() -> None:
    async with _operation_lock():
        operation = _runtime.operation
        if operation is None:
            return
        worker_task = operation.worker_task
        _request_operation_process_stop(operation)
    if worker_task is asyncio.current_task() or worker_task is None:
        await _cleanup_operation_shielded(operation)
        return
    try:
        await asyncio.wait_for(
            asyncio.shield(operation.completed.wait()),
            timeout=SPL_STOP_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise RuntimeError("Timed out waiting for SPL calibration cleanup") from exc
    if _runtime.operation is operation:
        await _cleanup_operation_shielded(operation)


async def shutdown() -> None:
    await _stop_active_operation()


@router.get("/api/measurements/spl-calibration")
async def get_spl_calibration():
    extras = _dependencies().require_easyeffects_manager().load_global_extras()
    return {
        "status": "ok",
        "target_spl_db": 83.0,
        "noise_lufs": -23.0,
        "meter_hint": "C-weighted / Slow",
        "output_profile": _spl_output_profile(),
        "loudness": extras["loudness"],
        "automatic": _spl_auto_capability(),
        "noise_active": bool(
            _runtime.operation is not None
            and _runtime.operation.noise_process is not None
            and _runtime.operation.noise_process.poll() is None
        ),
        "operation_active": _runtime.operation is not None,
    }


@router.post("/api/measurements/spl-calibration/noise")
async def set_spl_calibration_noise(request: Request):
    body = await request.json()
    enabled = bool(body.get("enabled"))
    if not enabled:
        await _stop_active_operation()
        return {"status": "stopped"}

    operation = await _acquire_operation("manual-noise")
    try:
        await _register_operation(operation)
        result = await _run_operation_thread(
            operation,
            _start_spl_calibration_noise,
            operation,
        )
        if operation.cancel_requested:
            raise RuntimeError("SPL calibration was stopped")
        watcher = asyncio.create_task(
            _watch_manual_noise(operation),
            name=f"spl-noise-watch:{operation.id}",
        )
        operation.worker_task = watcher
        return result
    except BaseException:
        await _cleanup_operation_shielded(operation)
        raise


@router.post("/api/measurements/spl-calibration/automatic")
async def measure_spl_automatically():
    dependencies = _dependencies()
    operation = await _acquire_operation("automatic")
    try:
        capability = _spl_auto_capability()
        if not capability["available"]:
            raise HTTPException(status_code=409, detail=capability["reason"])
        selected = capability["selected_input"]
        node_name = str(selected.get("node_name") or "")
        microphone_model = str(capability["microphone_model"])
        if not node_name:
            raise HTTPException(
                status_code=409,
                detail=f"The selected {microphone_model} PipeWire node is unavailable",
            )

        operation.capture_path = (
            Path(tempfile.gettempdir())
            / f"fxroute-spl-{microphone_model.lower()}-{uuid4().hex[:10]}.wav"
        )
        operation.source_node = node_name
        settings = dependencies.read_measurement_settings()
        channel_index = max(0, int(settings.get("selectedMicInputChannel") or "1") - 1)

        await _register_operation(operation)
        before_gain = await _run_operation_thread(
            operation,
            subprocess.run,
            ["pactl", "get-source-volume", node_name],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        before_match = re.search(
            r"/\s*([0-9.]+)%\s*/\s*([-+0-9.]+)\s*dB",
            before_gain.stdout or "",
        )
        if not before_match:
            raise RuntimeError(
                f"The {microphone_model} capture gain could not be read before calibration"
            )
        operation.source_volume_percent = float(before_match.group(1))
        if abs(operation.source_volume_percent - 100.0) > 0.05:
            await _run_operation_thread(
                operation,
                subprocess.run,
                ["pactl", "set-source-volume", node_name, "100%"],
                capture_output=True,
                text=True,
                timeout=3,
                check=True,
            )
        verified = await _run_operation_thread(
            operation,
            subprocess.run,
            ["pactl", "get-source-volume", node_name],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        gain = re.search(r"/\s*([0-9.]+)%\s*/\s*([-+0-9.]+)\s*dB", verified.stdout or "")
        if not gain or abs(float(gain.group(1)) - 100.0) > 0.05 or abs(float(gain.group(2))) > 0.05:
            raise RuntimeError(f"The {microphone_model} capture gain could not be set to the 100% / 0 dB reference")
        if operation.cancel_requested:
            raise RuntimeError("SPL calibration was stopped")

        operation.recorder = subprocess.Popen(
            [
                "pw-record", "--target", node_name, "--rate", "48000",
                "--channels", "2", "--format", "s16", "--sample-count", str(5 * 48000),
                str(operation.capture_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        await asyncio.sleep(0.2)
        if operation.cancel_requested:
            raise RuntimeError("SPL calibration was stopped")
        await _run_operation_thread(
            operation,
            _start_spl_calibration_noise,
            operation,
        )
        try:
            _stdout, stderr = await _run_operation_thread(
                operation,
                operation.recorder.communicate,
                timeout=8,
            )
        except subprocess.TimeoutExpired:
            operation.recorder.kill()
            _stdout, stderr = await _run_operation_thread(
                operation,
                operation.recorder.communicate,
            )
            raise RuntimeError(f"{microphone_model} automatic SPL capture timed out")
        if operation.cancel_requested:
            raise RuntimeError("SPL calibration was stopped")
        if operation.recorder.returncode != 0 and (
            not operation.capture_path.exists()
            or operation.capture_path.stat().st_size < 192044
        ):
            raise RuntimeError(
                f"pw-record exited {operation.recorder.returncode}: "
                f"{(stderr or f'{microphone_model} automatic SPL capture failed').strip()}"
            )

        try:
            sample_rate, samples = _read_pcm16_channel(operation.capture_path, channel_index)
        except Exception as exc:
            raise RuntimeError(f"Could not read the {microphone_model} capture: {type(exc).__name__}: {exc}") from exc
        stable = samples[-3 * sample_rate:]
        try:
            measured = _c_weighted_spl_from_capture(
                stable,
                sample_rate,
                float(capability["sensitivity_factor_db"]),
                Path(capability["calibration_path"]),
                profile=(
                    _UMM6_PROFILE
                    if microphone_model == _UMM6_PROFILE.model
                    else None
                ),
            )
        except Exception as exc:
            raise RuntimeError(f"Could not calculate {microphone_model} SPL: {type(exc).__name__}: {exc}") from exc
        if not math.isfinite(measured) or not 40.0 <= measured <= 130.0:
            raise RuntimeError(f"Automatic {microphone_model} SPL result is outside the valid range ({measured:.1f} dB)")
        return {
            "status": "ok",
            "measured_spl_db": round(measured, 2),
            "required_adjustment_db": round(_calculate_spl_required_adjustment(measured), 2),
            "weighting": "C",
            "averaging_seconds": 3.0,
            "microphone_model": microphone_model,
            "serial_number": capability["serial_number"],
        }
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=f"{type(exc).__name__}: {exc}") from exc
    finally:
        await _cleanup_operation_shielded(operation)


@router.post("/api/measurements/spl-calibration/apply")
async def apply_spl_calibration(request: Request):
    body = await request.json()
    measured = float(body.get("measured_spl_db"))
    try:
        adjustment = _calculate_spl_required_adjustment(measured)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _stop_active_operation()
    ee_manager = _dependencies().require_easyeffects_manager()
    extras = ee_manager.load_global_extras()
    profile = _spl_output_profile()
    automatic = _spl_auto_capability()
    extras["loudness"]["params"]["calibration"] = {
        "outputProfileId": profile["id"],
        "outputProfileLabel": profile["label"],
        "targetSplDb": 83.0,
        "measuredSplDb": measured,
        "requiredAdjustmentDb": adjustment,
        "calibrated": abs(adjustment) <= 1.0,
        "method": "automatic" if automatic["available"] else "manual",
        "microphoneModel": automatic["microphone_model"],
        "calibrationFileId": automatic["calibration_file_id"],
        "calibrationFilename": automatic["calibration_filename"],
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }
    profiles = dict(extras["loudness"]["params"].get("calibrationProfiles") or {})
    profiles[profile["id"]] = dict(extras["loudness"]["params"]["calibration"])
    extras["loudness"]["params"]["calibrationProfiles"] = profiles
    saved = ee_manager.apply_autogain_loudness_runtime(
        ee_manager.load_global_extras(), extras, persist_all_presets=False
    )["extras"]
    return {
        "status": "ok",
        "required_adjustment_db": adjustment,
        "calibrated": abs(adjustment) <= 1.0,
        "calibration": saved["loudness"]["params"]["calibration"],
    }
