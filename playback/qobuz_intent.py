# SPDX-License-Identifier: AGPL-3.0-only

"""Persisted Qobuz user intent (queue repeat mode and transport state).

qbzd keeps no local session state: every daemon start re-joins the Qobuz
Connect session and adopts the queue, repeat mode and current track that
session holds (live-verified on .104: after a power cycle the 27-track queue
came back with repeat ``One`` and the fork started playing a track that had
been paused before the restart). A repeat mode or a pause set through FXRoute
therefore only exists in the running daemon unless FXRoute restores it after
the (re)join.

This store holds the last intent FXRoute observed from the daemon so
:mod:`playback.qobuz_join_guard` can re-apply it once per session join. The
file lives next to the other FXRoute state (``~/.config/fxroute``) and an
absent or unreadable file simply means "no intent yet": nothing is restored
and the join behaves as before.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# FXRoute loop vocabulary shared with the provider (Spotify parity).
LOOP_MODES = ("none", "track", "playlist")

_FILE_NAME = "qobuz-intent.json"

# Observers run on the watch loops and the broadcaster at the same time.
_file_lock = threading.RLock()


def _config_dir() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return root / "fxroute"


def intent_path() -> Path:
    """Return the intent file path."""
    return _config_dir() / _FILE_NAME


def normalize_loop(value: object) -> str | None:
    """Return the FXRoute loop mode for a raw value, or ``None`` when unknown."""
    normalized = str(value or "").strip().lower()
    return normalized if normalized in LOOP_MODES else None


@dataclass(frozen=True)
class QobuzIntent:
    """The user intent FXRoute last observed for Qobuz."""

    loop: str
    playing: bool


def _parse(payload: object) -> QobuzIntent | None:
    if not isinstance(payload, dict):
        return None
    loop = normalize_loop(payload.get("loop"))
    if loop is None:
        return None
    return QobuzIntent(loop=loop, playing=bool(payload.get("playing")))


def load(path: Path | None = None) -> QobuzIntent | None:
    """Return the stored intent, or ``None`` when absent or unusable."""
    target = Path(path) if path is not None else intent_path()
    with _file_lock:
        try:
            raw = target.read_text(encoding="utf-8")
        except OSError:
            return None
    try:
        payload = json.loads(raw)
    except ValueError:
        logger.warning("Qobuz intent file is not valid JSON: %s", target)
        return None
    intent = _parse(payload)
    if intent is None:
        logger.warning("Qobuz intent file carries no usable loop mode: %s", target)
    return intent


def save(intent: QobuzIntent, path: Path | None = None) -> None:
    """Write the intent atomically; a missing directory is created."""
    target = Path(path) if path is not None else intent_path()
    payload = {"loop": intent.loop, "playing": bool(intent.playing)}
    with _file_lock:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".tmp")
        temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, target)


def observe(*, loop: str, playing: bool, path: Path | None = None) -> QobuzIntent | None:
    """Record one observed daemon state; writes only when it changed.

    Returns the intent now in force, or ``None`` when the observed loop mode is
    unknown (a daemon that is down or mid-restart must never overwrite a valid
    intent). Write failures are logged and never raised: intent persistence is
    a recovery aid, it must not break the state broadcast that drives the UI.
    """
    normalized = normalize_loop(loop)
    if normalized is None:
        return None
    intent = QobuzIntent(loop=normalized, playing=bool(playing))
    if load(path) == intent:
        return intent
    try:
        save(intent, path)
    except OSError as exc:
        logger.warning("Qobuz intent could not be persisted: %s", exc)
    return intent


def clear(path: Path | None = None) -> None:
    """Drop the stored intent."""
    target = Path(path) if path is not None else intent_path()
    with _file_lock:
        try:
            target.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning("Qobuz intent could not be removed: %s", exc)
