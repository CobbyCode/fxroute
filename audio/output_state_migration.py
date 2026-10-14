# SPDX-License-Identifier: AGPL-3.0-only
"""Side-effect-free upgrade of released output-state documents."""

from __future__ import annotations

import copy

from audio.output_state import default_level_cal, default_output_state, validate_output_state
from dsp.latency import default_av_sync


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


_UPGRADES = {3: _upgrade_v3_surround, 4: _upgrade_v4_level_calibration, 5: _upgrade_v5_av_sync}


def upgrade_output_state(payload: dict) -> dict:
    """Upgrade a released document step by step and validate the result.

    Releases wrote version three; versions four and five are the steps of
    that chain. Loading does not write; the next revision-checked commit
    atomically persists the upgraded document.
    """
    while isinstance(payload, dict) and payload.get("version") in _UPGRADES:
        payload = _UPGRADES[payload["version"]](payload)
    return validate_output_state(payload)
