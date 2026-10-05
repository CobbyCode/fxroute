# SPDX-License-Identifier: AGPL-3.0-only
"""Per-device custom input mapping: physical capture channels -> program positions.

One mapping per audio device, keyed by the card base (profile suffix
stripped) so it survives multichannel/pro-audio profile switches. Channels
are stored as 1-based capture-channel indices because the PipeWire channel
suffixes differ per profile.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Mapping

from audio.output_topology import SURROUND_POSITIONS
from common.atomic_write import atomic_write_text

logger = logging.getLogger(__name__)

POSITION_LABELS = {
    "FL": "Front L", "FR": "Front R", "FC": "Center", "LFE": "LFE",
    "SL": "Side L", "SR": "Side R", "BL": "Rear L", "BR": "Rear R",
}
REQUIRED_POSITIONS = ("FL", "FR")
INPUT_KEY_PREFIX = "custom-mapping::"
DEFAULT_LABEL = "Custom mapping"
MAX_NAME_LENGTH = 40
_NODE_PREFIXES = ("alsa_input.", "alsa_output.")


def storage_path() -> Path:
    config_root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return config_root / "fxroute" / "custom-input-mappings.json"


def card_base(node_name: str | None) -> str | None:
    """Device identity of an ALSA node: the name without prefix and profile."""
    name = str(node_name or "").strip()
    for prefix in _NODE_PREFIXES:
        if name.startswith(prefix):
            base, _, _profile = name[len(prefix):].rpartition(".")
            return base or None
    return None


def input_key(base: str) -> str:
    return f"{INPUT_KEY_PREFIX}{base}"


def card_base_from_key(key: str | None) -> str | None:
    text = str(key or "")
    if not text.startswith(INPUT_KEY_PREFIX):
        return None
    return text[len(INPUT_KEY_PREFIX):] or None


def label_for(mapping: Mapping[str, Any] | None) -> str:
    return str((mapping or {}).get("name") or "").strip() or DEFAULT_LABEL


def positions_payload() -> list[dict[str, str]]:
    return [{"id": position, "label": POSITION_LABELS[position]} for position in SURROUND_POSITIONS]


def validate(name: Any, channels: Any, capture_channel_count: int) -> dict[str, Any]:
    """Return the normalized mapping or raise ValueError with a user message."""
    if name is None:
        name = ""
    if not isinstance(name, str):
        raise ValueError("Name must be a string")
    name = name.strip()
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"Name must be at most {MAX_NAME_LENGTH} characters")
    if any(ord(char) < 32 for char in name):
        raise ValueError("Name must not contain control characters")
    if not isinstance(channels, dict):
        raise ValueError("channels must be an object mapping channel to input number")
    for position in channels:
        if position not in SURROUND_POSITIONS:
            raise ValueError(f"Unknown channel: {position}")
    normalized: dict[str, int] = {}
    seen: dict[int, str] = {}
    for position in SURROUND_POSITIONS:
        value = channels.get(position)
        if value is None:
            continue
        if type(value) is not int:
            raise ValueError(f"Input for {POSITION_LABELS[position]} must be a whole number")
        if not 1 <= value <= capture_channel_count:
            raise ValueError(
                f"Input {value} is not available: the device has {capture_channel_count} input channels"
            )
        if value in seen:
            raise ValueError(
                f"Input {value} is assigned to both {POSITION_LABELS[seen[value]]} and {POSITION_LABELS[position]}"
            )
        seen[value] = position
        normalized[position] = value
    missing = [POSITION_LABELS[position] for position in REQUIRED_POSITIONS if position not in normalized]
    if missing:
        raise ValueError(f"{' and '.join(missing)} must be mapped")
    return {"name": name, "channels": normalized}


def _sanitize(entry: Any) -> dict[str, Any] | None:
    """Stored entry as a mapping, or None when it is not usable."""
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    channels = entry.get("channels")
    if not isinstance(channels, dict):
        return None
    try:
        count = max((value for value in channels.values() if type(value) is int), default=0)
        return validate(name if isinstance(name, str) else "", channels, count)
    except ValueError:
        return None


def load_mappings() -> dict[str, dict[str, Any]]:
    """All stored mappings by card base. Never raises: unusable data is skipped."""
    try:
        payload = json.loads(storage_path().read_text())
    except (OSError, ValueError):
        return {}
    stored = payload.get("mappings") if isinstance(payload, dict) else None
    if not isinstance(stored, dict):
        return {}
    mappings: dict[str, dict[str, Any]] = {}
    for base, entry in stored.items():
        mapping = _sanitize(entry)
        if isinstance(base, str) and base and mapping is not None:
            mappings[base] = mapping
    return mappings


def _write(mappings: Mapping[str, Mapping[str, Any]]) -> None:
    atomic_write_text(storage_path(), json.dumps({"version": 1, "mappings": mappings}, indent=2) + "\n")


def save_mapping(base: str, mapping: Mapping[str, Any]) -> None:
    mappings = load_mappings()
    mappings[base] = dict(mapping)
    _write(mappings)


def delete_mapping(base: str) -> bool:
    mappings = load_mappings()
    if base not in mappings:
        return False
    del mappings[base]
    _write(mappings)
    return True
