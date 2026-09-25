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
from audio.filter_banks import resolve_bank
from audio.output_state_store import OutputStateStore, StateConflictError
from dsp.native_config import layout_from_plan
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
        candidate = mutate(self._deps.store.load())
        return self.commit_unowned(candidate, expected_revision=expected_revision)

    def commit_unowned(self, candidate: dict, *, expected_revision: int) -> dict:
        """Commit a prepared candidate only while no measurement owns the graph.

        Interactive edits prepare across awaits; a measurement job that took
        ownership meanwhile must win, so the guard runs again at the commit.
        """
        if self._deps.measurement_active():
            raise MeasurementActiveError("Measurement is active; output state is locked")
        return self.commit(candidate, expected_revision=expected_revision)

    def commit(self, candidate: dict, *, expected_revision: int) -> dict:
        """Commit a prepared candidate (see apply for the guarded variant).

        Separate load/mutate/commit phases enable two-stage transitions:
        prepare and verify first, commit only after the runtime readback.
        """
        return self._deps.store.commit(candidate, expected_revision=expected_revision)

    def revert(self, document: dict, *, expected_revision: int) -> dict:
        """Recommit a previous document as a new revision (rollback path).

        The document keeps its content but is rebased onto the expected
        head revision, so concurrent commits still conflict instead of
        being silently rewound.
        """
        candidate = dict(document)
        candidate["revision"] = expected_revision
        return self.commit(candidate, expected_revision=expected_revision)

    def compile_plan(self, state: dict, *, output_key: str, channels: int,
                     sample_rate_hz: int) -> dict:
        """Compile the effective processing plan for one device and rate."""
        return compile_processing_plan(
            state, output_key=output_key, channels=channels,
            sample_rate_hz=sample_rate_hz, preset_loader=self._deps.preset_loader)

    def compile_alignment_plan(self, state: dict, *, output_key: str, channels: int,
                               sample_rate_hz: int) -> dict:
        """Compile the neutralized alignment measurement plan.

        Shared AutoSub/Speaker alignment rendering: crossover filters,
        output trims and routing stay active, Global and area PEQ/convolver
        banks render bypassed. Physical Delay/Gain tuning is measured
        before any PEQ/convolver correction.
        """
        return compile_processing_plan(
            state, output_key=output_key, channels=channels,
            sample_rate_hz=sample_rate_hz, preset_loader=self._deps.preset_loader,
            neutralize_banks=True)

    def compile_layout(self, plan: dict) -> list[dict]:
        """Map a compiled plan to a native engine output layout."""
        return layout_from_plan(plan, resolve_ir=self._deps.resolve_ir)

    def validate_bank_preset(self, state: dict, mode: str, bank_id: str, preset: str, *, roles=None) -> None:
        """Check a preset against its bank's shape and owning bank.

        Each concrete bank (and Global) owns its presets; All Banks owns
        none. Built-ins and legacy files without a bank tag stay assignable
        everywhere, bank-tagged files only in their owning bank.
        """
        definition = resolve_bank(state["modes"][mode], bank_id, roles)
        payload = self._deps.preset_loader(preset)
        if preset not in ("Direct", "Neutral"):
            metadata = payload.get("metadata") if isinstance(payload, dict) else None
            tag = metadata.get("bank") if isinstance(metadata, dict) else None
            if isinstance(tag, str) and tag.strip() and tag.strip() != definition["id"]:
                raise ValueError(
                    f'Preset "{preset}" belongs to bank "{tag.strip()}", not "{definition["id"]}"')
        if definition["id"] == "global":
            return
        self.compile_layout({
            "sample_rate_hz": 48000,
            "sub_mode": "stereo" if definition["channel_mode"] == "stereo" else "mono",
            "outputs": [{"role": role, "bank": {"chain": payload["chain"], "bypass": preset == "Direct"}}
                        for role in definition["roles"]],
        })

    def fingerprint(self, state: dict, *, output_key: str, channels: int,
                    sample_rate_hz: int) -> str:
        """Hash the effective processing for one device and rate.

        Covers mode, roles, crossover filters, bank chains, trims and
        helpers; bank *selection* is intentionally excluded. Two commits
        with equal fingerprints drive identical DSP graphs on the device.
        """
        return self.fingerprint_plan(
            self.compile_plan(state, output_key=output_key, channels=channels,
                              sample_rate_hz=sample_rate_hz))

    def fingerprint_plan(self, plan: dict) -> str:
        """Hash a compiled plan without recompiling it."""
        canonical = json.dumps(plan, sort_keys=True, separators=(",", ":"),
                               allow_nan=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
