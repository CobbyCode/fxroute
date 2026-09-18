# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit, side-effect-free migration from the legacy output/DSP documents."""

from __future__ import annotations

import copy

from audio.output_routing import device_key
from audio.output_state import default_output_state, default_processing, set_mode_routing, validate_output_state
from audio.output_topology import MAX_CHANNELS, SUB_ROLES, roles_for_mode
from audio.samplerate.persistence import _normalize_subwoofer_config, _normalize_subwoofer_22_config
from dsp.banks import BankState

_LEGACY_ROLES = {
    "stereo": ("off", "main_l", "main_r", "off", "off"),
    "subwoofer-2.1": ("off", "main_l", "main_r", "sub1", "sub1"),
    "subwoofer-2.2": ("off", "main_l", "main_r", "sub1", "sub2"),
    "subwoofer-2.2-stereo": ("off", "main_l", "main_r", "sub_l", "sub_r"),
}


def migrate_legacy_output_state(*, mode: dict, routing: dict, active_preset: str,
                                compare: dict, extras: dict, output_key: str,
                                channels: int) -> dict:
    """Capture old files first; callers explicitly commit after runtime validation."""
    if any(not isinstance(item, dict) for item in (mode, routing, compare, extras)):
        raise ValueError("Legacy mode, routing, compare, and extras must be objects")
    name = mode.get("mode", "stereo")
    if not isinstance(name, str) or name not in _LEGACY_ROLES:
        raise ValueError("Unsupported legacy output mode")
    if not isinstance(output_key, str) or not output_key.strip() or output_key != output_key.strip():
        raise ValueError("Migration requires an output device key")
    if type(channels) is not int or not 0 <= channels <= MAX_CHANNELS:
        raise ValueError("Migration requires a hardware channel count from 0 to 32")
    state = default_output_state()
    target_mode = "stereo" if name == "stereo" else "stereo-sub"
    state["active_mode"] = target_mode
    state["legacy"] = copy.deepcopy({"mode": mode, "routing": routing,
                                      "active_preset": active_preset, "compare": compare,
                                      "extras": extras})
    normalized_routing = {}
    for key, assignments in routing.items():
        if not isinstance(key, str) or not key.strip() or key != key.strip():
            raise ValueError("Invalid legacy device key")
        if (not isinstance(assignments, list) or len(assignments) > MAX_CHANNELS
                or any(type(signal) is not int or not 0 <= signal <= 4 for signal in assignments)):
            raise ValueError("Legacy routing requires numeric signal IDs from 0 to 4")
        identity = device_key(key)
        if identity in normalized_routing and normalized_routing[identity] != assignments:
            raise ValueError("Conflicting legacy routing for device profile aliases")
        normalized_routing[identity] = list(assignments)
    identity = device_key(output_key)
    if identity not in normalized_routing:
        normalized_routing[identity] = [1, 2, 3, 4][:channels]
    selected = normalized_routing[identity]
    selected.extend([0] * max(0, channels - len(selected)))
    for key, assignments in normalized_routing.items():
        state = set_mode_routing(state, target_mode, key, [_LEGACY_ROLES[name][signal] for signal in assignments])

    stereo = state["modes"][target_mode]
    preset_a = compare.get("presetA") or active_preset
    preset_b = compare.get("presetB") or None
    if preset_b == preset_a:
        preset_b = None
    stereo["banks"]["global"] = BankState(active_preset, preset_a, preset_b).to_dict()
    stereo["extras"] = copy.deepcopy(extras)
    legacy_sub = mode.get("subwoofer") if isinstance(mode.get("subwoofer"), dict) else {}
    if name in ("subwoofer-2.2", "subwoofer-2.2-stereo"):
        storage_key = "subwoofers_22_stereo" if name.endswith("-stereo") else "subwoofers_22"
        subwoofers = mode.get(storage_key)
        if not isinstance(subwoofers, dict):
            subwoofers = mode.get("subwoofers")
        normalized = _normalize_subwoofer_22_config(
            subwoofers if isinstance(subwoofers, dict) else {}, legacy_sub, mode)
        subs = {role: normalized[key] for role, key in zip(_LEGACY_ROLES[name][3:], ("sub1", "sub2"))}
    else:
        normalized = _normalize_subwoofer_config(legacy_sub)
        subs = {"sub1": {"level_db": normalized["sub_level_db"],
                         "alignment_ms": normalized["sub_alignment_ms"],
                         "polarity": normalized["sub_polarity"]}} if name == "subwoofer-2.1" else {}
    stereo["bass_management"] = {"frequency_hz": normalized["crossover_frequency_hz"],
                                  "main_highpass_enabled": normalized["main_highpass_enabled"]}
    for role, settings in subs.items():
        stereo["banks"].setdefault(role, BankState().to_dict())
        stereo["processing"][role] = {**default_processing(), **settings}
    return validate_output_state(state)


def upgrade_output_state(payload: dict) -> dict:
    """Upgrade the old Stereo/Crossover document without an alternate live model.

    The active configuration wins if both old modes map to the same new mode.
    The complete original document is archived for recovery, never consumed by
    routing or DSP. Loading does not write; the next revision-checked commit
    atomically persists the upgraded document.
    """
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return validate_output_state(payload)
    if (set(payload) != {"schema", "version", "revision", "active_mode", "modes", "legacy"}
            or payload["schema"] != "fxroute.output-state"
            or payload["active_mode"] not in ("stereo", "crossover")
            or not isinstance(payload["modes"], dict)
            or set(payload["modes"]) != {"stereo", "crossover"}):
        raise ValueError("Invalid version-one output state")
    result = default_output_state()
    result["revision"] = payload["revision"]
    if not isinstance(payload["legacy"], dict):
        raise ValueError("Legacy snapshot must be an object")
    result["legacy"] = copy.deepcopy(payload["legacy"])
    result["legacy"]["output_state_v1"] = copy.deepcopy(payload)
    ordered = [name for name in payload["modes"] if name != payload["active_mode"]] + [payload["active_mode"]]
    for name in ordered:
        config = copy.deepcopy(payload["modes"][name])
        if not isinstance(config, dict) or set(config) != {
                "routing", "selected_bank", "banks", "processing", "bass_management", "extras"}:
            raise ValueError("Invalid version-one mode state")
        enabled = name == "crossover"
        # Validate every old configuration, including one superseded by the
        # active configuration. Old stereo was allowed to route sub roles.
        candidate = default_output_state()
        config["crossover_enabled"] = enabled
        candidate["modes"]["stereo-sub"] = config
        validate_output_state(candidate)
        old_allowed = roles_for_mode("stereo-sub", crossover_enabled=enabled)
        if any(role not in ("global", *old_allowed) for role in config["banks"]):
            raise ValueError("Invalid version-one bank role")
        has_subs = any(role in SUB_ROLES for assignments in config["routing"].values() for role in assignments)
        target = "stereo-sub" if has_subs else "stereo"
        if target == "stereo":
            # Keep dormant sub data in the archived original, not in Stereo.
            config["banks"] = {role: bank for role, bank in config["banks"].items() if role not in SUB_ROLES}
            config["processing"] = {role: settings for role, settings in config["processing"].items() if role not in SUB_ROLES}
            if config["selected_bank"] in SUB_ROLES:
                config["selected_bank"] = "global"
        result["modes"][target] = config
        if name == payload["active_mode"]:
            result["active_mode"] = target
    return validate_output_state(result)
