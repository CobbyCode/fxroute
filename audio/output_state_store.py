# SPDX-License-Identifier: AGPL-3.0-only
"""Atomic, revision-checked output-state persistence for a Linux audio host."""

from __future__ import annotations

import fcntl
import json
from pathlib import Path

from audio.output_state import default_output_state, validate_output_state
from common.atomic_write import atomic_write_text


class StateConflictError(ValueError):
    """A candidate no longer describes the currently committed state."""


class OutputStateStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return default_output_state()
        try:
            payload = json.loads(text)
        except (ValueError, RecursionError) as exc:
            raise ValueError("Invalid output state JSON") from exc
        return validate_output_state(payload)

    def commit(self, state: dict, *, expected_revision: int) -> dict:
        candidate = validate_output_state(state)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("Expected revision must be a non-negative integer")
        if candidate["revision"] != expected_revision:
            raise StateConflictError("Candidate was prepared from a different revision")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(self.path.suffix + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            previous = self.load()
            if previous["revision"] != expected_revision:
                raise StateConflictError("Output state changed; prepare the candidate again")
            candidate["revision"] = expected_revision + 1
            atomic_write_text(self.path, json.dumps(candidate, indent=2, sort_keys=True, allow_nan=False) + "\n")
        return candidate
