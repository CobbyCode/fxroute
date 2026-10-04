# SPDX-License-Identifier: AGPL-3.0-only
"""Output-mode bindings to logical inputs, independent of their transport."""

from audio.output_state import validate_output_state


def input_identity(selection: dict) -> str:
    mode = str(selection.get("mode") or "app-playback")
    selected = selection.get("selected_input") or {}
    key = selection.get("selected_input_key") or selected.get("key")
    return f"{mode}:{key}" if key and mode == "external-input" else mode


def remember_mode(state: dict, selection: dict) -> dict:
    result = validate_output_state(state)
    result["input_modes"][input_identity(selection)] = result["active_mode"]
    return result


def restore_mode(state: dict, previous: dict, target: dict) -> dict:
    result = remember_mode(state, previous)
    identity = input_identity(target)
    if identity != input_identity(previous):
        result["active_mode"] = result["input_modes"].get(identity, "stereo")
    result["input_modes"][identity] = result["active_mode"]
    return validate_output_state(result)
