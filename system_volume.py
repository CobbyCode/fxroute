"""Helpers for PipeWire/PulseAudio output volume control via wpctl."""

from __future__ import annotations

import math
import re
import subprocess


class SystemVolumeError(RuntimeError):
    """Raised when output volume cannot be read or changed."""


TARGET_SINK = "@DEFAULT_AUDIO_SINK@"


def volume_percent_to_linear_gain(percent: int | float) -> float:
    """Return PipeWire's effective gain for the cubic FXRoute slider."""
    normalized = max(0.0, min(100.0, float(percent))) / 100.0
    return normalized ** 3


def volume_percent_to_db(percent: int | float, floor_db: float = -80.0) -> float:
    gain = volume_percent_to_linear_gain(percent)
    if gain <= 0.0:
        return float(floor_db)
    return max(float(floor_db), 20.0 * math.log10(gain))


def volume_db_to_percent(volume_db: int | float) -> int:
    db_value = float(volume_db)
    if db_value <= -80.0:
        return 0
    gain = 10.0 ** (db_value / 20.0)
    return max(0, min(100, round(100.0 * (gain ** (1.0 / 3.0)))))


def _get_target_volume(target: str) -> int:
    output = _run_command(["wpctl", "get-volume", target])
    return _parse_wpctl_volume(output)


def _set_target_volume(target: str, percent: int | float) -> int:
    clamped = max(0, min(100, round(float(percent))))
    _run_command(["wpctl", "set-volume", target, f"{clamped}%"])
    return _get_target_volume(target)


def _run_command(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise SystemVolumeError(stderr or f"Command failed: {' '.join(args)}")
    return result.stdout.strip()


def _parse_wpctl_volume(output: str) -> int:
    match = re.search(r"Volume:\s*([0-9]*\.?[0-9]+)", output)
    if not match:
        raise SystemVolumeError(f"Unable to parse volume from wpctl output: {output!r}")
    normalized = float(match.group(1))
    percent = round(normalized * 100)
    return max(0, min(100, percent))


def get_output_volume() -> int:
    return _get_target_volume(TARGET_SINK)


def set_output_volume(percent: int | float) -> int:
    return _set_target_volume(TARGET_SINK, percent)


def get_node_volume(target: str) -> int:
    normalized = str(target or "").strip()
    if not normalized:
        raise SystemVolumeError("Node target is required")
    return _get_target_volume(normalized)


def set_node_volume(target: str, percent: int | float) -> int:
    normalized = str(target or "").strip()
    if not normalized:
        raise SystemVolumeError("Node target is required")
    return _set_target_volume(normalized, percent)
