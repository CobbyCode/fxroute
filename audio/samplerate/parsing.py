# SPDX-License-Identifier: AGPL-3.0-only

"""Command execution and output parsing for PipeWire/pactl/bluetoothctl text."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .constants import (
    COMMAND_TIMEOUT_SECONDS,
    PIPEWIRE_DEFAULT_RATE_OPTIONS,
    SAMPLE_RATE_CANDIDATES,
)
from audio.tool_env import c_locale_env


def _run_command(args: list[str]) -> str:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
            env=c_locale_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Command timed out: {' '.join(args)}") from exc
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

def _parse_pw_node_ids(output: str) -> dict[str, int]:
    nodes: dict[str, int] = {}
    node_id: int | None = None
    for raw_line in output.splitlines():
        id_match = re.match(r"\s*id\s+(\d+),", raw_line)
        if id_match:
            node_id = int(id_match.group(1))
            continue
        name_match = re.match(r'\s*node\.name\s*=\s*"([^"]+)"', raw_line)
        if node_id is not None and name_match:
            nodes[name_match.group(1)] = node_id
    return nodes

def _parse_enum_format_supported_rates(output: str) -> list[int]:
    supported: set[int] = set()
    rate_blocks = re.findall(
        r"Audio:rate\s*\([^\n]*\).*?(?=\n\s*Prop:|\Z)",
        output,
        re.DOTALL,
    )
    for block in rate_blocks:
        values = [int(value) for value in re.findall(r"\bInt\s+(\d+)\b", block)]
        if not values:
            continue
        if "Choice:Range" in block and len(values) >= 3:
            minimum, maximum = values[1], values[2]
            supported.update(rate for rate in SAMPLE_RATE_CANDIDATES if minimum <= rate <= maximum)
        else:
            supported.update(rate for rate in values if rate in SAMPLE_RATE_CANDIDATES)
    return [rate for rate in SAMPLE_RATE_CANDIDATES if rate in supported]

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
            "channels": _parse_sample_spec_channels(sample_spec),
            "active_rate": int(rate_match.group(1)) if rate_match else None,
            "state": parts[4].strip().upper(),
        })
    return sinks

def _parse_sample_spec_channels(sample_spec: str | None) -> int | None:
    match = re.search(r"\b(\d+)ch\b", sample_spec or "", re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None

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

def _parse_pipewire_clock_rate_dropin(text: str) -> dict[str, Any]:
    rate_match = re.search(r"default\.clock\.rate\s*=\s*(\d+)", text)
    allowed_match = re.search(r"default\.clock\.allowed-rates\s*=\s*\[([^\]]*)\]", text)
    configured_rate = None
    if rate_match:
        try:
            configured_rate = _normalize_pipewire_default_rate(rate_match.group(1))
        except ValueError:
            configured_rate = None
    allowed_rates = [int(item) for item in re.findall(r"\d+", allowed_match.group(1))] if allowed_match else []
    return {
        "configured_default_rate": configured_rate,
        "configured_allowed_rates": allowed_rates,
    }

def _normalize_pipewire_default_rate(value: Any) -> int:
    try:
        rate = int(value)
    except (TypeError, ValueError):
        raise ValueError("Invalid PipeWire default sample rate")
    if rate not in PIPEWIRE_DEFAULT_RATE_OPTIONS:
        raise ValueError("Unsupported PipeWire default sample rate")
    return rate

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

