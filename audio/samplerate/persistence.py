# SPDX-License-Identifier: AGPL-3.0-only

"""Config paths, JSON persistence, and policy/configuration normalization."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from .constants import (
    OUTPUT_MODE_STEREO,
    OUTPUT_MODE_SUBWOOFER_21,
    OUTPUT_MODE_SUBWOOFER_22_MODES,
    OUTPUT_MODE_SUBWOOFER_22_STEREO,
    OUTPUT_MODE_SUBWOOFER_MODES,
    SAMPLE_RATE_CANDIDATES,
    SOURCE_MODE_APP_PLAYBACK,
    SOURCE_MODE_BLUETOOTH_INPUT,
    SOURCE_MODE_EXTERNAL_INPUT,
)
from .parsing import _parse_pipewire_clock_rate_dropin, _safe_int


def _audio_output_selection_path() -> Path:
    config_root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return config_root / "fxroute" / "audio-output-selection.json"

def _audio_output_mode_path() -> Path:
    config_root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return config_root / "fxroute" / "audio-output-mode.json"

def _sample_rate_policy_path() -> Path:
    config_root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return config_root / "fxroute" / "sample-rate-policy.json"

def _pipewire_clock_rate_dropin_path() -> Path:
    config_root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return config_root / "pipewire" / "pipewire.conf.d" / "90-fxroute-clock-rate.conf"

def _audio_source_selection_path() -> Path:
    config_root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return config_root / "fxroute" / "audio-source-selection.json"

def _load_audio_output_selection() -> dict[str, Any]:
    path = _audio_output_selection_path()
    if not path.exists():
        return {"selected_key": None}
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return {"selected_key": None}
    selected_key = payload.get("selected_key")
    return {
        "selected_key": selected_key if isinstance(selected_key, str) and selected_key else None,
    }

def load_sample_rate_policy() -> dict[str, Any]:
    path = _sample_rate_policy_path()
    try:
        payload = json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        payload = {}
    mode = str(payload.get("mode") or "auto").strip().lower()
    rate = _safe_int(payload.get("rate"))
    if mode != "fixed" or rate not in SAMPLE_RATE_CANDIDATES:
        return {"mode": "auto", "rate": None}
    return {"mode": "fixed", "rate": rate}

def normalize_sample_rate_policy(mode: Any, rate: Any = None) -> dict[str, Any]:
    normalized_mode = str(mode or "auto").strip().lower()
    if normalized_mode == "auto":
        return {"mode": "auto", "rate": None}
    if normalized_mode != "fixed":
        raise ValueError("Sample rate policy must be auto or fixed")
    normalized_rate = _safe_int(rate)
    if normalized_rate not in SAMPLE_RATE_CANDIDATES:
        raise ValueError("Unsupported fixed sample rate")
    return {"mode": "fixed", "rate": normalized_rate}

def persist_sample_rate_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    normalized = normalize_sample_rate_policy(policy.get("mode"), policy.get("rate"))
    path = _sample_rate_policy_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(normalized, indent=2) + "\n")
    return normalized

def effective_playback_rate(source_rate: int | None, policy: Mapping[str, Any] | None = None) -> int | None:
    current = dict(policy or load_sample_rate_policy())
    fixed_rate = current.get("rate") if current.get("mode") == "fixed" else None
    if isinstance(fixed_rate, int) and fixed_rate > 0:
        return fixed_rate
    return source_rate if isinstance(source_rate, int) and source_rate > 0 else None

def _normalize_single_sub_config(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize one sub's {level_db, alignment_ms, polarity} for 2.2."""
    payload = payload or {}
    try:
        level = round(float(payload.get("level_db", 0.0)), 1)
    except (TypeError, ValueError):
        level = 0.0
    try:
        alignment = round(float(payload.get("alignment_ms", 0.0)), 2)
    except (TypeError, ValueError):
        alignment = 0.0
    polarity = str(payload.get("polarity", "normal") or "normal").strip().lower()
    level = max(-80.0, min(12.0, level))
    alignment = max(-40.0, min(40.0, alignment))
    return {
        "level_db": level,
        "alignment_ms": alignment,
        "polarity": "invert" if polarity in {"invert", "inverted", "180"} else "normal",
    }

def _normalize_subwoofer_22_config(
    subwoofers: dict[str, Any] | None = None,
    fallback_21: dict[str, Any] | None = None,
    global_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize 2.2 subwoofer config.

    sub1 derives from fallback_21 when subwoofers.sub1 is missing.
    Global fields (crossover, slope, highpass) come from fallback_21 or
    defaults, unless ``global_override`` supplies explicit durable fields
    (the top-level 2.2 payload wins over the legacy 2.1 block).
    """
    subwoofers = subwoofers or {}
    override = global_override or {}

    # Global fields from fallback_21 (existing 2.1 subwoofer block) or defaults
    if fallback_21:
        raw_freq = fallback_21.get("crossover_frequency_hz", 80)
        raw_hp = fallback_21.get("main_highpass_enabled", True)
    else:
        raw_freq = 80
        raw_hp = True
    raw_freq = override.get("crossover_frequency_hz", raw_freq)
    raw_hp = override.get("main_highpass_enabled", raw_hp)

    try:
        frequency = int(round(float(raw_freq)))
    except (TypeError, ValueError):
        frequency = 80

    # Sub 1: from subwoofers.sub1 or derived from 2.1 config
    sub1_payload = subwoofers.get("sub1") if isinstance(subwoofers.get("sub1"), dict) else None
    if sub1_payload is None and fallback_21 is not None:
        sub1_payload = {
            "level_db": fallback_21.get("sub_level_db", 0.0),
            "alignment_ms": fallback_21.get("sub_alignment_ms", 0.0),
            "polarity": fallback_21.get("sub_polarity", "normal"),
        }
    sub1 = _normalize_single_sub_config(sub1_payload)

    # Sub 2: from subwoofers.sub2 or defaults
    sub2 = _normalize_single_sub_config(
        subwoofers.get("sub2") if isinstance(subwoofers.get("sub2"), dict) else None
    )

    return {
        "crossover_frequency_hz": max(40, min(200, frequency)),
        "slope": "LR24",
        "main_highpass_enabled": bool(raw_hp),
        "sub1": sub1,
        "sub2": sub2,
    }

def _normalize_subwoofer_config(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    raw_frequency = payload.get("crossover_frequency_hz", payload.get("crossoverFrequencyHz", 80))
    raw_level = payload.get("sub_level_db", payload.get("subLevelDb", 0.0))
    raw_alignment = payload.get("sub_alignment_ms", payload.get("subAlignmentMs", 0.0))
    try:
        frequency = int(round(float(raw_frequency)))
    except (TypeError, ValueError):
        frequency = 80
    try:
        level = round(float(raw_level), 1)
    except (TypeError, ValueError):
        level = 0.0
    try:
        alignment = round(float(raw_alignment), 2)
    except (TypeError, ValueError):
        alignment = 0.0
    polarity = str(payload.get("sub_polarity", payload.get("subPolarity", "normal")) or "normal").strip().lower()
    alignment = max(-40.0, min(40.0, alignment))
    return {
        "crossover_frequency_hz": max(40, min(200, frequency)),
        "slope": "LR24",
        "main_highpass_enabled": bool(payload.get("main_highpass_enabled", payload.get("mainHighpassEnabled", True))),
        "sub_level_db": max(-24.0, min(12.0, level)),
        "sub_alignment_ms": alignment,
        "sub_polarity": "invert" if polarity in {"invert", "inverted", "180"} else "normal",
    }

def _subwoofer_22_storage_key(mode: str) -> str:
    """Return the config JSON key for mode-specific 2.2 subwoofer data."""
    if mode == OUTPUT_MODE_SUBWOOFER_22_STEREO:
        return "subwoofers_22_stereo"
    return "subwoofers_22"

def _load_audio_output_mode() -> dict[str, Any]:
    path = _audio_output_mode_path()
    default_payload = {
        "mode": OUTPUT_MODE_STEREO,
        "subwoofer": _normalize_subwoofer_config(None),
    }
    if not path.exists():
        return default_payload
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return default_payload
    mode = payload.get("mode")

    if mode in OUTPUT_MODE_SUBWOOFER_22_MODES:
        # Read from mode-specific key; fall back to shared subwoofers (BC)
        storage_key = _subwoofer_22_storage_key(mode)
        source_subwoofers = payload.get(storage_key)
        if not isinstance(source_subwoofers, dict):
            source_subwoofers = payload.get("subwoofers")
        normalized = _normalize_subwoofer_22_config(
            source_subwoofers,
            payload.get("subwoofer"),
            payload,
        )
        return {
            "mode": mode,
            "crossover_frequency_hz": normalized["crossover_frequency_hz"],
            "slope": normalized["slope"],
            "main_highpass_enabled": normalized["main_highpass_enabled"],
            "subwoofers": {
                "sub1": normalized["sub1"],
                "sub2": normalized["sub2"],
            },
        }

    return {
        "mode": mode if mode in {OUTPUT_MODE_STEREO, OUTPUT_MODE_SUBWOOFER_21} else OUTPUT_MODE_STEREO,
        "subwoofer": _normalize_subwoofer_config(payload.get("subwoofer") if isinstance(payload.get("subwoofer"), dict) else {}),
    }

def _load_audio_source_selection() -> dict[str, Any]:
    path = _audio_source_selection_path()
    if not path.exists():
        return {"mode": SOURCE_MODE_APP_PLAYBACK, "selected_input_key": None}
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return {"mode": SOURCE_MODE_APP_PLAYBACK, "selected_input_key": None}
    mode = payload.get("mode")
    selected_input_key = payload.get("selected_input_key")
    return {
        "mode": mode if mode in {SOURCE_MODE_APP_PLAYBACK, SOURCE_MODE_EXTERNAL_INPUT, SOURCE_MODE_BLUETOOTH_INPUT} else SOURCE_MODE_APP_PLAYBACK,
        "selected_input_key": selected_input_key if isinstance(selected_input_key, str) and selected_input_key else None,
    }

def _load_pipewire_clock_rate_config() -> dict[str, Any]:
    path = _pipewire_clock_rate_dropin_path()
    if not path.exists():
        return {
            "configured_default_rate": None,
            "configured_allowed_rates": [],
            "config_path": str(path),
            "config_exists": False,
        }
    try:
        parsed = _parse_pipewire_clock_rate_dropin(path.read_text())
    except Exception:
        parsed = {
            "configured_default_rate": None,
            "configured_allowed_rates": [],
        }
    parsed.update({
        "config_path": str(path),
        "config_exists": True,
    })
    return parsed

def _save_audio_output_selection(selected_key: str) -> None:
    path = _audio_output_selection_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "selected_key": selected_key,
    }, indent=2) + "\n")

def _build_audio_output_mode_payload(
    mode: str,
    subwoofer: dict[str, Any] | None = None,
    subwoofers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    valid_modes = {OUTPUT_MODE_STEREO, *OUTPUT_MODE_SUBWOOFER_MODES}
    normalized_mode = mode if mode in valid_modes else OUTPUT_MODE_STEREO

    # Load existing config to preserve the other mode's block
    existing: dict[str, Any] = {}
    path = _audio_output_mode_path()
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except Exception:
            pass
    previous = _load_audio_output_mode()

    if normalized_mode in OUTPUT_MODE_SUBWOOFER_22_MODES:
        storage_key = _subwoofer_22_storage_key(normalized_mode)
        if subwoofers is None:
            target_subwoofers = existing.get(storage_key)
            if not isinstance(target_subwoofers, dict):
                target_subwoofers = existing.get("subwoofers")
            subwoofers = target_subwoofers
        if subwoofer is None and isinstance(existing.get("subwoofer"), dict):
            subwoofer = existing.get("subwoofer")
        if subwoofer is None and isinstance(previous.get("subwoofer"), dict):
            subwoofer = previous.get("subwoofer")
        normalized = _normalize_subwoofer_22_config(subwoofers, subwoofer or existing.get("subwoofer"))
        subwoofer_22_payload = {
            "sub1": normalized["sub1"],
            "sub2": normalized["sub2"],
        }
        # Determine the other 2.2 storage key to preserve when saving
        other_storage_keys = ["subwoofers_22", "subwoofers_22_stereo"]
        try:
            other_storage_keys.remove(storage_key)
        except ValueError:
            pass
        payload: dict[str, Any] = {
            "mode": normalized_mode,
            "crossover_frequency_hz": normalized["crossover_frequency_hz"],
            "slope": normalized["slope"],
            "main_highpass_enabled": normalized["main_highpass_enabled"],
            storage_key: subwoofer_22_payload,
            # Keep shared subwoofers for BC (other 2.2 modes can migrate from it)
            "subwoofers": subwoofer_22_payload,
        }
        # Preserve existing other 2.2 mode's subwoofers block
        for other_key in other_storage_keys:
            if other_key in existing:
                payload[other_key] = existing[other_key]
        if "device_modes" in existing:
            payload["device_modes"] = existing["device_modes"]
        # Preserve existing 2.1 subwoofer block for BC. The 2.2 save owns the
        # global fields, so keep the legacy block's crossover/highpass in sync
        # with the top-level 2.2 payload; otherwise a later 2.1 migration (or
        # the legacy readback path) would resurrect the stale value.
        if "subwoofer" in existing:
            if isinstance(existing.get("subwoofer"), dict):
                payload["subwoofer"] = {
                    **existing["subwoofer"],
                    "crossover_frequency_hz": normalized["crossover_frequency_hz"],
                    "main_highpass_enabled": normalized["main_highpass_enabled"],
                }
            else:
                payload["subwoofer"] = existing["subwoofer"]
    else:
        if subwoofer is None and isinstance(existing.get("subwoofer"), dict):
            subwoofer = existing.get("subwoofer")
        if subwoofer is None and isinstance(previous.get("subwoofer"), dict):
            subwoofer = previous.get("subwoofer")
        payload = {
            "mode": normalized_mode,
            "subwoofer": _normalize_subwoofer_config(subwoofer),
        }
        # Preserve existing 2.2 subwoofers blocks for BC
        for bc_key in ("subwoofers", "subwoofers_22", "subwoofers_22_stereo"):
            if bc_key in existing:
                payload[bc_key] = existing[bc_key]
        if "device_modes" in existing:
            payload["device_modes"] = existing["device_modes"]

    return payload

def _save_audio_source_selection(mode: str, selected_input_key: str | None) -> None:
    path = _audio_source_selection_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "mode": mode,
        "selected_input_key": selected_input_key,
    }, indent=2) + "\n")

def _load_raw_audio_output_mode() -> dict[str, Any]:
    """Load raw config payload without normalization. Returns {} on failure."""
    path = _audio_output_mode_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}

def _load_device_output_modes() -> dict[str, str]:
    """Load the last-valid-mode map per selected output device key.

    The map is a hint only: callers must still verify that the currently
    recognized device can carry the remembered mode and degrade otherwise.
    Only known modes with non-empty string keys are returned.
    """
    device_modes = _load_raw_audio_output_mode().get("device_modes")
    if not isinstance(device_modes, dict):
        return {}
    valid_modes = {OUTPUT_MODE_STEREO, *OUTPUT_MODE_SUBWOOFER_MODES}
    return {
        str(key): str(value)
        for key, value in device_modes.items()
        if isinstance(key, str) and key.strip()
        and isinstance(value, str) and value in valid_modes
    }

