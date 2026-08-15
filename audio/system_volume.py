"""Helpers for PipeWire/PulseAudio output volume control via wpctl."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import subprocess
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)


class SystemVolumeError(RuntimeError):
    """Raised when output volume cannot be read or changed."""


TARGET_SINK = "@DEFAULT_AUDIO_SINK@"

# Conservative bound for every wpctl invocation: a wedged PipeWire must
# never hold an API call or the event loop hostage indefinitely.
SYSTEM_VOLUME_COMMAND_TIMEOUT_SECONDS = 3.0

# Background refresh interval for the non-blocking status volume cache.
# External volume changes stay visible within roughly one monitor interval.
VOLUME_MONITOR_INTERVAL_SECONDS = 1.0

# Non-blocking last-known status volume for the playback/UI hot path, only
# for TARGET_SINK.  The tuple is (percent, read_started_at); the timestamp
# prevents a stale concurrent monitor read from overwriting a newer
# set/readback value.  The cache is shared between the event loop and
# worker threads (volume monitor, canonical writes), so publish performs
# its check+write under a small threading lock.  Reads stay lock-free.
_status_volume_cache: tuple[int, float] | None = None
_status_volume_publish_lock = threading.Lock()
_volume_monitor_task: asyncio.Task[Any] | None = None


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
    """Live, timeout-bounded wpctl read (never served from a cache)."""
    output = _run_command(["wpctl", "get-volume", target])
    return _parse_wpctl_volume(output)


def _set_target_volume(target: str, percent: int | float) -> int:
    clamped = max(0, min(100, round(float(percent))))
    _run_command(["wpctl", "set-volume", target, f"{clamped}%"])
    verified = _get_target_volume(target)
    if target == TARGET_SINK:
        _publish_status_volume(verified, time.monotonic())
    return verified


def _run_command(args: list[str]) -> str:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
            timeout=SYSTEM_VOLUME_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise SystemVolumeError(f"Command timed out: {' '.join(args)}") from exc
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
    """Live system output volume (timeout-bounded wpctl read)."""
    return _get_target_volume(TARGET_SINK)


def set_output_volume(percent: int | float) -> int:
    return _set_target_volume(TARGET_SINK, percent)


def get_node_volume(target: str) -> int:
    """Live node volume (e.g. measurement microphone gain)."""
    normalized = str(target or "").strip()
    if not normalized:
        raise SystemVolumeError("Node target is required")
    return _get_target_volume(normalized)


def set_node_volume(target: str, percent: int | float) -> int:
    normalized = str(target or "").strip()
    if not normalized:
        raise SystemVolumeError("Node target is required")
    return _set_target_volume(normalized, percent)


def _publish_status_volume(percent: int, read_started_at: float) -> None:
    """Publish a status volume unless a newer read already owns the slot.

    ``read_started_at`` is the moment the hardware read began.  A monitor
    read that started before a set/readback finished must not overwrite the
    newer set value.
    """
    global _status_volume_cache
    with _status_volume_publish_lock:
        current = _status_volume_cache
        if current is None or read_started_at >= current[1]:
            _status_volume_cache = (percent, read_started_at)


def get_status_volume(default: int = 100) -> int:
    """Non-blocking last-known status volume for the playback/UI hot path.

    Never spawns a subprocess: returns the last published value (monitor
    refresh or verified set readback), or ``default`` when no value has
    been published yet.
    """
    entry = _status_volume_cache
    if entry is None:
        return default
    return entry[0]


async def _volume_monitor_loop() -> None:
    """Refresh the status volume cache with real reads outside the loop."""
    while True:
        started_at = time.monotonic()
        try:
            percent = await asyncio.to_thread(get_output_volume)
            _publish_status_volume(percent, started_at)
        except Exception:
            logger.warning("Volume read monitor refresh failed", exc_info=True)
        await asyncio.sleep(VOLUME_MONITOR_INTERVAL_SECONDS)


def start_volume_read_monitor() -> asyncio.Task[Any]:
    """Start the owned background refresh of the status volume cache."""
    global _volume_monitor_task
    if _volume_monitor_task is not None and not _volume_monitor_task.done():
        return _volume_monitor_task
    _volume_monitor_task = asyncio.create_task(
        _volume_monitor_loop(),
        name="volume-read-monitor",
    )
    return _volume_monitor_task


async def stop_volume_read_monitor() -> None:
    """Cancel and drain the owned volume read monitor."""
    global _volume_monitor_task
    task = _volume_monitor_task
    _volume_monitor_task = None
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
