# SPDX-License-Identifier: AGPL-3.0-only
"""Authoritative per-mode output state: guarded mutations and fingerprints.

The service owns one :class:`OutputStateStore` and serializes mutations
through its revision-checked commit: every change loads the current
document, applies a pure ``audio.output_state`` mutation, and commits
against the caller-provided base revision. Concurrent edits fail with
``StateConflictError`` instead of overwriting each other, and an active
measurement blocks every mutation while its DSP graph is owned elsewhere.

Drafts may be incomplete (routing without an activatable topology); the
transition coordinator gates *activation*, not persistence. Fingerprints
hash the compiled processing plan, so edit selection never affects them
but every audible change does.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

from audio.output_state_migration import migrate_legacy_output_state
from audio.output_state_store import OutputStateStore, StateConflictError
from dsp.processing_plan import compile_processing_plan

__all__ = [
    "MeasurementActiveError",
    "OutputService",
    "OutputServiceDeps",
    "StateConflictError",
]


class MeasurementActiveError(RuntimeError):
    """A mutation was refused because a measurement owns the DSP graph."""


@dataclass(frozen=True)
class OutputServiceDeps:
    """Application services injected by the composition root."""

    store: OutputStateStore
    preset_loader: Callable[[str], dict]
    resolve_ir: Callable[[str], dict]
    measurement_active: Callable[[], bool]
    legacy_snapshot_loader: Callable[[], dict] | None = None


class OutputService:
    def __init__(self, deps: OutputServiceDeps):
        self._deps = deps

    def load(self) -> dict:
        return self._deps.store.load()

    def ensure_state(self) -> dict:
        """Load persisted state, migrating legacy documents exactly once.

        A present file is only ever loaded (corrupt input raises and keeps
        its bytes); a missing file migrates from the legacy snapshot when a
        loader is configured, otherwise the unwritten defaults are returned.
        A lost migration race reloads the winner instead of failing.
        """
        if self._deps.store.path.exists():
            return self._deps.store.load()
        if self._deps.legacy_snapshot_loader is None:
            return self._deps.store.load()
        try:
            return self._deps.store.commit(
                migrate_legacy_output_state(**self._deps.legacy_snapshot_loader()),
                expected_revision=0)
        except StateConflictError:
            return self._deps.store.load()

    def apply(self, mutate: Callable[[dict], dict], *, expected_revision: int) -> dict:
        """Apply a pure state mutation under the measurement and revision guards."""
        if self._deps.measurement_active():
            raise MeasurementActiveError("Measurement is active; output state is locked")
        return self._deps.store.commit(
            mutate(self._deps.store.load()), expected_revision=expected_revision)

    def fingerprint(self, state: dict, *, output_key: str, channels: int,
                    sample_rate_hz: int) -> str:
        """Hash the effective processing for one device and rate.

        Covers mode, roles, crossover filters, bank chains, trims and
        helpers; bank *selection* is intentionally excluded. Two commits
        with equal fingerprints drive identical DSP graphs on the device.
        """
        plan = compile_processing_plan(
            state, output_key=output_key, channels=channels,
            sample_rate_hz=sample_rate_hz, preset_loader=self._deps.preset_loader)
        canonical = json.dumps(plan, sort_keys=True, separators=(",", ":"),
                               allow_nan=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
