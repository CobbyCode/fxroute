# SPDX-License-Identifier: AGPL-3.0-only

"""Bluetooth receiver/source actions and the Bluetooth audio overview."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .constants import SOURCE_MODE_BLUETOOTH_INPUT
from .parsing import (
    _bluetooth_device_id,
    _bluetooth_profile_from_node_name,
    _command_available,
    _extract_bluetooth_address,
    _humanize_sink_name,
    _humanize_source_name,
    _infer_bluetooth_codec,
    _is_bluetooth_sink_name,
    _is_bluetooth_source_name,
    _parse_bluetoothctl_devices,
    _parse_bluetoothctl_info,
    _parse_bluetoothctl_show,
    _parse_fraction_rate,
    _parse_pactl_sinks_detailed,
    _parse_pactl_sinks_short,
    _parse_pactl_sources_detailed,
    _parse_pactl_sources_short,
    _parse_wpctl_inspect,
    _parse_wpctl_status_bluetooth_streams,
    _pipewire_bluez_plugin_available,
    _run_command,
)
from .persistence import _load_audio_source_selection


def set_bluetooth_receiver_enabled(enabled: bool) -> dict[str, Any]:
    if not _command_available("bluetoothctl"):
        raise RuntimeError("bluetoothctl is not installed or not available in PATH")
    if not _bluetooth_daemon_reachable():
        # Nothing to configure: every bluetoothctl call would block until its
        # command timeout, stalling e.g. the shutdown cleanup on hosts with
        # bluez installed but no active daemon.
        return get_bluetooth_audio_overview()

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
    if not _bluetooth_daemon_reachable():
        return []

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

def _bluetooth_daemon_reachable() -> bool:
    """Return True when the system BlueZ D-Bus service answers quickly.

    bluetoothctl blocks until its command timeout when bluez is installed
    but not running (no D-Bus service), so probing the bus first keeps the
    audio overviews fast on hosts without an active Bluetooth stack.
    """
    probes = [
        ["dbus-send", "--system", "--print-reply", "--dest=org.bluez", "/org/bluez", "org.freedesktop.DBus.Peer.Ping"],
        ["busctl", "--system", "status", "org.bluez"],
    ]
    for probe in probes:
        if not _command_available(probe[0]):
            continue
        try:
            _run_command(probe)
        except Exception:
            return False
        return True
    return True  # no probe tool; fall back to the bounded bluetoothctl call


def get_bluetooth_audio_overview() -> dict[str, Any]:
    notes: list[str] = []
    selection_state = _load_audio_source_selection()
    receiver_enabled_intent = selection_state.get("mode") == SOURCE_MODE_BLUETOOTH_INPUT
    bluetoothctl_available = _command_available("bluetoothctl")
    bluetooth_daemon_reachable = _bluetooth_daemon_reachable()
    pactl_available = _command_available("pactl")
    pw_cli_available = _command_available("pw-cli")
    wpctl_available = _command_available("wpctl")

    # Independent command inventories run concurrently: serial execution made
    # this builder the latency floor of every audio overview on slow hosts.
    with ThreadPoolExecutor(max_workers=6) as pool:
        controller_future = pool.submit(_run_command, ["bluetoothctl", "show"]) if (bluetoothctl_available and bluetooth_daemon_reachable) else None
        sources_short_future = pool.submit(_run_command, ["pactl", "list", "sources", "short"]) if pactl_available else None
        sinks_short_future = pool.submit(_run_command, ["pactl", "list", "sinks", "short"]) if pactl_available else None
        sources_detailed_future = pool.submit(_run_command, ["pactl", "list", "sources"]) if pactl_available else None
        sinks_detailed_future = pool.submit(_run_command, ["pactl", "list", "sinks"]) if pactl_available else None
        wpctl_status_future = pool.submit(_run_command, ["wpctl", "status"]) if wpctl_available else None

        controller: dict[str, Any] | None = None
        adapter_present = False
        if bluetoothctl_available and bluetooth_daemon_reachable:
            try:
                controller = _parse_bluetoothctl_show(controller_future.result())
                adapter_present = bool(controller.get("address"))
            except Exception as exc:
                notes.append(f"Bluetooth adapter status unavailable: {exc}")
        elif bluetoothctl_available:
            notes.append("Bluetooth daemon is not reachable on the system bus.")
        else:
            notes.append("bluetoothctl is not installed or not available in PATH.")

        sources: list[dict[str, Any]] = []
        sinks: list[dict[str, Any]] = []
        source_details: dict[str, dict[str, Any]] = {}
        sink_details: dict[str, dict[str, Any]] = {}
        if pactl_available:
            try:
                sources = _parse_pactl_sources_short(sources_short_future.result())
            except Exception as exc:
                notes.append(f"Bluetooth source inventory unavailable: {exc}")
            try:
                sinks = _parse_pactl_sinks_short(sinks_short_future.result())
            except Exception as exc:
                notes.append(f"Bluetooth sink inventory unavailable: {exc}")
            try:
                source_details = _parse_pactl_sources_detailed(sources_detailed_future.result())
            except Exception as exc:
                notes.append(f"Bluetooth source details unavailable: {exc}")
            try:
                sink_details = _parse_pactl_sinks_detailed(sinks_detailed_future.result())
            except Exception as exc:
                notes.append(f"Bluetooth sink details unavailable: {exc}")
        else:
            notes.append("pactl is not installed or not available in PATH.")

        wpctl_bluetooth_streams: list[dict[str, Any]] = []
        if wpctl_available:
            try:
                wpctl_bluetooth_streams = _parse_wpctl_status_bluetooth_streams(wpctl_status_future.result())
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
    # When the BlueZ daemon is installed but not reachable (no DBus service
    # or no adapter), every bluetoothctl call blocks until the command
    # timeout. `show` already failed above, so skip the per-device queries
    # instead of adding their timeouts on top: with an unreachable daemon
    # they cannot return anything useful and would stall the audio overview
    # for seconds on every request.
    if bluetoothctl_available and bluetooth_daemon_reachable:
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

