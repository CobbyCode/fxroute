"""Sink-Input-Kurzfassung und Aktivitätsfilter (REFACTOR-005-Extrakt).

Zustandsfreie Helfer rund um PipeWire-Sink-Input-Einträge, 1:1 aus
``main.py`` extrahiert. Keine Imports aus ``main`` oder anderen
Projektmodulen — nur stdlib.
"""

import re
import subprocess


def list_sink_inputs() -> list[dict]:
    """Query pactl and parse all sink-input entries (REFACTOR-011-Extrakt)."""
    try:
        completed = subprocess.run(["pactl", "list", "sink-inputs"], capture_output=True, text=True, check=False, timeout=1.5)
    except Exception:
        return []
    if completed.returncode != 0:
        return []

    entries: list[dict] = []
    current: dict | None = None
    in_properties = False
    for raw_line in completed.stdout.splitlines():
        if raw_line.startswith("Sink Input #"):
            if current:
                entries.append(current)
            current = {"id": raw_line.split("#", 1)[1].strip(), "properties": {}}
            in_properties = False
            continue
        if current is None:
            continue

        stripped = raw_line.strip()
        if stripped.startswith("Sample Specification:"):
            match = re.search(r"(\d+)\s*Hz\b", stripped)
            if match:
                try:
                    current["sample_rate"] = int(match.group(1))
                except ValueError:
                    pass
            continue
        if stripped.startswith("Sink:"):
            current["sink"] = stripped.split(":", 1)[1].strip()
            continue
        if stripped.startswith("Corked:"):
            current["corked"] = stripped.split(":", 1)[1].strip().lower() == "yes"
            continue
        if stripped.startswith("Mute:"):
            current["muted"] = stripped.split(":", 1)[1].strip().lower() == "yes"
            continue
        if stripped.startswith("Volume:"):
            match = re.search(r"/\s*(\d+)%\s*/", stripped)
            if match:
                try:
                    current["volume_percent"] = int(match.group(1))
                except ValueError:
                    pass
            continue
        if stripped == "Properties:":
            in_properties = True
            continue
        if not stripped:
            in_properties = False
            continue
        if in_properties:
            if " = " not in stripped:
                continue
            key, value = stripped.split(" = ", 1)
            current["properties"][key.strip()] = value.strip().strip('"')

    if current:
        entries.append(current)
    return entries


def brief_sink_inputs(entries: list[dict]) -> list[dict]:
    result = []
    for entry in entries:
        props = entry.get("properties") or {}
        result.append({
            "id": entry.get("id"),
            "sink": entry.get("sink"),
            "node": props.get("node.name"),
            "app": props.get("application.name") or props.get("application.id"),
            "media": props.get("media.name"),
            "rate": entry.get("sample_rate"),
            "corked": entry.get("corked"),
            "muted": entry.get("muted"),
            "volume_percent": entry.get("volume_percent"),
        })
    return result


def active_unmuted_sink_inputs(entries: list[dict]) -> list[dict]:
    return [
        entry for entry in entries
        if not entry.get("corked")
        and not entry.get("muted")
        and (entry.get("volume_percent") is None or int(entry.get("volume_percent")) > 0)
    ]
