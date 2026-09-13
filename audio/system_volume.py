"""Helpers for PipeWire/PulseAudio output volume control via wpctl.

Volume scale semantics (verified on the real .104 sink path, UMC204HD,
pipewire 1.6.8): the FXRoute master percent is the PipeWire/Pulse volume
fraction times 100. The sink applies that volume as a float-domain gain
before the float→integer conversion, and the transfer curve is the
PulseAudio cubic ``(percent / 100) ** 3`` (100% -> 1.0, 50% -> 0.125,
31% -> -30.5 dB, 10% -> -60.0 dB). Peak/headroom calculations must
therefore never treat ``percent / 100`` as a linear gain; use
:func:`volume_percent_to_linear_gain` instead.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import subprocess
import threading
import time
from typing import Any

from audio.tool_env import c_locale_env

logger = logging.getLogger(__name__)


class SystemVolumeError(RuntimeError):
    """Raised when output volume cannot be read or changed."""


class SystemVolumeReadbackError(SystemVolumeError):
    """Raised when the set completed but its verification read failed."""

    volume_write_applied = True


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


def volume_percent_to_linear_gain(percent: int | float, *, clamp_upper: bool = True) -> float:
    """Return the real linear gain PipeWire applies for a master percent.

    The FXRoute master percent is the PipeWire/Pulse volume fraction times
    100. PipeWire applies the volume as a float-domain gain on the sink, and
    its transfer curve is the PulseAudio cubic ``(percent / 100) ** 3``:
    100% -> 1.0 (0 dB), 50% -> 0.125 (-18.1 dB), 31% -> 0.0298 (-30.5 dB),
    0% -> 0. This was verified on the real sink path (.104, UMC204HD,
    pipewire 1.6.8): five master settings measured on the sink monitor match
    the cubic curve exactly. Do not use ``percent / 100`` as a linear gain in
    peak or headroom calculations.

    ``clamp_upper=False`` keeps the FXRoute-internal 100% cap out of the
    conversion (the lower bound is always enforced). FXRoute itself limits
    the master to 100%, but PipeWire can be set above 100% externally (e.g.
    unity-backed clients); a safety decision must not under-estimate the
    gain the sink actually applies, so safety reads pass ``clamp_upper=False``.
    """
    value = max(0.0, float(percent))
    if clamp_upper:
        value = min(100.0, value)
    return (value / 100.0) ** 3


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


def _selected_output_name() -> str | None:
    from audio.samplerate.persistence import _load_audio_output_selection

    return _load_audio_output_selection().get("selected_key")


def _get_target_volume(target: str, *, clamp_upper: bool = True, sink: bool = False) -> int:
    """Live, timeout-bounded wpctl read (never served from a cache)."""
    if sink:
        output = _run_command(["pactl", "get-sink-volume", target])
        channels = re.findall(r"/\s*([0-9]+)%", output)
        if not channels:
            raise SystemVolumeError(f"Unable to parse sink volume: {output!r}")
        percent = max(map(int, channels))
        return min(100, percent) if clamp_upper else percent
    output = _run_command(["wpctl", "get-volume", target])
    return _parse_wpctl_volume(output, clamp_upper=clamp_upper)


def _set_target_volume(target: str, percent: int | float, *, sink: bool = False, master: bool = False) -> int:
    clamped = max(0, min(100, round(float(percent))))
    publish_status = master or target == TARGET_SINK
    write_started_at = time.monotonic()
    command = ["pactl", "set-sink-volume"] if sink else ["wpctl", "set-volume"]
    _run_command([*command, target, f"{clamped}%"])
    if publish_status:
        # Publish the committed command value before verification so an
        # unreadable readback cannot make a later relative write reuse stale
        # status state.
        _publish_status_volume(clamped, write_started_at)
    try:
        verified = _get_target_volume(target, sink=True) if sink else _get_target_volume(target)
    except Exception as exc:
        raise SystemVolumeReadbackError(
            f"Volume set completed but readback failed: {exc}"
        ) from exc
    if publish_status:
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
            env=c_locale_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise SystemVolumeError(f"Command timed out: {' '.join(args)}") from exc
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise SystemVolumeError(stderr or f"Command failed: {' '.join(args)}")
    return result.stdout.strip()


def _parse_wpctl_volume(output: str, *, clamp_upper: bool = True) -> int:
    match = re.search(r"Volume:\s*([0-9]*\.?[0-9]+)", output)
    if not match:
        raise SystemVolumeError(f"Unable to parse volume from wpctl output: {output!r}")
    normalized = float(match.group(1))
    percent = round(normalized * 100)
    if clamp_upper:
        return max(0, min(100, percent))
    return max(0, percent)


def get_output_volume() -> int:
    """Read the saved FXRoute sink by name, even if the system default drifted."""
    selected = _selected_output_name()
    if selected:
        return _get_target_volume(selected, sink=True)
    return _get_target_volume(TARGET_SINK)


def get_output_volume_unclamped() -> int:
    """Live sink master percent without the 100% cap (safety reads only).

    FXRoute's regular master limit is 100%, but PipeWire can be set above
    100% externally (e.g. unity-backed clients). A safety decision must not
    under-estimate the gain the sink applies, so this read reports the real
    percent. The canonical clamped :func:`get_output_volume` remains the
    UI/master value; change only the safety read, never the master limit.
    """
    selected = _selected_output_name()
    if selected:
        return _get_target_volume(selected, clamp_upper=False, sink=True)
    return _get_target_volume(TARGET_SINK, clamp_upper=False)


def set_output_volume(percent: int | float) -> int:
    selected = _selected_output_name()
    if selected:
        # Keep write and readback on one stable name across node ID rebuilds.
        return _set_target_volume(selected, percent, sink=True, master=True)
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
