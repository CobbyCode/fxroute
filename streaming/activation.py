# SPDX-License-Identifier: AGPL-3.0-only

"""Persisted per-provider enable/disable state (Settings -> Providers).

"Installed" is a fact about the machine (binaries/services on disk); "enabled"
is the operator's choice in the Settings surface: an enabled provider shows its
tab and participates in the UI, a disabled one is hidden but stays installed.

The state is one small JSON file under the FXRoute config dir so it survives
restarts without touching .env (pydantic Settings keys) or install state.
Unknown providers are ignored on read; missing file means "all enabled", which
keeps existing installs unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

STATE_FILENAME = "provider-activation.json"


def _state_path() -> Path:
    config_dir = Path(os.environ.get("FXROUTE_CONFIG_DIR", "")) if os.environ.get("FXROUTE_CONFIG_DIR") else Path.home() / ".config" / "fxroute"
    return config_dir / STATE_FILENAME


def _normalize_id(provider_id: Any) -> str:
    return str(provider_id or "").strip()


def _read_raw() -> dict:
    path = _state_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("Provider activation state unreadable (%s): %s", path, exc)
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _write_raw(payload: dict) -> None:
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".provider-activation.")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=1, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    except OSError as exc:
        logger.warning("Provider activation state could not be written (%s): %s", path, exc)


def is_enabled(provider_id: str) -> bool:
    """Return the persisted enabled flag; absent entries default to enabled."""
    raw = _read_raw()
    entry = raw.get(_normalize_id(provider_id))
    if not isinstance(entry, dict):
        return True
    return entry.get("enabled") is not False


def set_enabled(provider_id: str, enabled: bool) -> None:
    """Persist the enabled flag for one provider (creating the entry if needed)."""
    key = _normalize_id(provider_id)
    if not key:
        return
    raw = _read_raw()
    entry = raw.get(key) if isinstance(raw.get(key), dict) else {}
    entry["enabled"] = bool(enabled)
    raw[key] = entry
    _write_raw(raw)


def enabled_map(provider_ids: list[str] | tuple[str, ...]) -> dict[str, bool]:
    """Return {provider_id: enabled} for the given known provider ids."""
    return {pid: is_enabled(pid) for pid in provider_ids}
