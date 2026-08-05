"""EasyEffects extras payload helpers.

Extracted verbatim from main.py (REFACTOR-010). Behavior is identical to the
previous inline implementation: camelCase/snake_case alias parsing with
camelCase priority, exact defaults and `or`-fallback behavior, full type
conversions (including `strength` as str on merge), `toneEffectMode`
normalization only on full parse, merge of only explicitly supplied fields,
deep-copy non-mutation semantics, calibration/calibrationProfiles validation
with exact error messages, and exact change detection for strength as well as
autogain/loudness.
"""

from __future__ import annotations

import copy
from typing import Any


def parse_effects_extras_from_json(body: dict) -> dict:
    limiter_enabled = bool(body.get("limiterEnabled", body.get("limiter_enabled", False)))
    headroom_enabled = bool(body.get("headroomEnabled", body.get("headroom_enabled", False)))
    headroom_gain_db = float(body.get("headroomGainDb", body.get("headroom_gain_db", -3.0)) or -3.0)
    autogain_enabled = bool(body.get("autogainEnabled", body.get("autogain_enabled", False)))
    autogain_target_db = float(body.get("autogainTargetDb", body.get("autogain_target_db", -12.0)) or -12.0)
    loudness_enabled = bool(body.get("loudnessEnabled", body.get("loudness_enabled", False)))
    loudness_fft_size = int(body.get("loudnessFftSize", body.get("loudness_fft_size", 4096)) or 4096)
    loudness_strength = body.get("loudnessStrength", body.get("loudness_strength", 10))
    loudness_volume_db = float(body.get("loudnessVolumeDb", body.get("loudness_volume_db", 0.0)) or 0.0)
    delay_enabled = bool(body.get("delayEnabled", body.get("delay_enabled", False)))
    delay_left_ms = float(body.get("delayLeftMs", body.get("delay_left_ms", 0.0)) or 0.0)
    delay_right_ms = float(body.get("delayRightMs", body.get("delay_right_ms", 0.0)) or 0.0)
    bass_enabled = bool(body.get("bassEnabled", body.get("bass_enabled", False)))
    bass_amount = float(body.get("bassAmount", body.get("bass_amount", 0.0)) or 0.0)
    tone_effect_enabled = bool(body.get("toneEffectEnabled", body.get("tone_effect_enabled", False)))
    tone_effect_mode = str(body.get("toneEffectMode", body.get("tone_effect_mode", "crystalizer")) or "crystalizer").strip().lower()
    return {
        "limiter": {"enabled": limiter_enabled},
        "headroom": {
            "enabled": headroom_enabled,
            "params": {
                "gainDb": headroom_gain_db,
            },
        },
        "autogain": {
            "enabled": autogain_enabled,
            "params": {
                "targetDb": autogain_target_db,
            },
        },
        "loudness": {
            "enabled": loudness_enabled,
            "params": {
                "fftSize": loudness_fft_size,
                "strength": loudness_strength,
                "volumeDb": loudness_volume_db,
                "calibration": body.get("calibration") if isinstance(body.get("calibration"), dict) else {},
                "calibrationProfiles": body.get("calibrationProfiles") if isinstance(body.get("calibrationProfiles"), dict) else {},
            },
        },
        "delay": {
            "enabled": delay_enabled,
            "params": {
                "leftMs": delay_left_ms,
                "rightMs": delay_right_ms,
            },
        },
        "bass_enhancer": {
            "enabled": bass_enabled,
            "params": {
                "amount": bass_amount,
                "harmonics": 8.5,
                "scope": 100.0,
                "blend": 0.0,
            },
        },
        "tone_effect": {
            "enabled": tone_effect_enabled,
            "mode": tone_effect_mode,
        },
    }


def merge_effects_extras_from_json(previous: dict, body: dict) -> dict:
    """Apply only explicitly supplied JSON fields to persisted extras."""
    merged = copy.deepcopy(previous)

    def supplied(*names: str) -> tuple[bool, Any]:
        for name in names:
            if name in body:
                return True, body[name]
        return False, None

    scalar_fields = (
        ("limiter", "enabled", ("limiterEnabled", "limiter_enabled"), bool),
        ("headroom", "enabled", ("headroomEnabled", "headroom_enabled"), bool),
        ("autogain", "enabled", ("autogainEnabled", "autogain_enabled"), bool),
        ("loudness", "enabled", ("loudnessEnabled", "loudness_enabled"), bool),
        ("delay", "enabled", ("delayEnabled", "delay_enabled"), bool),
        ("bass_enhancer", "enabled", ("bassEnabled", "bass_enabled"), bool),
        ("tone_effect", "enabled", ("toneEffectEnabled", "tone_effect_enabled"), bool),
        ("tone_effect", "mode", ("toneEffectMode", "tone_effect_mode"), str),
    )
    for section, field, names, converter in scalar_fields:
        present, value = supplied(*names)
        if present:
            merged.setdefault(section, {})[field] = converter(value)

    param_fields = (
        ("headroom", "gainDb", ("headroomGainDb", "headroom_gain_db"), float),
        ("autogain", "targetDb", ("autogainTargetDb", "autogain_target_db"), float),
        ("loudness", "fftSize", ("loudnessFftSize", "loudness_fft_size"), int),
        ("loudness", "strength", ("loudnessStrength", "loudness_strength"), str),
        ("delay", "leftMs", ("delayLeftMs", "delay_left_ms"), float),
        ("delay", "rightMs", ("delayRightMs", "delay_right_ms"), float),
        ("bass_enhancer", "amount", ("bassAmount", "bass_amount"), float),
    )
    for section, field, names, converter in param_fields:
        present, value = supplied(*names)
        if present:
            merged.setdefault(section, {}).setdefault("params", {})[field] = converter(value)

    for field, body_name in (
        ("calibration", "calibration"),
        ("calibrationProfiles", "calibrationProfiles"),
    ):
        if body_name in body:
            value = body[body_name]
            if not isinstance(value, dict):
                raise ValueError(f"{body_name} must be an object")
            merged.setdefault("loudness", {}).setdefault("params", {})[field] = copy.deepcopy(value)
    return merged


def is_pure_loudness_strength_change(previous: dict, current: dict) -> bool:
    previous_without_strength = copy.deepcopy(previous)
    current_without_strength = copy.deepcopy(current)
    previous_strength = (
        previous_without_strength.get("loudness", {})
        .get("params", {})
        .pop("strength", None)
    )
    current_strength = (
        current_without_strength.get("loudness", {})
        .get("params", {})
        .pop("strength", None)
    )
    return (
        previous_strength != current_strength
        and previous_without_strength == current_without_strength
        and bool(current.get("loudness", {}).get("enabled"))
    )


def is_runtime_autogain_loudness_change(previous: dict, current: dict) -> bool:
    previous_other = copy.deepcopy(previous)
    current_other = copy.deepcopy(current)
    previous_pair = (
        previous_other.pop("autogain", None),
        previous_other.pop("loudness", None),
    )
    current_pair = (
        current_other.pop("autogain", None),
        current_other.pop("loudness", None),
    )
    return previous_pair != current_pair and previous_other == current_other
