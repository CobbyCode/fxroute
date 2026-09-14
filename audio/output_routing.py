# SPDX-License-Identifier: AGPL-3.0-only
"""Device-scoped assignment of logical DSP signals to hardware outputs."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

SIGNALS = ("Off", "Main L", "Main R", "Sub 1", "Sub 2")


def device_key(output_key: str) -> str:
    """Keep the USB PCM identity across ACP multichannel/Pro Audio profiles."""
    if output_key.startswith("alsa_output.usb-") and output_key.endswith((".multichannel-output", ".pro-output-0")):
        return output_key.rsplit(".", 1)[0]
    return output_key


def _path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "fxroute" / "output-routing.json"


def _load() -> dict:
    try:
        data = json.loads(_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _valid(values: Any) -> bool:
    return isinstance(values, list) and all(type(value) is int and 0 <= value <= 4 for value in values)


def routing_payload(output_key: str, channels: int | None) -> dict[str, Any]:
    count = max(0, int(channels or 0))
    saved = _load().get(device_key(output_key))
    customized = _valid(saved)
    assignments = list(saved) if customized else [1, 2, 3, 4][:count]
    assignments.extend([0] * max(0, count - len(assignments)))
    return {
        "available": count > 2,
        "device_key": device_key(output_key),
        "assignments": assignments[:count],
        "customized": customized,
        "signals": [{"id": index, "label": label} for index, label in enumerate(SIGNALS)],
        "inactive_assignments": [index + 1 for index, value in enumerate(assignments) if index >= count and value],
    }


def validate_assignments(values: Any, channels: int) -> list[int]:
    if channels <= 2:
        raise ValueError("Routing requires more than two hardware outputs")
    if not _valid(values) or len(values) != channels:
        raise ValueError("Assign one signal (0–4) to each available hardware output")
    return list(values)


def save_assignments(output_key: str, values: Any, channels: int) -> None:
    assignments = validate_assignments(values, channels)
    data = _load()
    previous = data.get(device_key(output_key))
    if _valid(previous):
        assignments.extend(previous[channels:])
    data[device_key(output_key)] = assignments
    _write(data)


def saved_routing_state(output_key: str) -> list[int] | None:
    values = _load().get(device_key(output_key))
    return list(values) if _valid(values) else None


def restore_routing_state(output_key: str, state: list[int] | None) -> None:
    data = _load()
    if _valid(state):
        data[device_key(output_key)] = list(state)
    else:
        data.pop(device_key(output_key), None)
    _write(data)


def _write(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.replace(path)


def output_route_pairs(mode: Mapping[str, Any], ports: Sequence[str]) -> tuple[tuple[int, str], ...]:
    """Resolve physical edges; Off and unavailable channels create no link."""
    assignments = (mode.get("output_routing") or {}).get("assignments")
    if not _valid(assignments):
        assignments = [1, 2, 3, 4]
    return tuple((signal, port) for signal, port in zip(assignments, ports) if signal)
