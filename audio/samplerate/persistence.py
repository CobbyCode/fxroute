# SPDX-License-Identifier: AGPL-3.0-only

"""Config paths, JSON persistence, and policy/configuration normalization."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from common.atomic_write import atomic_write_text

from .capability import playable_rate
from .constants import (
    SAMPLE_RATE_CANDIDATES,
    SOURCE_MODE_APP_PLAYBACK,
    SOURCE_MODE_BLUETOOTH_INPUT,
    SOURCE_MODE_EXTERNAL_INPUT,
    SOURCE_MODE_STDIN_INPUT,
)
from .parsing import _parse_pipewire_clock_rate_dropin, _safe_int


def _audio_output_selection_path() -> Path:
    config_root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return config_root / "fxroute" / "audio-output-selection.json"

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


def playback_target_rate(source_rate: int | None, policy: Mapping[str, Any] | None = None) -> int | None:
    """Return the graph rate for a source: the policy rate fitted to the output.

    A rate the selected output supports stays native; an unsupported one
    falls back to the highest supported rate below it (see
    :func:`audio.samplerate.capability.playable_rate`).
    """
    return playable_rate(effective_playback_rate(source_rate, policy))


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
        "mode": mode if mode in {SOURCE_MODE_APP_PLAYBACK, SOURCE_MODE_EXTERNAL_INPUT, SOURCE_MODE_BLUETOOTH_INPUT, SOURCE_MODE_STDIN_INPUT} else SOURCE_MODE_APP_PLAYBACK,
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
