# SPDX-License-Identifier: AGPL-3.0-only

"""Module-level sink/mute primitives and constants of the runtime adapter."""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path

from .deps import PlaybackRuntimeDependencies

SOURCE_HANDOFF_SETTLE_MS = 260
RADIO_EXPECTED_SAMPLE_RATE_HZ = 44100

# The gate sink is re-read at every mute readback boundary of a transition.
# Each resolution spawns the full PipeWire status pipeline, which dominated
# radio-start latency on slow hosts, so a very short memo keeps the burst of
# readbacks inside one transition on one resolution while still picking up an
# output change between transitions.
_GATE_SINK_CACHE_TTL_S = 2.0
_gate_sink_cache: dict[str, tuple[float, str]] = {}
_gate_sink_cache_lock = threading.Lock()


def _hardware_sink_for_transition(deps: PlaybackRuntimeDependencies) -> str:
    """Resolve the physical sink used by the coordinator output gate."""
    now = time.monotonic()
    with _gate_sink_cache_lock:
        cached = _gate_sink_cache.get("sink")
        if cached is not None and now - cached[0] <= _GATE_SINK_CACHE_TTL_S:
            return cached[1]
    resolved = _resolve_hardware_sink_for_transition(deps)
    with _gate_sink_cache_lock:
        _gate_sink_cache["sink"] = (now, resolved)
    return resolved


def _resolve_hardware_sink_for_transition(deps: PlaybackRuntimeDependencies) -> str:
    status = deps.get_samplerate_status()
    relevant_sink = status.get("relevant_sink") or {}
    output_key = str(relevant_sink.get("name") or "").strip()
    if output_key:
        return output_key
    overview = deps.get_audio_output_overview()
    output_mode = overview.get("output_mode") or {}
    output_key = str(output_mode.get("effective_output_key") or "").strip()
    if not output_key:
        raise RuntimeError("Playback transition output gate has no hardware sink")
    return output_key

def _playback_gate_state_path() -> Path:
    """Return the per-user marker used to recover a stale FXRoute mute."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return Path(runtime_dir) / "fxroute-playback-gate.json"
    return Path("/tmp") / f"fxroute-playback-gate-{os.getuid()}.json"

def _read_sink_mute(sink_name: str) -> bool:
    completed = subprocess.run(
        ["pactl", "get-sink-mute", sink_name],
        capture_output=True,
        text=True,
        check=False,
        timeout=1.5,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise RuntimeError(stderr or f"pactl get-sink-mute {sink_name} failed")
    match = re.search(
        r"(?:^|\n)\s*Mute:\s*(yes|no)\s*$",
        completed.stdout or "",
        re.IGNORECASE | re.MULTILINE,
    )
    if not match:
        raise RuntimeError(f"Could not parse mute state for sink {sink_name}")
    return match.group(1).lower() == "yes"

def _set_sink_mute(sink_name: str, muted: bool) -> None:
    completed = subprocess.run(
        ["pactl", "set-sink-mute", sink_name, "1" if muted else "0"],
        capture_output=True,
        text=True,
        check=False,
        timeout=1.5,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise RuntimeError(stderr or f"pactl set-sink-mute {sink_name} {int(muted)} failed")

def _read_hardware_sink_mute(output_key: str) -> bool:
    return _read_sink_mute(output_key)

def _set_hardware_sink_mute(output_key: str, muted: bool) -> None:
    _set_sink_mute(output_key, muted)

