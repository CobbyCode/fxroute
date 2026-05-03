"""Helpers for PipeWire samplerate status and conservative settings inventory."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any


NON_SELECTABLE_OUTPUT_KEYS = {"easyeffects_sink"}
NON_SELECTABLE_INPUT_KEYS = {"easyeffects_source"}
SOURCE_MODE_APP_PLAYBACK = "app-playback"
SOURCE_MODE_EXTERNAL_INPUT = "external-input"
SOURCE_MODE_BLUETOOTH_INPUT = "bluetooth-input"


def _run_command(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(stderr or f"Command failed: {' '.join(args)}")
    return result.stdout


def _command_available(command: str) -> bool:
    return any(
        Path(path, command).exists() and os.access(Path(path, command), os.X_OK)
        for path in os.environ.get("PATH", "").split(os.pathsep)
        if path
    )


def _pipewire_bluez_plugin_available() -> bool:
    candidates = [
        "/usr/lib64/spa-0.2/bluez5/libspa-bluez5.so",
        "/usr/lib/spa-0.2/bluez5/libspa-bluez5.so",
        "/usr/lib/*/spa-0.2/bluez5/libspa-bluez5.so",
    ]
    for candidate in candidates:
        if any(Path("/").glob(candidate.lstrip("/"))):
            return True
    return False


def _is_bluetooth_sink_name(name: str | None) -> bool:
    normalized = (name or "").strip()
    return normalized.startswith("bluez_output.")


def _is_bluetooth_source_name(name: str | None) -> bool:
    normalized = (name or "").strip()
    return normalized.startswith("bluez_input.") or normalized.startswith("bluez_source.")


def _extract_bluetooth_address(value: str | None) -> str | None:
    normalized = (value or "").strip()
    match = re.search(r"([0-9A-F]{2}(?:[:_][0-9A-F]{2}){5})", normalized, re.IGNORECASE)
    if not match:
        return None
    return match.group(1).replace("_", ":").upper()


def _bluetooth_device_id(address: str | None) -> str | None:
    normalized = _extract_bluetooth_address(address)
    if not normalized:
        return None
    return f"bluez-dev-{normalized.replace(':', '_')}"


def _parse_bluetoothctl_show(output: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "address": None,
        "name": None,
        "alias": None,
        "powered": None,
        "discoverable": None,
        "pairable": None,
        "discovering": None,
        "roles": [],
        "uuids": [],
    }

    for index, raw_line in enumerate(output.splitlines()):
        line = raw_line.strip()
        if index == 0:
            match = re.match(r"Controller\s+([0-9A-F:]{17})", line, re.IGNORECASE)
            if match:
                result["address"] = match.group(1).upper()
        elif line.startswith("Name:"):
            result["name"] = line.split(":", 1)[1].strip() or None
        elif line.startswith("Alias:"):
            result["alias"] = line.split(":", 1)[1].strip() or None
        elif line.startswith("Powered:"):
            result["powered"] = line.split(":", 1)[1].strip().lower() == "yes"
        elif line.startswith("Discoverable:"):
            result["discoverable"] = line.split(":", 1)[1].strip().lower() == "yes"
        elif line.startswith("Pairable:"):
            result["pairable"] = line.split(":", 1)[1].strip().lower() == "yes"
        elif line.startswith("Discovering:"):
            result["discovering"] = line.split(":", 1)[1].strip().lower() == "yes"
        elif line.startswith("UUID:"):
            uuid_label = line.split(":", 1)[1].strip()
            if uuid_label:
                result.setdefault("uuids", []).append(uuid_label)
        elif line.startswith("Roles:"):
            role = line.split(":", 1)[1].strip()
            if role:
                result.setdefault("roles", []).append(role)

    return result


def _parse_bluetoothctl_devices(output: str) -> list[dict[str, str]]:
    devices: list[dict[str, str]] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        match = re.match(r"Device\s+([0-9A-F:]{17})\s+(.+)$", line, re.IGNORECASE)
        if not match:
            continue
        address, name = match.groups()
        devices.append({
            "address": address.upper(),
            "name": name.strip(),
        })
    return devices


def _parse_bluetoothctl_info(output: str) -> dict[str, Any]:
    info: dict[str, Any] = {
        "address": None,
        "name": None,
        "alias": None,
        "paired": False,
        "trusted": False,
        "connected": False,
        "blocked": False,
        "rssi": None,
        "battery_percent": None,
        "uuids": [],
        "modalias": None,
    }

    for index, raw_line in enumerate(output.splitlines()):
        line = raw_line.strip()
        if index == 0:
            match = re.match(r"Device\s+([0-9A-F:]{17})\s+(.+)$", line, re.IGNORECASE)
            if match:
                info["address"] = match.group(1).upper()
                info["name"] = match.group(2).strip() or None
            continue
        if line.startswith("Name:"):
            info["name"] = line.split(":", 1)[1].strip() or None
        elif line.startswith("Alias:"):
            info["alias"] = line.split(":", 1)[1].strip() or None
        elif line.startswith("Paired:"):
            info["paired"] = line.split(":", 1)[1].strip().lower() == "yes"
        elif line.startswith("Trusted:"):
            info["trusted"] = line.split(":", 1)[1].strip().lower() == "yes"
        elif line.startswith("Connected:"):
            info["connected"] = line.split(":", 1)[1].strip().lower() == "yes"
        elif line.startswith("Blocked:"):
            info["blocked"] = line.split(":", 1)[1].strip().lower() == "yes"
        elif line.startswith("RSSI:"):
            info["rssi"] = _safe_int(line.split(":", 1)[1].strip())
        elif line.startswith("Battery Percentage:"):
            battery_value = line.split(":", 1)[1].strip().replace("%", "")
            info["battery_percent"] = _safe_int(battery_value)
        elif line.startswith("UUID:"):
            uuid_label = line.split(":", 1)[1].strip()
            if uuid_label:
                info.setdefault("uuids", []).append(uuid_label)
        elif line.startswith("Modalias:"):
            info["modalias"] = line.split(":", 1)[1].strip() or None

    return info


def _bluetooth_profile_from_node_name(name: str | None) -> str | None:
    normalized = (name or "").strip()
    if not normalized or "." not in normalized:
        return None
    suffix = normalized.rsplit(".", 1)[-1]
    return suffix or None


def _infer_bluetooth_codec(profile: str | None) -> str | None:
    normalized = (profile or "").strip().lower()
    if "ldac" in normalized:
        return "ldac"
    if "aac" in normalized:
        return "aac"
    if "aptx" in normalized:
        return "aptx"
    if normalized.startswith("a2dp"):
        return "a2dp"
    return None


def _parse_wpctl_status_bluetooth_streams(output: str) -> list[dict[str, Any]]:
    streams: list[dict[str, Any]] = []
    in_audio_streams = False
    current_stream: dict[str, Any] | None = None

    for raw_line in output.splitlines():
        stripped = raw_line.strip()
        if stripped == "Audio":
            in_audio_streams = False
            current_stream = None
            continue
        if "Streams:" in stripped and ("└" in raw_line or "├" in raw_line or stripped == "Streams:"):
            in_audio_streams = True
            current_stream = None
            continue
        if in_audio_streams and stripped in {"Video", "Settings"}:
            break
        if not in_audio_streams:
            continue

        stream_match = re.match(r"^[\s│├└─]*(\d+)\.\s+(bluez_(?:input|source|output)\.[^\s]+)", raw_line)
        if stream_match:
            current_stream = {
                "id": int(stream_match.group(1)),
                "name": stream_match.group(2),
                "active": False,
            }
            streams.append(current_stream)
            continue

        if current_stream and "[active]" in raw_line:
            current_stream["active"] = True

    return streams


def _parse_wpctl_inspect(output: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in output.splitlines():
        match = re.match(r'^\s*(?:\*\s+)?([A-Za-z0-9._-]+)\s*=\s*(.+?)\s*$', raw_line)
        if not match:
            continue
        key, value = match.groups()
        cleaned = value.strip()
        if cleaned.startswith('"') and cleaned.endswith('"') and len(cleaned) >= 2:
            cleaned = cleaned[1:-1]
        result[key] = cleaned
    return result


def _parse_fraction_rate(value: str | None) -> int | None:
    normalized = (value or "").strip()
    if not normalized:
        return None
    slash_match = re.search(r"/(\d+)$", normalized)
    if slash_match:
        return _safe_int(slash_match.group(1))
    hz_match = re.search(r"(\d+)\s*Hz$", normalized, re.IGNORECASE)
    if hz_match:
        return _safe_int(hz_match.group(1))
    return _safe_int(normalized)


def _parse_pw_metadata_settings(output: str) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "clock_rate": None,
        "force_rate": None,
        "allowed_rates": [],
    }

    for line in output.splitlines():
        match = re.search(r"key:'([^']+)' value:'([^']*)'", line)
        if not match:
            continue
        key, value = match.groups()
        if key == "clock.rate":
            settings["clock_rate"] = _safe_int(value)
        elif key == "clock.force-rate":
            settings["force_rate"] = _safe_int(value)
        elif key == "clock.allowed-rates":
            settings["allowed_rates"] = [int(item) for item in re.findall(r"\d+", value)]

    return settings


def _parse_default_sink(output: str) -> dict[str, Any]:
    sink: dict[str, Any] = {"id": None, "name": None, "description": None}
    first_line = output.splitlines()[0] if output.splitlines() else ""
    id_match = re.search(r"id\s+(\d+),", first_line)
    if id_match:
        sink["id"] = int(id_match.group(1))

    for line in output.splitlines():
        line = line.strip()
        if line.startswith("* node.name = "):
            sink["name"] = _strip_quoted_value(line)
        elif line.startswith("* node.description = "):
            sink["description"] = _strip_quoted_value(line)

    return sink


def _parse_active_rate(output: str) -> int | None:
    match = re.search(r"Audio:rate.*?\n\s+Int\s+(\d+)", output, re.DOTALL)
    if match:
        return int(match.group(1))
    return None


def _parse_pactl_sinks_short(output: str) -> list[dict[str, Any]]:
    sinks: list[dict[str, Any]] = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        sample_spec = parts[3].strip()
        rate_match = re.search(r"(\d+)Hz", sample_spec)
        sinks.append({
            "id": _safe_int(parts[0].strip()),
            "name": parts[1].strip(),
            "driver": parts[2].strip(),
            "sample_spec": sample_spec,
            "active_rate": int(rate_match.group(1)) if rate_match else None,
            "state": parts[4].strip().upper(),
        })
    return sinks


def _parse_pactl_sources_short(output: str) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        sample_spec = parts[3].strip()
        rate_match = re.search(r"(\d+)Hz", sample_spec)
        sources.append({
            "id": _safe_int(parts[0].strip()),
            "name": parts[1].strip(),
            "driver": parts[2].strip(),
            "sample_spec": sample_spec,
            "active_rate": int(rate_match.group(1)) if rate_match else None,
            "state": parts[4].strip().upper(),
        })
    return sources


def _parse_pactl_sinks_detailed(output: str) -> dict[str, dict[str, Any]]:
    sinks: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    in_ports = False

    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.startswith('Sink #'):
            if current and current.get('name'):
                sinks[current['name']] = current
            current = {
                'description': None,
                'device_description': None,
                'sample_spec': None,
                'state': None,
                'ports': [],
                'active_port': None,
            }
            in_ports = False
            continue

        if current is None:
            continue

        if stripped.startswith('Name:'):
            current['name'] = stripped.split(':', 1)[1].strip()
            in_ports = False
        elif stripped.startswith('Description:'):
            current['description'] = stripped.split(':', 1)[1].strip()
            in_ports = False
        elif stripped.startswith('State:'):
            current['state'] = stripped.split(':', 1)[1].strip().upper()
            in_ports = False
        elif stripped.startswith('Sample Specification:'):
            current['sample_spec'] = stripped.split(':', 1)[1].strip()
            in_ports = False
        elif stripped.startswith('device.description = '):
            current['device_description'] = _strip_quoted_value(stripped)
        elif stripped == 'Ports:':
            in_ports = True
        elif stripped.startswith('Active Port:'):
            current['active_port'] = stripped.split(':', 1)[1].strip() or None
            in_ports = False
        elif stripped == 'Formats:':
            in_ports = False
        elif in_ports and line.startswith('\t\t'):
            port_match = re.match(r'([^:]+):\s+(.+?)\s+\((.*)\)$', stripped)
            if port_match:
                port_key, port_label, port_meta = port_match.groups()
                unavailable = 'not available' in port_meta.lower()
                current['ports'].append({
                    'key': port_key.strip(),
                    'label': port_label.strip(),
                    'available': not unavailable,
                })

    if current and current.get('name'):
        sinks[current['name']] = current

    return sinks


def _parse_pactl_cards_detailed(output: str) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    current_port: dict[str, Any] | None = None
    in_ports = False

    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.startswith('Card #'):
            if current_port and current is not None:
                current.setdefault('ports', []).append(current_port)
                current_port = None
            if current:
                cards.append(current)
            current = {
                'name': None,
                'device_description': None,
                'ports': [],
            }
            in_ports = False
            continue

        if current is None:
            continue

        if stripped.startswith('Name:'):
            current['name'] = stripped.split(':', 1)[1].strip()
            in_ports = False
        elif stripped.startswith('device.description = '):
            current['device_description'] = _strip_quoted_value(stripped)
        elif stripped == 'Ports:':
            in_ports = True
            if current_port:
                current['ports'].append(current_port)
                current_port = None
        elif stripped.startswith('Active Profile:') or stripped == 'Profiles:' or stripped == 'Properties:' or stripped == 'Formats:':
            in_ports = False
            if current_port:
                current['ports'].append(current_port)
                current_port = None
        elif in_ports and line.startswith('\t\t') and not line.startswith('\t\t\t'):
            if current_port:
                current['ports'].append(current_port)
            port_match = re.match(r'([^:]+):\s+(.+?)\s+\((.*)\)$', stripped)
            if port_match:
                port_key, port_label, port_meta = port_match.groups()
                meta_lower = port_meta.lower()
                current_port = {
                    'key': port_key.strip(),
                    'label': port_label.strip(),
                    'available': 'not available' not in meta_lower,
                    'profiles': [],
                }
            else:
                current_port = None
        elif in_ports and current_port and line.startswith('\t\t\tPart of profile(s):'):
            profiles_value = stripped.split(':', 1)[1].strip()
            current_port['profiles'] = [item.strip() for item in profiles_value.split(',') if item.strip()]

    if current_port and current is not None:
        current.setdefault('ports', []).append(current_port)
    if current:
        cards.append(current)

    return cards


def _parse_pactl_sources_detailed(output: str) -> dict[str, dict[str, Any]]:
    sources: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    in_ports = False

    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.startswith('Source #'):
            if current and current.get('name'):
                sources[current['name']] = current
            current = {
                'description': None,
                'device_description': None,
                'sample_spec': None,
                'state': None,
                'ports': [],
                'active_port': None,
            }
            in_ports = False
            continue

        if current is None:
            continue

        if stripped.startswith('Name:'):
            current['name'] = stripped.split(':', 1)[1].strip()
            in_ports = False
        elif stripped.startswith('Description:'):
            current['description'] = stripped.split(':', 1)[1].strip()
            in_ports = False
        elif stripped.startswith('State:'):
            current['state'] = stripped.split(':', 1)[1].strip().upper()
            in_ports = False
        elif stripped.startswith('Sample Specification:'):
            current['sample_spec'] = stripped.split(':', 1)[1].strip()
            in_ports = False
        elif stripped.startswith('device.description = '):
            current['device_description'] = _strip_quoted_value(stripped)
        elif stripped == 'Ports:':
            in_ports = True
        elif stripped.startswith('Active Port:'):
            current['active_port'] = stripped.split(':', 1)[1].strip() or None
            in_ports = False
        elif stripped == 'Formats:':
            in_ports = False
        elif in_ports and line.startswith('\t\t'):
            port_match = re.match(r'([^:]+):\s+(.+?)\s+\((.*)\)$', stripped)
            if port_match:
                port_key, port_label, port_meta = port_match.groups()
                unavailable = 'not available' in port_meta.lower()
                current['ports'].append({
                    'key': port_key.strip(),
                    'label': port_label.strip(),
                    'available': not unavailable,
                })

    if current and current.get('name'):
        sources[current['name']] = current

    return sources


def _humanize_sink_name(name: str | None) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        return "Unknown output"
    cleaned = cleaned.replace("alsa_output.", "")
    cleaned = cleaned.replace("bluez_output.", "")
    cleaned = cleaned.replace(".analog-stereo", "")
    cleaned = cleaned.replace(".digital-stereo", "")
    cleaned = cleaned.replace(".iec958-stereo", "")
    cleaned = cleaned.replace(".hdmi-stereo", "")
    cleaned = cleaned.replace("_", " ")
    cleaned = re.sub(r"\.(pro|output|sink)$", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or (name or "Unknown output")


def _normalize_output_label(label: str | None) -> str:
    cleaned = (label or '').lower()
    cleaned = cleaned.replace('displayport', 'display port')
    cleaned = re.sub(r'\boutput\b', ' ', cleaned)
    cleaned = re.sub(r'\bdevice\b', ' ', cleaned)
    cleaned = re.sub(r'[^a-z0-9]+', ' ', cleaned)
    return re.sub(r'\s+', ' ', cleaned).strip()


def _prefer_output_port_label(port_label: str | None, fallback_label: str | None) -> str | None:
    cleaned_port = (port_label or '').strip()
    if not cleaned_port:
        return fallback_label
    lowered = cleaned_port.lower()
    if 'headphone' in lowered:
        return 'Headphones'
    if 'hdmi' in lowered or 'displayport' in lowered or 'display port' in lowered:
        return 'HDMI'
    if cleaned_port.lower() in {'analog output', 'line out', 'speaker'}:
        return fallback_label or cleaned_port
    return cleaned_port


def _build_sink_output_label(name: str | None, details: dict[str, Any], default_label: str | None = None) -> str:
    fallback_label = (
        details.get('description')
        or details.get('device_description')
        or default_label
        or _humanize_sink_name(name)
    )
    active_port = details.get('active_port')
    port_label = None
    for port in details.get('ports') or []:
        if port.get('key') == active_port:
            port_label = port.get('label')
            break
    return _prefer_output_port_label(port_label, fallback_label) or fallback_label


def _build_card_port_output_entries(cards: list[dict[str, Any]], existing_outputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    existing_labels = {
        _normalize_output_label(item.get('label') or item.get('name'))
        for item in existing_outputs
        if item.get('label') or item.get('name')
    }
    inferred_outputs: list[dict[str, Any]] = []

    for card in cards:
        card_name = card.get('name')
        card_device_label = card.get('device_description') or 'Audio device'
        for port in card.get('ports') or []:
            port_key = port.get('key') or ''
            profiles = port.get('profiles') or []
            if 'output' not in port_key and not any(profile.startswith('output:') for profile in profiles):
                continue
            label = _prefer_output_port_label(port.get('label'), card_device_label) or card_device_label
            normalized_label = _normalize_output_label(label)
            if not normalized_label or normalized_label in existing_labels:
                continue
            existing_labels.add(normalized_label)
            inferred_outputs.append({
                'id': None,
                'key': f"card-port::{card_name or 'unknown'}::{port_key}",
                'name': None,
                'label': label,
                'sample_spec': None,
                'active_rate': None,
                'state': 'AVAILABLE' if port.get('available', True) else 'UNAVAILABLE',
                'is_default': False,
                'is_current': False,
                'is_selected': False,
                'selectable': False,
                'inventory_source': 'card-port',
            })

    return inferred_outputs


def _humanize_source_name(name: str | None) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        return "Unknown input"
    cleaned = cleaned.replace("alsa_input.", "")
    cleaned = cleaned.replace("bluez_input.", "")
    cleaned = cleaned.replace("source.", "")
    cleaned = cleaned.replace(".analog-stereo", "")
    cleaned = cleaned.replace(".digital-stereo", "")
    cleaned = cleaned.replace(".iec958-stereo", "")
    cleaned = cleaned.replace(".mono-fallback", "")
    cleaned = cleaned.replace("_", " ")
    cleaned = re.sub(r"\.input$", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or (name or "Unknown input")



def _audio_output_selection_path() -> Path:
    config_root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return config_root / "fxroute" / "audio-output-selection.json"



def _audio_source_selection_path() -> Path:
    config_root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return config_root / "fxroute" / "audio-source-selection.json"


def _load_audio_output_selection() -> dict[str, Any]:
    path = _audio_output_selection_path()
    if not path.exists():
        return {"selected_key": None}
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return {"selected_key": None}
    selected_key = payload.get("selected_key")
    return {
        "selected_key": selected_key if isinstance(selected_key, str) and selected_key else None,
    }


def _load_audio_source_selection() -> dict[str, Any]:
    path = _audio_source_selection_path()
    if not path.exists():
        return {"mode": SOURCE_MODE_APP_PLAYBACK, "selected_input_key": None}
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return {"mode": SOURCE_MODE_APP_PLAYBACK, "selected_input_key": None}
    mode = payload.get("mode")
    selected_input_key = payload.get("selected_input_key")
    return {
        "mode": mode if mode in {SOURCE_MODE_APP_PLAYBACK, SOURCE_MODE_EXTERNAL_INPUT, SOURCE_MODE_BLUETOOTH_INPUT} else SOURCE_MODE_APP_PLAYBACK,
        "selected_input_key": selected_input_key if isinstance(selected_input_key, str) and selected_input_key else None,
    }


def _save_audio_output_selection(selected_key: str) -> None:
    path = _audio_output_selection_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "selected_key": selected_key,
    }, indent=2) + "\n")


def _save_audio_source_selection(mode: str, selected_input_key: str | None) -> None:
    path = _audio_source_selection_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "mode": mode,
        "selected_input_key": selected_input_key,
    }, indent=2) + "\n")


def set_bluetooth_receiver_enabled(enabled: bool) -> dict[str, Any]:
    if not _command_available("bluetoothctl"):
        raise RuntimeError("bluetoothctl is not installed or not available in PATH")

    commands = [["bluetoothctl", "power", "on"]] if enabled else []
    commands.extend([
        ["bluetoothctl", "pairable", "on" if enabled else "off"],
        ["bluetoothctl", "discoverable", "on" if enabled else "off"],
    ])

    failures: list[str] = []
    for command in commands:
        try:
            _run_command(command)
        except Exception as exc:
            failures.append(f"{' '.join(command[1:])}: {exc}")

    if failures:
        raise RuntimeError("; ".join(failures))

    return get_bluetooth_audio_overview()


def disconnect_connected_bluetooth_audio_sources() -> list[str]:
    if not _command_available("bluetoothctl"):
        raise RuntimeError("bluetoothctl is not installed or not available in PATH")

    disconnected: list[str] = []
    failures: list[str] = []
    for item in _parse_bluetoothctl_devices(_run_command(["bluetoothctl", "devices"])):
        address = item.get("address")
        if not address:
            continue
        try:
            info = _parse_bluetoothctl_info(_run_command(["bluetoothctl", "info", address]))
        except Exception as exc:
            failures.append(f"info {address}: {exc}")
            continue
        uuids = list(info.get("uuids") or [])
        is_audio_source = any("Audio Source" in uuid for uuid in uuids)
        if not info.get("connected") or not is_audio_source:
            continue
        try:
            _run_command(["bluetoothctl", "disconnect", address])
            disconnected.append(address)
        except Exception as exc:
            failures.append(f"disconnect {address}: {exc}")

    if failures and not disconnected:
        raise RuntimeError("; ".join(failures))
    return disconnected


def _set_default_sink(name: str) -> None:
    _run_command(["pactl", "set-default-sink", name])


def _parse_default_source_name(output: str) -> str | None:
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("Default Source:"):
            value = stripped.split(":", 1)[1].strip()
            return value or None
    return None


def _build_source_selection_key(source_name: str, port_key: str | None = None) -> str:
    return f"{source_name}::{port_key}" if port_key else source_name


def _split_source_selection_key(selection_key: str | None) -> tuple[str | None, str | None]:
    normalized = (selection_key or '').strip()
    if not normalized:
        return None, None
    if '::' in normalized:
        source_name, port_key = normalized.split('::', 1)
        return source_name or None, port_key or None
    return normalized, None


def _set_source_port(source_name: str, port_key: str) -> None:
    _run_command(["pactl", "set-source-port", source_name, port_key])


def _build_selected_output_payload(selected_key: str | None, current_name: str | None, explicit_outputs: list[dict[str, Any]]) -> dict[str, Any] | None:
    lookup_key = selected_key or current_name
    selected_output = next((item for item in explicit_outputs if item.get("key") == lookup_key), None)
    if selected_output:
        return {
            "key": selected_output.get("key"),
            "label": selected_output.get("label") or selected_output.get("name") or "Unknown output",
            "target_name": selected_output.get("name"),
            "target_label": selected_output.get("label") or selected_output.get("name") or "Unknown output",
            "is_default": selected_output.get("is_default", False),
        }
    return None


def get_bluetooth_audio_overview() -> dict[str, Any]:
    notes: list[str] = []
    selection_state = _load_audio_source_selection()
    receiver_enabled_intent = selection_state.get("mode") == SOURCE_MODE_BLUETOOTH_INPUT
    bluetoothctl_available = _command_available("bluetoothctl")
    pactl_available = _command_available("pactl")
    pw_cli_available = _command_available("pw-cli")
    wpctl_available = _command_available("wpctl")

    controller: dict[str, Any] | None = None
    adapter_present = False
    if bluetoothctl_available:
        try:
            controller = _parse_bluetoothctl_show(_run_command(["bluetoothctl", "show"]))
            adapter_present = bool(controller.get("address"))
        except Exception as exc:
            notes.append(f"Bluetooth adapter status unavailable: {exc}")
    else:
        notes.append("bluetoothctl is not installed or not available in PATH.")

    sources: list[dict[str, Any]] = []
    sinks: list[dict[str, Any]] = []
    source_details: dict[str, dict[str, Any]] = {}
    sink_details: dict[str, dict[str, Any]] = {}
    if pactl_available:
        try:
            sources = _parse_pactl_sources_short(_run_command(["pactl", "list", "sources", "short"]))
        except Exception as exc:
            notes.append(f"Bluetooth source inventory unavailable: {exc}")
        try:
            sinks = _parse_pactl_sinks_short(_run_command(["pactl", "list", "sinks", "short"]))
        except Exception as exc:
            notes.append(f"Bluetooth sink inventory unavailable: {exc}")
        try:
            source_details = _parse_pactl_sources_detailed(_run_command(["pactl", "list", "sources"]))
        except Exception as exc:
            notes.append(f"Bluetooth source details unavailable: {exc}")
        try:
            sink_details = _parse_pactl_sinks_detailed(_run_command(["pactl", "list", "sinks"]))
        except Exception as exc:
            notes.append(f"Bluetooth sink details unavailable: {exc}")
    else:
        notes.append("pactl is not installed or not available in PATH.")

    wpctl_bluetooth_streams: list[dict[str, Any]] = []
    if wpctl_available:
        try:
            wpctl_bluetooth_streams = _parse_wpctl_status_bluetooth_streams(_run_command(["wpctl", "status"]))
        except Exception as exc:
            notes.append(f"Bluetooth PipeWire stream inventory unavailable: {exc}")

    source_names = {str(source.get("name") or "") for source in sources}
    sink_names = {str(sink.get("name") or "") for sink in sinks}
    for stream in wpctl_bluetooth_streams:
        stream_name = str(stream.get("name") or "").strip()
        if not stream_name:
            continue
        try:
            inspect = _parse_wpctl_inspect(_run_command(["wpctl", "inspect", str(stream.get("id"))]))
        except Exception:
            inspect = {}
        detail_payload = {
            "description": inspect.get("node.description") or inspect.get("media.name") or _humanize_source_name(stream_name),
            "device_description": inspect.get("node.description") or inspect.get("media.name") or _humanize_source_name(stream_name),
            "active_codec": inspect.get("api.bluez5.codec"),
            "profile": inspect.get("api.bluez5.profile"),
            "address": inspect.get("api.bluez5.address"),
            "active_rate": _parse_fraction_rate(inspect.get("node.rate")) or _parse_fraction_rate(inspect.get("node.latency")),
        }
        if _is_bluetooth_source_name(stream_name) and stream_name not in source_names:
            sources.append({
                "id": stream.get("id"),
                "name": stream_name,
                "state": "RUNNING" if stream.get("active") else "IDLE",
            })
            source_details[stream_name] = detail_payload
            source_names.add(stream_name)
        elif _is_bluetooth_sink_name(stream_name) and stream_name not in sink_names:
            sinks.append({
                "id": stream.get("id"),
                "name": stream_name,
                "state": "RUNNING" if stream.get("active") else "IDLE",
            })
            sink_details[stream_name] = detail_payload
            sink_names.add(stream_name)
        elif _is_bluetooth_source_name(stream_name):
            source_details.setdefault(stream_name, detail_payload)
        elif _is_bluetooth_sink_name(stream_name):
            sink_details.setdefault(stream_name, detail_payload)

    bluez_device_available = False
    if pw_cli_available:
        try:
            bluez_device_available = "api.bluez5" in _run_command(["pw-cli", "ls", "Device"])
        except Exception:
            bluez_device_available = False

    bt_sources = [source for source in sources if _is_bluetooth_source_name(source.get("name"))]
    bt_sinks = [sink for sink in sinks if _is_bluetooth_sink_name(sink.get("name"))]
    controller_uuids = [str(uuid) for uuid in (controller or {}).get("uuids") or []]
    can_receive_audio = any("Audio Sink" in uuid for uuid in controller_uuids)
    can_send_audio = any("Audio Source" in uuid for uuid in controller_uuids)
    pipewire_bluetooth_available = bool(
        bluez_device_available
        or bt_sources
        or bt_sinks
        or _pipewire_bluez_plugin_available()
    )

    device_seed_output = ""
    if bluetoothctl_available:
        try:
            paired_output = _run_command(["bluetoothctl", "devices", "Paired"])
            all_output = _run_command(["bluetoothctl", "devices"])
            device_seed_output = "\n".join(filter(None, [paired_output, all_output]))
        except Exception as exc:
            notes.append(f"Bluetooth device list unavailable: {exc}")

    devices_by_address: dict[str, dict[str, Any]] = {}
    for item in _parse_bluetoothctl_devices(device_seed_output):
        devices_by_address[item["address"]] = {
            "id": _bluetooth_device_id(item.get("address")),
            "address": item.get("address"),
            "name": item.get("name"),
            "alias": item.get("name"),
            "transport": "bluetooth",
            "paired": False,
            "trusted": False,
            "connected": False,
            "connection_state": "disconnected",
            "rssi": None,
            "battery_percent": None,
            "roles": {
                "can_stream_to_fxroute": False,
                "can_receive_from_fxroute": False,
                "can_remote_control": False,
                "metadata_capable": False,
            },
            "profiles": [],
            "active_profile": None,
            "supported_codecs": [],
            "active_codec": None,
            "session": None,
            "output_binding": None,
            "notes": [],
        }

    for address, device in list(devices_by_address.items()):
        try:
            info = _parse_bluetoothctl_info(_run_command(["bluetoothctl", "info", address]))
        except Exception:
            continue
        device.update({
            "name": info.get("name") or device.get("name"),
            "alias": info.get("alias") or device.get("alias") or info.get("name") or device.get("name"),
            "paired": bool(info.get("paired")),
            "trusted": bool(info.get("trusted")),
            "connected": bool(info.get("connected")),
            "connection_state": "connected" if info.get("connected") else "disconnected",
            "rssi": info.get("rssi"),
            "battery_percent": info.get("battery_percent"),
            "profiles": list(info.get("uuids") or []),
        })
        device["roles"] = {
            "can_stream_to_fxroute": any("Audio Source" in uuid for uuid in device.get("profiles") or []),
            "can_receive_from_fxroute": any("Audio Sink" in uuid for uuid in device.get("profiles") or []),
            "can_remote_control": any("A/V Remote Control" in uuid for uuid in device.get("profiles") or []),
            "metadata_capable": any("A/V Remote Control" in uuid for uuid in device.get("profiles") or []),
        }

    receiver_session = None
    for source in bt_sources:
        source_name = source.get("name")
        details = source_details.get(source_name or "", {})
        address = _extract_bluetooth_address(details.get("address") or source_name)
        device_id = _bluetooth_device_id(address)
        profile = details.get("profile") or _bluetooth_profile_from_node_name(source_name)
        device = devices_by_address.get(address or "")
        label = details.get("description") or details.get("device_description") or _humanize_source_name(source_name)
        active_codec = details.get("active_codec") or _infer_bluetooth_codec(profile)
        session_payload = {
            "active": True,
            "source_name": source_name,
            "device_id": device_id,
            "device_name": (device or {}).get("alias") or label,
            "mode": SOURCE_MODE_BLUETOOTH_INPUT,
            "streaming": str(source.get("state") or "").upper() == "RUNNING",
            "profile": profile,
            "active_codec": active_codec,
            "active_rate": source.get("active_rate") or details.get("active_rate"),
            "sample_spec": details.get("sample_spec") or source.get("sample_spec"),
            "controllable": bool((device or {}).get("roles", {}).get("can_remote_control")),
            "metadata_capable": bool((device or {}).get("roles", {}).get("metadata_capable")),
            "metadata": None,
            "controls": {
                "play_pause": False,
                "next": False,
                "previous": False,
                "volume": False,
            },
        }
        if device:
            device["connected"] = True
            device["connection_state"] = "connected"
            device["active_profile"] = profile
            device["active_codec"] = active_codec
            device["active_rate"] = session_payload["active_rate"]
            device["session"] = {
                "mode": SOURCE_MODE_BLUETOOTH_INPUT,
                "streaming": session_payload["streaming"],
                "source_name": source_name,
                "active_rate": session_payload["active_rate"],
            }
        if receiver_session is None or session_payload["streaming"]:
            receiver_session = session_payload

    for sink in bt_sinks:
        sink_name = sink.get("name")
        details = sink_details.get(sink_name or "", {})
        address = _extract_bluetooth_address(details.get("address") or sink_name)
        device = devices_by_address.get(address or "")
        profile = details.get("profile") or _bluetooth_profile_from_node_name(sink_name)
        if device:
            device["connected"] = True
            device["connection_state"] = "connected"
            device["active_profile"] = profile
            device["active_codec"] = details.get("active_codec") or _infer_bluetooth_codec(profile)
            device["output_binding"] = {
                "key": sink_name,
                "label": details.get("description") or details.get("device_description") or _humanize_sink_name(sink_name),
            }

    receiver_selectable = bool(bluetoothctl_available and adapter_present and pipewire_bluetooth_available and can_receive_audio)
    connected_receiver_devices = [
        device
        for device in devices_by_address.values()
        if device.get("connected") and bool((device.get("roles") or {}).get("can_stream_to_fxroute"))
    ]

    receiver_state = "unavailable"
    if receiver_selectable:
        receiver_state = "discoverable" if (controller or {}).get("discoverable") else "idle"
    if receiver_session and receiver_session.get("streaming"):
        receiver_state = "streaming"
    elif receiver_session or connected_receiver_devices:
        receiver_state = "connected"

    devices = sorted(devices_by_address.values(), key=lambda item: ((not item.get("connected")), (item.get("alias") or item.get("name") or "").lower()))

    return {
        "available": bool(bluetoothctl_available and adapter_present),
        "stack": {
            "bluez_available": bluetoothctl_available,
            "pipewire_bluetooth_available": pipewire_bluetooth_available,
            "wireplumber_available": _command_available("wpctl"),
            "adapter_present": adapter_present,
            "adapter_powered": (controller or {}).get("powered"),
            "adapter_alias": (controller or {}).get("alias") or (controller or {}).get("name"),
            "adapter_address": (controller or {}).get("address"),
        },
        "roles": {
            "bluetooth_input": {
                "available": bool(can_receive_audio),
                "selectable": receiver_selectable,
                "enabled": bool(receiver_enabled_intent),
                "discoverable": bool((controller or {}).get("discoverable")),
                "pairable": bool((controller or {}).get("pairable")),
                "state": receiver_state,
                "active_session": receiver_session,
                "supported_codecs": [],
                "notes": [] if receiver_selectable else ["Bluetooth receiver mode is not currently available on this host."],
            },
            "bluetooth_output": {
                "available": bool(can_send_audio),
                "selectable": bool(bt_sinks),
                "enabled": bool(bt_sinks),
                "active_device_key": next((sink.get("name") for sink in bt_sinks if str(sink.get("state") or "").upper() == "RUNNING"), None),
                "notes": [],
            },
        },
        "devices": devices,
        "receiver_session": receiver_session,
        "notes": notes,
    }


def get_audio_output_overview() -> dict[str, Any]:
    status = get_samplerate_status()
    bluetooth_overview = get_bluetooth_audio_overview()
    default_sink = status.get("sink") or {"id": None, "name": None, "description": None}
    relevant_sink = status.get("relevant_sink") or {}
    selection_state = _load_audio_output_selection()

    sinks: list[dict[str, Any]] = []
    sink_details: dict[str, dict[str, Any]] = {}
    notes = list(status.get("notes") or [])
    try:
        pactl_sinks_output = _run_command(["pactl", "list", "sinks", "short"])
        sinks = _parse_pactl_sinks_short(pactl_sinks_output)
    except Exception as exc:
        notes.append(f"Output list unavailable: {exc}")

    try:
        sink_details = _parse_pactl_sinks_detailed(_run_command(["pactl", "list", "sinks"]))
    except Exception as exc:
        notes.append(f"Output details unavailable: {exc}")

    default_name = default_sink.get("name")
    current_name = relevant_sink.get("name") or default_name
    default_label = default_sink.get("description") or _humanize_sink_name(default_name)
    selected_key = selection_state.get("selected_key")

    explicit_outputs = []
    for sink in sinks:
        name = sink.get("name")
        details = sink_details.get(name or "", {})
        label = _build_sink_output_label(name, details, default_label if name == default_name and default_label else None)
        profile = _bluetooth_profile_from_node_name(name)
        explicit_outputs.append({
            "id": sink.get("id"),
            "key": name,
            "name": name,
            "label": label,
            "sample_spec": details.get("sample_spec") or sink.get("sample_spec"),
            "active_rate": sink.get("active_rate"),
            "state": details.get("state") or sink.get("state"),
            "is_default": name == default_name,
            "is_current": name == current_name,
            "is_selected": name == selected_key,
            "selectable": name not in NON_SELECTABLE_OUTPUT_KEYS,
            "inventory_source": "sink",
            "transport": "bluetooth" if _is_bluetooth_sink_name(name) else "local",
            "device_class": "bluetooth_output" if _is_bluetooth_sink_name(name) else "local_output",
            "profile": profile,
            "active_codec": _infer_bluetooth_codec(profile) if _is_bluetooth_sink_name(name) else None,
            "pairing_required": False if _is_bluetooth_sink_name(name) else None,
            "connection_state": "connected" if _is_bluetooth_sink_name(name) else None,
            "controllable": False if _is_bluetooth_sink_name(name) else None,
            "metadata_capable": False if _is_bluetooth_sink_name(name) else None,
        })

    if selected_key and not any(item.get("key") == selected_key for item in explicit_outputs):
        notes.append(f"Saved output selection {selected_key} is not currently available.")
        selected_key = None

    current_output = next((item for item in explicit_outputs if item.get("is_current")), None)
    selected_output = _build_selected_output_payload(selected_key, current_name, explicit_outputs)

    return {
        "available": bool(status.get("available")),
        "default_output": {
            "key": default_name,
            "label": default_label,
            "target_name": default_name,
            "target_label": default_label,
            "is_selected": bool(default_name and selected_key == default_name),
        },
        "selected_output": selected_output,
        "current_output": current_output,
        "outputs": explicit_outputs,
        "bluetooth": {
            "available": bluetooth_overview.get("available", False),
            "device_count": len(bluetooth_overview.get("devices") or []),
            "output_available": ((bluetooth_overview.get("roles") or {}).get("bluetooth_output") or {}).get("available", False),
        },
        "notes": notes,
    }


def get_audio_source_overview() -> dict[str, Any]:
    selection_state = _load_audio_source_selection()
    bluetooth_overview = get_bluetooth_audio_overview()
    notes: list[str] = []

    try:
        default_source_name = _parse_default_source_name(_run_command(["pactl", "info"]))
    except Exception as exc:
        default_source_name = None
        notes.append(f"Default input unavailable: {exc}")

    try:
        sources = _parse_pactl_sources_short(_run_command(["pactl", "list", "sources", "short"]))
    except Exception as exc:
        sources = []
        notes.append(f"Input list unavailable: {exc}")

    try:
        source_details = _parse_pactl_sources_detailed(_run_command(["pactl", "list", "sources"]))
    except Exception as exc:
        source_details = {}
        notes.append(f"Input details unavailable: {exc}")

    selected_input_key = selection_state.get("selected_input_key")
    selected_source_name, selected_port_key = _split_source_selection_key(selected_input_key)
    inputs = []
    for source in sources:
        name = source.get("name")
        details = source_details.get(name or "", {})
        if name and (name.endswith(".monitor") or name in NON_SELECTABLE_INPUT_KEYS):
            continue
        device_label = (
            details.get("description")
            or details.get("device_description")
            or _humanize_source_name(name)
        )
        ports = [port for port in (details.get("ports") or []) if port.get("available", True)]
        active_port_key = details.get("active_port")
        base_payload = {
            "id": source.get("id"),
            "name": name,
            "device_label": device_label,
            "sample_spec": details.get("sample_spec") or source.get("sample_spec"),
            "active_rate": source.get("active_rate"),
            "state": details.get("state") or source.get("state"),
            "is_default": name == default_source_name,
            "selectable": True,
        }
        if ports:
            for port in ports:
                port_key = port.get("key")
                port_label = port.get("label") or device_label
                label = f"{port_label} — {device_label}" if port_label and port_label != device_label else device_label
                inputs.append({
                    **base_payload,
                    "key": _build_source_selection_key(name or '', port_key),
                    "source_key": name,
                    "port_key": port_key,
                    "port_label": port_label,
                    "label": label,
                    "is_active_port": port_key == active_port_key,
                    "is_selected": name == selected_source_name and port_key == selected_port_key,
                })
        else:
            inputs.append({
                **base_payload,
                "key": name,
                "source_key": name,
                "port_key": None,
                "port_label": None,
                "label": device_label,
                "is_active_port": True,
                "is_selected": name == selected_source_name and not selected_port_key,
            })

    if selected_input_key and not any(item.get("key") == selected_input_key for item in inputs):
        migrated_input = next((item for item in inputs if item.get("source_key") == selected_source_name), None)
        if migrated_input:
            selected_input_key = migrated_input.get("key")
            selected_source_name, selected_port_key = _split_source_selection_key(selected_input_key)
        else:
            notes.append(f"Saved input selection {selected_input_key} is not currently available.")
            selected_input_key = None
            selected_source_name = None
            selected_port_key = None

    default_input = next((item for item in inputs if item.get("is_default") and item.get("is_active_port")), None)
    selected_input = next((item for item in inputs if item.get("key") == selected_input_key), None)
    current_input = selected_input or next((item for item in inputs if item.get("is_active_port")), None) or default_input
    mode = selection_state.get("mode") or SOURCE_MODE_APP_PLAYBACK
    if mode == SOURCE_MODE_EXTERNAL_INPUT and not inputs:
        mode = SOURCE_MODE_APP_PLAYBACK
        notes.append("No real external inputs detected; staying on App playback.")
    if mode == SOURCE_MODE_BLUETOOTH_INPUT:
        bt_input_role = ((bluetooth_overview.get("roles") or {}).get("bluetooth_input") or {})
        if not bt_input_role.get("selectable"):
            mode = SOURCE_MODE_APP_PLAYBACK
            notes.append("Bluetooth input is not currently available on this host; staying on App playback.")

    return {
        "mode": mode,
        "modes": [
            {"key": SOURCE_MODE_APP_PLAYBACK, "label": "App playback", "selectable": True},
            {"key": SOURCE_MODE_EXTERNAL_INPUT, "label": "External input", "selectable": bool(inputs)},
            {"key": SOURCE_MODE_BLUETOOTH_INPUT, "label": "Bluetooth input", "selectable": bool(((bluetooth_overview.get("roles") or {}).get("bluetooth_input") or {}).get("selectable"))},
        ],
        "default_input": default_input,
        "selected_input": selected_input,
        "current_input": current_input,
        "inputs": inputs,
        "bluetooth": {
            "available": bluetooth_overview.get("available", False),
            "selectable": bool(((bluetooth_overview.get("roles") or {}).get("bluetooth_input") or {}).get("selectable")),
            "state": ((bluetooth_overview.get("roles") or {}).get("bluetooth_input") or {}).get("state", "unavailable"),
            "receiver_enabled": ((bluetooth_overview.get("roles") or {}).get("bluetooth_input") or {}).get("enabled", False),
            "discoverable": ((bluetooth_overview.get("roles") or {}).get("bluetooth_input") or {}).get("discoverable", False),
            "pairable": ((bluetooth_overview.get("roles") or {}).get("bluetooth_input") or {}).get("pairable", False),
            "connected_device": ((bluetooth_overview.get("receiver_session") or {}).get("device_name")),
            "active_codec": ((bluetooth_overview.get("receiver_session") or {}).get("active_codec")),
            "active_rate": ((bluetooth_overview.get("receiver_session") or {}).get("active_rate")),
            "notes": list((((bluetooth_overview.get("roles") or {}).get("bluetooth_input") or {}).get("notes") or [])),
        },
        "notes": notes,
    }


def set_audio_output_selection(key: str) -> dict[str, Any]:
    normalized_key = (key or "").strip()
    if not normalized_key:
        raise ValueError("Output key is required")

    overview_before = get_audio_output_overview()
    outputs = overview_before.get("outputs") or []

    selected_output = next((item for item in outputs if item.get("key") == normalized_key), None)
    if not selected_output:
        raise ValueError(f"Unknown output: {normalized_key}")
    if not selected_output.get("selectable", True):
        raise ValueError(f"Output is not selectable: {normalized_key}")

    _set_default_sink(selected_output["name"])
    _save_audio_output_selection(selected_output["key"])
    return get_audio_output_overview()


def set_audio_source_selection(mode: str, input_key: str | None = None) -> dict[str, Any]:
    normalized_mode = (mode or "").strip()
    normalized_input_key = (input_key or "").strip() or None
    if normalized_mode not in {SOURCE_MODE_APP_PLAYBACK, SOURCE_MODE_EXTERNAL_INPUT, SOURCE_MODE_BLUETOOTH_INPUT}:
        raise ValueError(f"Unknown source mode: {normalized_mode or mode}")

    overview_before = get_audio_source_overview()
    inputs = overview_before.get("inputs") or []

    if normalized_mode == SOURCE_MODE_BLUETOOTH_INPUT:
        bt_input_role = overview_before.get("bluetooth") or {}
        if not bt_input_role.get("selectable"):
            raise ValueError("Bluetooth input is not currently available")
        _save_audio_source_selection(normalized_mode, None)
        return get_audio_source_overview()

    if normalized_mode == SOURCE_MODE_EXTERNAL_INPUT:
        selected_input = None
        if normalized_input_key:
            selected_input = next((item for item in inputs if item.get("key") == normalized_input_key), None)
            if not selected_input:
                raise ValueError(f"Unknown input: {normalized_input_key}")
        elif overview_before.get("selected_input"):
            selected_input = overview_before["selected_input"]
        elif overview_before.get("current_input"):
            selected_input = overview_before["current_input"]
        elif inputs:
            selected_input = inputs[0]
        if not selected_input:
            raise ValueError("No external inputs are currently available")
        source_name = selected_input.get("source_key") or selected_input.get("name")
        port_key = selected_input.get("port_key")
        if source_name and port_key:
            _set_source_port(source_name, port_key)
        _save_audio_source_selection(normalized_mode, selected_input.get("key"))
    else:
        _save_audio_source_selection(normalized_mode, normalized_input_key or (overview_before.get("selected_input") or {}).get("key"))

    return get_audio_source_overview()


def apply_persisted_audio_output_selection() -> dict[str, Any] | None:
    selection_state = _load_audio_output_selection()
    selected_key = selection_state.get("selected_key")
    if not selected_key:
        return None
    try:
        return set_audio_output_selection(selected_key)
    except Exception:
        return None


def _select_relevant_sink(default_sink: dict[str, Any], sinks: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not sinks:
        return None

    running = [sink for sink in sinks if sink.get("state") == "RUNNING"]
    default_name = default_sink.get("name") if default_sink else None

    if default_name:
        for sink in running:
            if sink.get("name") == default_name:
                return sink

    for sink in running:
        if sink.get("name") == "easyeffects_sink":
            return sink

    if running:
        return running[0]

    if default_name:
        for sink in sinks:
            if sink.get("name") == default_name:
                return sink

    return sinks[0]


def _parse_default_rate(output: str) -> int | None:
    match = re.search(r'default\.clock\.rate\s*=\s*"?(\d+)"?', output)
    if match:
        return int(match.group(1))
    return None


def _strip_quoted_value(line: str) -> str:
    _, _, value = line.partition("=")
    return value.strip().strip('"')


def _safe_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def get_samplerate_status() -> dict[str, Any]:
    notes: list[str] = []

    try:
        metadata_output = _run_command(["pw-metadata", "-n", "settings", "0"])
        metadata = _parse_pw_metadata_settings(metadata_output)
    except Exception as exc:
        return {
            "status": "error",
            "available": False,
            "detail": str(exc),
            "mode": None,
            "force_rate": None,
            "active_rate": None,
            "clock_rate": None,
            "allowed_rates": [],
            "default_rate": None,
            "sink": {"id": None, "name": None, "description": None},
            "notes": ["pw-metadata unavailable"],
        }

    try:
        sink_output = _run_command(["wpctl", "inspect", "@DEFAULT_AUDIO_SINK@"])
        sink = _parse_default_sink(sink_output)
    except Exception as exc:
        sink = {"id": None, "name": None, "description": None}
        notes.append(f"Default sink unavailable: {exc}")

    active_rate = None
    relevant_sink = None
    try:
        pactl_sinks_output = _run_command(["pactl", "list", "sinks", "short"])
        pactl_sinks = _parse_pactl_sinks_short(pactl_sinks_output)
        relevant_sink = _select_relevant_sink(sink, pactl_sinks)
        active_rate = (relevant_sink or {}).get("active_rate")
        if active_rate is None and relevant_sink:
            notes.append(f"No parsed active rate for sink {relevant_sink.get('name')}")
        easyeffects_sink = next((item for item in pactl_sinks if item.get("name") == "easyeffects_sink"), None)
        if relevant_sink and easyeffects_sink and relevant_sink.get("name") != easyeffects_sink.get("name"):
            relevant_rate = relevant_sink.get("active_rate")
            ee_rate = easyeffects_sink.get("active_rate")
            if relevant_rate and ee_rate and relevant_rate != ee_rate:
                notes.append(f"Hardware sink {relevant_sink.get('name')} at {relevant_rate} Hz differs from easyeffects_sink at {ee_rate} Hz")
    except Exception as exc:
        notes.append(f"pactl sink rate unavailable: {exc}")

    if active_rate is None and sink.get("id") is not None:
        try:
            format_output = _run_command(["pw-cli", "enum-params", str(sink["id"]), "Format"])
            active_rate = _parse_active_rate(format_output)
            if active_rate is None:
                notes.append("Sink idle or no active format")
        except Exception as exc:
            notes.append(f"Active rate unavailable: {exc}")
    elif active_rate is None:
        notes.append("No default audio sink resolved")

    default_rate = None
    try:
        core_output = _run_command(["pw-cli", "info", "0"])
        default_rate = _parse_default_rate(core_output)
    except Exception as exc:
        notes.append(f"Default rate unavailable: {exc}")

    force_rate = metadata.get("force_rate") or 0
    return {
        "status": "ok",
        "available": True,
        "mode": "auto" if force_rate == 0 else "fixed",
        "force_rate": force_rate,
        "active_rate": active_rate,
        "clock_rate": metadata.get("clock_rate"),
        "allowed_rates": metadata.get("allowed_rates") or [],
        "default_rate": default_rate,
        "sink": sink,
        "relevant_sink": relevant_sink,
        "notes": notes,
    }
