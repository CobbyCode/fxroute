"""Helpers for read-only PipeWire samplerate status."""

from __future__ import annotations

import re
import subprocess
from typing import Any


def _run_command(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(stderr or f"Command failed: {' '.join(args)}")
    return result.stdout


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
