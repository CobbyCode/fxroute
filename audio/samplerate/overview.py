# SPDX-License-Identifier: AGPL-3.0-only

"""Samplerate status, audio output/source overviews, and selection actions."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Mapping

from .bluetooth import get_bluetooth_audio_overview
from .constants import (
    NON_SELECTABLE_INPUT_KEYS,
    NON_SELECTABLE_OUTPUT_KEYS,
    effective_supported_rates,
    OUTPUT_MODE_STEREO,
    OUTPUT_MODE_SUBWOOFER_21,
    OUTPUT_MODE_SUBWOOFER_22,
    OUTPUT_MODE_SUBWOOFER_22_STEREO,
    OUTPUT_MODE_SUBWOOFER_MODES,
    OUTPUT_MODES,
    PIPEWIRE_DEFAULT_RATE_OPTIONS,
    SAMPLE_RATE_CANDIDATES,
    SOURCE_MODE_APP_PLAYBACK,
    SOURCE_MODE_BLUETOOTH_INPUT,
    SOURCE_MODE_EXTERNAL_INPUT,
)
from .parsing import (
    _bluetooth_profile_from_node_name,
    _build_sink_output_label,
    _humanize_sink_name,
    _humanize_source_name,
    _infer_bluetooth_codec,
    _is_bluetooth_sink_name,
    _parse_active_rate,
    _parse_default_rate,
    _parse_default_sink,
    _parse_enum_format_supported_rates,
    _parse_pactl_sinks_detailed,
    _parse_pactl_sinks_short,
    _parse_pactl_sources_detailed,
    _parse_pactl_sources_short,
    _parse_pw_metadata_settings,
    _parse_pw_node_ids,
    _parse_sample_spec_channels,
    _run_command,
)
from .persistence import (
    _audio_output_mode_path,
    _build_audio_output_mode_payload,
    _load_audio_output_mode,
    _load_audio_output_selection,
    _load_audio_source_selection,
    _load_device_output_modes,
    _load_pipewire_clock_rate_config,
    _save_audio_output_selection,
    _save_audio_source_selection,
    load_sample_rate_policy,
)


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
            "sample_spec": selected_output.get("sample_spec"),
            "channels": selected_output.get("channels"),
            "active_rate": selected_output.get("active_rate"),
            "supported_rates": list(selected_output.get("supported_rates") or []),
        }
    return None

def get_audio_output_overview(status: dict[str, Any] | None = None) -> dict[str, Any]:
    # The independent PipeWire/BlueZ enumerations below each spawn their own
    # subprocess; running them concurrently keeps this builder's latency near
    # the slowest single read instead of the sum of all reads.  Callers that
    # already hold a fresh samplerate status may pass it as ``status`` so the
    # build does not repeat that pipeline.
    notes: list[str] = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        status_future = (
            None if isinstance(status, dict) else pool.submit(get_samplerate_status)
        )
        bluetooth_future = pool.submit(get_bluetooth_audio_overview)
        sinks_short_future = pool.submit(_run_command, ["pactl", "list", "sinks", "short"])
        sinks_detailed_future = pool.submit(_run_command, ["pactl", "list", "sinks"])
        nodes_future = pool.submit(_run_command, ["pw-cli", "ls", "Node"])

        if status_future is not None:
            status = status_future.result()
        bluetooth_overview = bluetooth_future.result()

        default_sink = status.get("sink") or {"id": None, "name": None, "description": None}
        relevant_sink = status.get("relevant_sink") or {}
        selection_state = _load_audio_output_selection()
        output_mode = _load_audio_output_mode()

        sinks: list[dict[str, Any]] = []
        sink_details: dict[str, dict[str, Any]] = {}
        notes = list(status.get("notes") or [])
        try:
            sinks = _parse_pactl_sinks_short(sinks_short_future.result())
        except Exception as exc:
            notes.append(f"Output list unavailable: {exc}")

        try:
            sink_details = _parse_pactl_sinks_detailed(sinks_detailed_future.result())
        except Exception as exc:
            notes.append(f"Output details unavailable: {exc}")

        node_ids: dict[str, int] = {}
        try:
            node_ids = _parse_pw_node_ids(nodes_future.result())
        except Exception as exc:
            notes.append(f"Output sample-rate capabilities unavailable: {exc}")

    default_name = default_sink.get("name")
    current_name = relevant_sink.get("name") or default_name
    default_label = default_sink.get("description") or _humanize_sink_name(default_name)
    selected_key = selection_state.get("selected_key")

    # The per-sink EnumFormat reads are independent commands; run them
    # concurrently before the assembly loop.  A per-sink failure is stored so
    # the loop can append the exact same note it appended when these reads
    # were serial.
    enum_results: dict[int, Any] = {}
    enum_node_ids = sorted({
        node_ids[str(sink.get("name") or "")]
        for sink in sinks
        if node_ids.get(str(sink.get("name") or "")) is not None
    })
    if enum_node_ids:
        with ThreadPoolExecutor(max_workers=min(4, len(enum_node_ids))) as enum_pool:
            enum_futures = {
                node_id: enum_pool.submit(
                    _run_command, ["pw-cli", "enum-params", str(node_id), "EnumFormat"]
                )
                for node_id in enum_node_ids
            }
            for node_id, future in enum_futures.items():
                try:
                    enum_results[node_id] = _parse_enum_format_supported_rates(future.result())
                except Exception as exc:
                    enum_results[node_id] = exc

    explicit_outputs = []
    for sink in sinks:
        name = sink.get("name")
        details = sink_details.get(name or "", {})
        label = _build_sink_output_label(name, details, default_label if name == default_name and default_label else None)
        profile = _bluetooth_profile_from_node_name(name)
        native_supported_rates: list[int] = []
        node_id = node_ids.get(str(name or ""))
        if node_id is not None:
            enum_result = enum_results.get(node_id)
            if isinstance(enum_result, Exception):
                notes.append(f"Sample-rate capabilities unavailable for {label}: {enum_result}")
            elif enum_result is not None:
                native_supported_rates = enum_result
        if not native_supported_rates and sink.get("active_rate") in SAMPLE_RATE_CANDIDATES:
            native_supported_rates = [sink["active_rate"]]
        # native_supported_rates is the raw hardware/PipeWire capability;
        # supported_rates is capped at the FXRoute DSP processing maximum and
        # is the only list the API/UI may offer for FXRoute playback.
        explicit_outputs.append({
            "id": sink.get("id"),
            "key": name,
            "name": name,
            "label": label,
            "sample_spec": details.get("sample_spec") or sink.get("sample_spec"),
            "channels": _parse_sample_spec_channels(details.get("sample_spec")) or sink.get("channels"),
            "active_rate": sink.get("active_rate"),
            "supported_rates": effective_supported_rates(native_supported_rates),
            "native_supported_rates": native_supported_rates,
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
    effective_output = next((item for item in explicit_outputs if item.get("key") == (selected_output or {}).get("key")), None) or current_output
    output_mode_available = bool((effective_output or {}).get("channels") and (effective_output or {}).get("channels") >= 4)
    if output_mode.get("mode") in OUTPUT_MODE_SUBWOOFER_MODES and not output_mode_available:
        label = (
            "2.1"
            if output_mode.get("mode") == OUTPUT_MODE_SUBWOOFER_21
            else "2.2 Stereo Bass"
            if output_mode.get("mode") == OUTPUT_MODE_SUBWOOFER_22_STEREO
            else "2.2"
        )
        notes.append(f"{label} Subwoofer mode requires a selected multichannel output with at least 4 channels.")
    routing_status = (
        "Out 1/2 Main · Out 3 Left Sub · Out 4 Right Sub"
        if output_mode.get("mode") == OUTPUT_MODE_SUBWOOFER_22_STEREO
        else "Out 1/2 Main · Out 3/4 Sub"
    )

    return {
        "available": bool(status.get("available")),
        "default_output": {
            "key": default_name,
            "label": default_label,
            "target_name": default_name,
            "target_label": default_label,
            "is_selected": bool(default_name and selected_key == default_name),
            "channels": next((item.get("channels") for item in explicit_outputs if item.get("key") == default_name), None),
        },
        "selected_output": selected_output,
        "current_output": current_output,
        "outputs": explicit_outputs,
        "output_mode": {
            **output_mode,
            "routing": {
                "main_pair": [1, 2],
                "sub_pair": [3, 4],
                "status": routing_status,
            },
            "available": output_mode_available,
            "required_channels": 4,
            "effective_output_key": (effective_output or {}).get("key"),
            "effective_output_channels": (effective_output or {}).get("channels"),
            "effective_output_rate": (effective_output or {}).get("active_rate"),
        },
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

def _output_mode_label(mode: str) -> str:
    if mode == OUTPUT_MODE_SUBWOOFER_21:
        return "2.1"
    if mode == OUTPUT_MODE_SUBWOOFER_22_STEREO:
        return "2.2 Stereo Bass"
    if mode == OUTPUT_MODE_SUBWOOFER_22:
        return "2.2"
    return "Stereo"

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
    policy = load_sample_rate_policy()
    if (
        policy.get("mode") == "fixed"
        and policy.get("rate") not in (selected_output.get("supported_rates") or [])
    ):
        raise ValueError("Selected output does not support the configured fixed sample rate")

    # A deliberate device switch wins over the current output mode: derive the
    # mode this device can actually carry instead of refusing the switch.  A
    # subwoofer mode on a stereo-only device falls back to Stereo, and a mode
    # remembered for this device is restored when the device can carry it.
    valid_modes = OUTPUT_MODES
    channels = int(selected_output.get("channels") or 0)
    current_mode = _load_audio_output_mode()
    current_mode_name = str(current_mode.get("mode") or OUTPUT_MODE_STEREO).strip()
    candidate_mode = (_load_device_output_modes().get(normalized_key) or current_mode_name)
    if candidate_mode not in valid_modes:
        candidate_mode = OUTPUT_MODE_STEREO
    effective_mode = candidate_mode
    if effective_mode in OUTPUT_MODE_SUBWOOFER_MODES and channels < 4:
        effective_mode = OUTPUT_MODE_STEREO
    mode_changed = effective_mode != current_mode_name

    _set_default_sink(selected_output["name"])
    _save_audio_output_selection(selected_output["key"])
    if mode_changed:
        persist_audio_output_mode(_build_audio_output_mode_payload(effective_mode))

    overview = get_audio_output_overview()
    if mode_changed:
        reason = (
            "device-channel-capacity"
            if candidate_mode in OUTPUT_MODE_SUBWOOFER_MODES and channels < 4
            else "device-remembered-mode"
        )
        if reason == "device-channel-capacity":
            message = (
                f"Output mode switched to {_output_mode_label(effective_mode)} — "
                f"selected device supports {channels} channels."
            )
        else:
            message = (
                f"Output mode restored to {_output_mode_label(effective_mode)} — "
                "last used with this device."
            )
        overview["output_mode"]["mode_adjustment"] = {
            "adjusted": True,
            "previous_mode": current_mode_name,
            "mode": effective_mode,
            "reason": reason,
            "message": message,
        }
    return overview

def prepare_audio_output_mode(
    mode: str,
    subwoofer: dict[str, Any] | None = None,
    subwoofers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate and build an output-mode target without persisting it.

    The returned ``config`` is the exact durable payload that may be written
    only after the Coordinator has committed the corresponding runtime graph.
    ``overview`` is a live hardware overview with the target mode overlaid so
    the guarded runtime can stage the new topology before persistence.
    """
    normalized_mode = (mode or OUTPUT_MODE_STEREO).strip()
    valid_modes = OUTPUT_MODES
    if normalized_mode not in valid_modes:
        raise ValueError(f"Unknown output mode: {mode}")
    if normalized_mode in OUTPUT_MODE_SUBWOOFER_MODES:
        overview = get_audio_output_overview()
        output_mode = overview.get("output_mode") or {}
        if not output_mode.get("available"):
            label = (
                "2.1"
                if normalized_mode == OUTPUT_MODE_SUBWOOFER_21
                else "2.2 Stereo Bass"
                if normalized_mode == OUTPUT_MODE_SUBWOOFER_22_STEREO
                else "2.2"
            )
            raise ValueError(f"{label} Subwoofer requires a selected multichannel output with at least 4 channels")
    saved = _build_audio_output_mode_payload(normalized_mode, subwoofer, subwoofers)
    overview = get_audio_output_overview()
    overview["output_mode"] = {
        **(overview.get("output_mode") or {}),
        **saved,
    }
    return {"overview": overview, "config": saved}

def persist_audio_output_mode(config: Mapping[str, Any]) -> dict[str, Any]:
    """Persist a previously validated output-mode target after graph commit.

    The mode is also remembered as the last valid mode of the currently
    selected output device so a later device switch can restore it.
    """
    payload = dict(config or {})
    mode = str(payload.get("mode") or "").strip()
    if mode not in OUTPUT_MODES:
        raise ValueError(f"Unknown output mode: {mode}")
    device_key = (_load_audio_output_selection() or {}).get("selected_key")
    if device_key:
        device_modes = dict(_load_device_output_modes())
        device_modes[device_key] = mode
        payload["device_modes"] = device_modes
    path = _audio_output_mode_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return get_audio_output_overview()

def set_audio_output_mode(
    mode: str,
    subwoofer: dict[str, Any] | None = None,
    subwoofers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compatibility wrapper for non-playback configuration callers.

    Playback-facing API routes must use ``prepare_audio_output_mode`` and let
    the Coordinator call ``persist_audio_output_mode`` after its graph commit.
    """
    target = prepare_audio_output_mode(mode, subwoofer, subwoofers)
    return persist_audio_output_mode(target["config"])

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
        if sink.get("name") == "fxroute_dsp_sink":
            return sink

    if running:
        return running[0]

    if default_name:
        for sink in sinks:
            if sink.get("name") == default_name:
                return sink

    return sinks[0]

def get_samplerate_status() -> dict[str, Any]:
    notes: list[str] = []
    policy = load_sample_rate_policy()
    clock_rate_config = _load_pipewire_clock_rate_config()

    # These four reads are independent commands; run them concurrently so the
    # status cost tracks the slowest single read instead of their sum.  The
    # results below are still consumed in the original serial order, so note
    # texts and error semantics stay exactly the same.
    with ThreadPoolExecutor(max_workers=4) as pool:
        metadata_future = pool.submit(_run_command, ["pw-metadata", "-n", "settings", "0"])
        sink_inspect_future = pool.submit(_run_command, ["wpctl", "inspect", "@DEFAULT_AUDIO_SINK@"])
        sinks_short_future = pool.submit(_run_command, ["pactl", "list", "sinks", "short"])
        core_info_future = pool.submit(_run_command, ["pw-cli", "info", "0"])

        try:
            metadata_output = metadata_future.result()
            metadata = _parse_pw_metadata_settings(metadata_output)
        except Exception as exc:
            return {
                "status": "error",
                "available": False,
                "detail": str(exc),
                "mode": None,
                "policy": policy,
                "force_rate": None,
                "configured_default_rate": clock_rate_config.get("configured_default_rate"),
                "configured_allowed_rates": clock_rate_config.get("configured_allowed_rates") or [],
                "active_rate": None,
                "clock_rate": None,
                "allowed_rates": [],
                "default_rate_options": PIPEWIRE_DEFAULT_RATE_OPTIONS,
                "pipewire_clock_config_path": clock_rate_config.get("config_path"),
                "pipewire_clock_config_exists": bool(clock_rate_config.get("config_exists")),
                "restart_required": False,
                "default_rate": None,
                "sink": {"id": None, "name": None, "description": None},
                "notes": ["pw-metadata unavailable"],
            }

        try:
            sink_output = sink_inspect_future.result()
            sink = _parse_default_sink(sink_output)
        except Exception as exc:
            sink = {"id": None, "name": None, "description": None}
            notes.append(f"Default sink unavailable: {exc}")

        active_rate = None
        relevant_sink = None
        try:
            pactl_sinks_output = sinks_short_future.result()
            pactl_sinks = _parse_pactl_sinks_short(pactl_sinks_output)
            relevant_sink = _select_relevant_sink(sink, pactl_sinks)
            active_rate = (relevant_sink or {}).get("active_rate")
            if active_rate is None and relevant_sink:
                notes.append(f"No parsed active rate for sink {relevant_sink.get('name')}")
            dsp_sink = next((item for item in pactl_sinks if item.get("name") == "fxroute_dsp_sink"), None)
            if relevant_sink and dsp_sink and relevant_sink.get("name") != dsp_sink.get("name"):
                relevant_rate = relevant_sink.get("active_rate")
                dsp_rate = dsp_sink.get("active_rate")
                if relevant_rate and dsp_rate and relevant_rate != dsp_rate:
                    notes.append(f"Hardware sink {relevant_sink.get('name')} at {relevant_rate} Hz differs from fxroute_dsp_sink at {dsp_rate} Hz")
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
            core_output = core_info_future.result()
            default_rate = _parse_default_rate(core_output)
        except Exception as exc:
            notes.append(f"Default rate unavailable: {exc}")

    configured_default_rate = clock_rate_config.get("configured_default_rate")
    force_rate = metadata.get("force_rate") or 0
    restart_required = bool(
        configured_default_rate
        and default_rate
        and configured_default_rate != default_rate
    )
    return {
        "status": "ok",
        "available": True,
        # The mode field mirrors the persisted policy, not the live pin: a
        # leftover force-rate at the graph default (harmless, and only ever
        # written by the reconciliation) must not make an auto policy report
        # mode=fixed.  Consumers decide from ``policy``; ``mode`` stays a
        # payload summary of the intended mode.
        "mode": policy.get("mode", "auto"),
        "policy": policy,
        "force_rate": force_rate,
        "configured_default_rate": configured_default_rate,
        "configured_allowed_rates": clock_rate_config.get("configured_allowed_rates") or [],
        "active_rate": active_rate,
        "clock_rate": metadata.get("clock_rate"),
        "allowed_rates": metadata.get("allowed_rates") or [],
        "default_rate_options": PIPEWIRE_DEFAULT_RATE_OPTIONS,
        "pipewire_clock_config_path": clock_rate_config.get("config_path"),
        "pipewire_clock_config_exists": bool(clock_rate_config.get("config_exists")),
        "restart_required": restart_required,
        "default_rate": default_rate,
        "sink": sink,
        "relevant_sink": relevant_sink,
        "notes": notes,
    }

def overview_sample_rate(overview: dict | None) -> int | None:
    """Return a rate from an already captured overview for stale detection only."""
    if not isinstance(overview, dict):
        return None
    output_mode = overview.get("output_mode") or {}
    selected_output = overview.get("selected_output") or overview.get("current_output") or {}
    for value in (
        output_mode.get("effective_output_rate"),
        selected_output.get("active_rate"),
        overview.get("active_rate"),
    ):
        if isinstance(value, int) and value > 0:
            return value
    return None

def authoritative_sample_rate(status: dict | None) -> int | None:
    """Read the live rate which owns the helper start decision."""
    if not isinstance(status, dict):
        return None
    for key in ("force_rate", "active_rate"):
        value = status.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return None

def audio_output_overview_with_effective_rate(overview: dict, effective_rate: int) -> dict:
    output_mode = dict(overview.get("output_mode") or {})
    selected_output = dict(overview.get("selected_output") or {})
    current_output = dict(overview.get("current_output") or {})
    output_mode["effective_output_rate"] = effective_rate
    if selected_output:
        selected_output["active_rate"] = effective_rate
    if current_output and current_output.get("key") == selected_output.get("key"):
        current_output["active_rate"] = effective_rate
    return {
        **overview,
        "output_mode": output_mode,
        "selected_output": selected_output or overview.get("selected_output"),
        "current_output": current_output or overview.get("current_output"),
    }

def measurement_helper_snapshot_summary(snapshot: dict | None) -> dict:
    snapshot = snapshot or {}
    config = snapshot.get("config") or {}
    return {
        "active": bool(snapshot.get("active")),
        "helper_pid": snapshot.get("helper_pid"),
        "sample_rate": config.get("sample_rate"),
        "sub_alignment_ms": config.get("sub_alignment_ms"),
        "main_delay_ms": config.get("derived_main_delay_ms"),
        "sub_delay_ms": config.get("derived_sub_delay_ms"),
        "stage": snapshot.get("stage"),
        "last_error": snapshot.get("last_error"),
    }

def playback_rate_aligned(status: Mapping[str, Any] | None, target_rate: int | None) -> bool:
    """Return whether a samplerate status readback matches the target rate.

    Canonical alignment predicate shared by the transition adapter: the active
    rate must equal the target and no conflicting force-rate may be in place
    (None/0 mean "no force-rate set").
    """
    if not isinstance(status, Mapping):
        return False
    return bool(
        status.get("active_rate") == target_rate
        and status.get("force_rate") in {None, 0, target_rate}
    )

