"""Helpers for PipeWire/PulseAudio output volume control via wpctl."""

from __future__ import annotations

import re
import subprocess


class SystemVolumeError(RuntimeError):
    """Raised when output volume cannot be read or changed."""


TARGET_SINK = "@DEFAULT_AUDIO_SINK@"


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
    output = _run_command(["wpctl", "get-volume", TARGET_SINK])
    return _parse_wpctl_volume(output)


def set_output_volume(percent: int | float) -> int:
    clamped = max(0, min(100, round(float(percent))))
    _run_command(["wpctl", "set-volume", TARGET_SINK, f"{clamped}%"])
    return get_output_volume()
