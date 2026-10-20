# SPDX-License-Identifier: AGPL-3.0-only
"""Prepare device-local routing without changing another device's crossover."""

from audio import hdmi
from audio.output_routing import device_key
from audio.output_state import set_mode_routing, validate_output_state
from audio.output_topology import CENTER_ROLES, MAIN_ROLES, SPEAKER_ROLES


def prepare_device_state(state: dict, output_key: str, *, defaults: dict) -> dict:
    """Scope legacy crossover intent on an explicitly selected target device.

    Legacy global toggles also converted full-range devices to Low-only
    drafts. Only that unsplit layout can return to full-range: any configured
    High/Mid way remains a crossover, including incomplete drafts. Explicit
    device selections are never inferred again.
    """
    result = validate_output_state(state)
    key = hdmi.routing_key(device_key(output_key))
    for mode in result["modes"]:
        config = result["modes"][mode]
        if key in config["device_crossovers"]:
            continue
        saved = hdmi.stored_routing(config["routing"], device_key(output_key))
        split = set(saved or []) & set((*SPEAKER_ROLES, *CENTER_ROLES))
        enabled = config["crossover_enabled"] and saved is not None
        unsplit = split <= {"left_low", "right_low"}
        if unsplit:
            enabled = False
        config["device_crossovers"][key] = bool(enabled)
        if saved is None:
            result = set_mode_routing(result, mode, output_key, defaults.get(mode) or list(MAIN_ROLES))
        elif config["crossover_enabled"] and unsplit:
            mapping = {"left_low": "main_l", "right_low": "main_r"}
            result = set_mode_routing(result, mode, output_key, [mapping.get(role, role) for role in saved])
    return validate_output_state(result)
