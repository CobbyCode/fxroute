# SPDX-License-Identifier: AGPL-3.0-only

"""Config paths, JSON persistence, and policy/configuration normalization."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from common.atomic_write import atomic_write_text

from .constants import (
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
    atomic_write_text(path, json.dumps(normalized, indent=2) + "\n")
    return normalized

def effective_playback_rate(source_rate: int | None, policy: Mapping[str, Any] | None = None) -> int | None:
    # Rate decides, tier follows: the coordinator reprobes profiled devices
    # into the target rate's native tier, so auto keeps the raw source rate
    # instead of mapping it into the current band.
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
    atomic_write_text(path, json.dumps({
        "selected_key": selected_key,
    }, indent=2) + "\n")


def _save_audio_source_selection(mode: str, selected_input_key: str | None) -> None:
    path = _audio_source_selection_path()
    atomic_write_text(path, json.dumps({
        "mode": mode,
        "selected_input_key": selected_input_key,
    }, indent=2) + "\n")
