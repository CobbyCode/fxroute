# SPDX-License-Identifier: AGPL-3.0-only
"""Side-effect-free upgrade of released output-state documents."""

from __future__ import annotations

import copy

from audio.output_state import default_level_cal, default_output_state, validate_output_state
from dsp.latency import default_av_sync
from dsp.surround_global import neutral_selection


def _upgrade_v3_surround(payload: dict) -> dict:
    """Give a released version-three document the Surround mode and input modes."""
    if (set(payload) != {"schema", "version", "revision", "active_mode", "modes", "legacy"}
            or not isinstance(payload.get("modes"), dict)
            or set(payload["modes"]) != {"stereo", "stereo-sub"}):
        raise ValueError("Invalid version-three output state")
    result = copy.deepcopy(payload)
    result["version"] = 4
    result["input_modes"] = {}
    result["modes"]["surround"] = default_output_state()["modes"]["surround"]
    return result


def _upgrade_v4_level_calibration(payload: dict) -> dict:
    """Give a version-four document neutral Level Calibration slots.

    A mode that already carries slots (the Surround block the version-three
    step creates) keeps them; ``validate_output_state`` still checks every value.
    """
    result = copy.deepcopy(payload)
    result["version"] = 5
    modes = result.get("modes")
    if not isinstance(modes, dict):
        raise ValueError("Invalid version-four output state")
    for config in modes.values():
        processing = config.get("processing") if isinstance(config, dict) else None
        if not isinstance(processing, dict) or any(not isinstance(item, dict) for item in processing.values()):
            raise ValueError("Invalid version-four output state")
        for settings in processing.values():
            settings.setdefault("level_cal_db", default_level_cal())
    return result


def _upgrade_v5_av_sync(payload: dict) -> dict:
    """Give a version-five document the neutral AV Sync block per mode."""
    result = copy.deepcopy(payload)
    result["version"] = 6
    modes = result.get("modes")
    if not isinstance(modes, dict):
        raise ValueError("Invalid version-five output state")
    for config in modes.values():
        if not isinstance(config, dict):
            raise ValueError("Invalid version-five output state")
        config.setdefault("av_sync", default_av_sync())
    return result


def _upgrade_v6_surround_global_groups(payload: dict) -> dict:
    """Give the Surround Global slots a preset selection per speaker group.

    A slot that named one preset cannot be mapped to its group without the
    preset library: it starts without filters, Direct stays Direct. A B equal
    to A is cleared; the listened side is kept.
    """
    result = copy.deepcopy(payload)
    result["version"] = 7
    try:
        binding = result["modes"]["surround"]["banks"]["global"]
        preset, preset_a, preset_b = binding["preset"], binding["preset_a"], binding["preset_b"]
    except (KeyError, TypeError) as exc:
        raise ValueError("Invalid version-six output state") from exc

    def selection(slot):
        return slot if slot is None or slot == "Direct" or isinstance(slot, dict) else neutral_selection()

    listened_b = preset_b is not None and preset == preset_b and preset != preset_a
    preset_a, preset_b = selection(preset_a), selection(preset_b)
    if preset_b == preset_a:
        preset_b = None
    binding.update(preset=preset_b if listened_b and preset_b is not None else preset_a,
                   preset_a=preset_a, preset_b=preset_b)
    return result


_UPGRADES = {3: _upgrade_v3_surround, 4: _upgrade_v4_level_calibration, 5: _upgrade_v5_av_sync,
             6: _upgrade_v6_surround_global_groups}


def upgrade_output_state(payload: dict) -> dict:
    """Upgrade a released document step by step and validate the result.

    Releases wrote version three; versions four to six are the steps of
    that chain. Loading does not write; the next revision-checked commit
    atomically persists the upgraded document.
    """
    while isinstance(payload, dict) and payload.get("version") in _UPGRADES:
        payload = _UPGRADES[payload["version"]](payload)
    return validate_output_state(payload)
