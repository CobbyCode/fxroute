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
import asyncio
import copy
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from audio.output_state_migration import upgrade_output_state
from audio.filter_banks import resolve_bank
from audio.output_state_store import OutputStateStore, StateConflictError
from audio.output_state import default_output_state, validate_output_state
from common.atomic_write import atomic_write_text
from dsp.native_config import layout_from_plan
from dsp.processing_plan import compile_processing_plan
from dsp.surround_global import preset_group

logger = logging.getLogger(__name__)

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
    # preset_loader(mode, bank_id, name) and resolve_ir(bank_id, kernel): every
    # bank has its own presets and IR files, Global one set per output mode.
    preset_loader: Callable[[str, str, str], dict]
    resolve_ir: Callable[[str, str], dict]
    measurement_active: Callable[[], bool]
    global_extras_loader: Callable[[], dict] | None = None


class OutputService:
    def __init__(self, deps: OutputServiceDeps):
        self._deps = deps

    def load(self) -> dict:
        return self._deps.store.load()

    def load_for_activation(self) -> dict:
        """Unwritten editing defaults are not a safe production recovery state."""
        return self._deps.store.load(required=True)

    def ensure_state(self) -> dict:
        """First-initialize a fresh installation's state document.

        Only a document that was never written is initialized: the default
        stereo state becomes its first head, so the first activation has a
        production document to load. A present document is only loaded --
        corrupt bytes stay untouched and an unrenderable draft stays the
        draft -- while a missing document with an applied checkpoint is left
        to activation recovery, which restores the last applied state
        instead of unwritten defaults.
        """
        try:
            return self.load_for_activation()
        except FileNotFoundError:
            if self._applied_path().exists():
                raise
            state = default_output_state()
            try:
                return self._deps.store.commit(state, expected_revision=state["revision"])
            except StateConflictError:
                # Another start initialized the document first.
                return self._deps.store.load()

    def _applied_path(self) -> Path:
        return self._deps.store.path.with_name("output-state-applied.json")

    def activation_token(self) -> bytes | None:
        """Include missing/corrupt heads in freshness checks without normalizing them."""
        try:
            return self._deps.store.path.read_bytes()
        except FileNotFoundError:
            return None

    def require_activation_token(self, token: bytes | None) -> None:
        if self.activation_token() != token:
            raise StateConflictError("Output state changed while preparing the DSP graph")

    def remember_applied(self, state: dict, *, output_key: str, channels: int,
                         hardware_ports: list[str], sample_rate_hz: int,
                         fingerprint: str | None = None) -> None:
        """Checkpoint only a successfully applied production state, never a draft."""
        payload = {"state": validate_output_state(state), "output_key": output_key,
                   "channels": channels, "hardware_ports": list(hardware_ports),
                   "sample_rate_hz": sample_rate_hz,
                   "fingerprint": fingerprint or self.fingerprint(
                       state, output_key=output_key, channels=channels, sample_rate_hz=sample_rate_hz)}
        atomic_write_text(self._applied_path(),
                          json.dumps(payload, sort_keys=True, allow_nan=False) + "\n")

    def load_applied(self, *, output_key: str, channels: int,
                     hardware_ports: list[str]) -> dict:
        """Load the last applied state only for the exact physical output context."""
        return self._applied_record(output_key=output_key, channels=channels,
                                    hardware_ports=hardware_ports)["state"]

    def _applied_record(self, *, output_key: str, channels: int,
                        hardware_ports: list[str]) -> dict:
        payload = json.loads(self._applied_path().read_text(encoding="utf-8"))
        if (not isinstance(payload, dict) or payload.get("output_key") != output_key
                or payload.get("channels") != channels
                or payload.get("hardware_ports") != list(hardware_ports)):
            raise ValueError("Last applied output state belongs to a different device or port layout")
        payload["state"] = upgrade_output_state(payload["state"])
        return payload

    def applied_graph_matches(self, snapshot: dict, *, output_key: str, channels: int,
                              hardware_ports: list[str], sample_rate_hz: int) -> bool:
        """Only a confirmed production graph may survive failed recovery rendering."""
        if not snapshot.get("active") or snapshot.get("measurement_active"):
            return False
        config = snapshot.get("config") or {}
        try:
            payload = self._applied_record(output_key=output_key, channels=channels,
                                           hardware_ports=hardware_ports)
            return bool(payload.get("fingerprint")
                        and config.get("plan_fingerprint") == payload["fingerprint"]
                        and config.get("sample_rate") == payload.get("sample_rate_hz") == sample_rate_hz
                        and config.get("output_key") == output_key
                        and config.get("hardware_ports") == hardware_ports)
        except (OSError, ValueError, KeyError):
            return False

    async def render_for_activation(self, render, *, preflight, output_key: str,
                                    channels: int, hardware_ports: list[str]) -> tuple:
        """Select one fully renderable production state, including native preflight."""
        token = self.activation_token()
        errors = []
        for recovering in (False, True):
            try:
                state = (self.load_applied(output_key=output_key, channels=channels,
                                           hardware_ports=hardware_ports)
                         if recovering else self.load_for_activation())
                targets = await asyncio.to_thread(render, state)
                for target in targets:
                    await preflight(target)
                self.require_activation_token(token)
                if recovering:
                    logger.warning("Restoring last applied v2 output state: head_error=%s", errors[0])
                return state, targets
            except StateConflictError:
                raise
            except Exception as exc:
                self.require_activation_token(token)
                errors.append(str(exc))
        raise RuntimeError("No renderable v2 output state: " + "; ".join(errors))

    def apply(self, mutate: Callable[[dict], dict], *, expected_revision: int) -> dict:
        """Apply a pure state mutation under the measurement and revision guards."""
        if self._deps.measurement_active():
            raise MeasurementActiveError("Measurement is active; output state is locked")
        candidate = mutate(self._deps.store.load())
        return self.commit_unowned(candidate, expected_revision=expected_revision)

    def require_writable(self) -> None:
        """Refuse a write while a measurement owns the DSP graph.

        Create and import endpoints guard their preset file with the same
        check the state commit applies, so nothing is written that could not
        be assigned to its bank.
        """
        if self._deps.measurement_active():
            raise MeasurementActiveError("Measurement is active; output state is locked")

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
                     sample_rate_hz: int, extras_override: dict | None = None) -> dict:
        """Compile the effective processing plan for one device and rate."""
        plan = compile_processing_plan(
            state, output_key=output_key, channels=channels,
            sample_rate_hz=sample_rate_hz, preset_loader=self._deps.preset_loader)
        if not plan["global"]["bypass"]:
            extras = extras_override
            if extras is None and self._deps.global_extras_loader is not None:
                extras = self._deps.global_extras_loader()
            if extras is not None:
                plan["global"]["extras"] = copy.deepcopy(extras)
        return plan

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

    def validate_bank_preset(self, state: dict, mode: str, bank_id: str, preset: str, *, roles=None,
                             group: str | None = None) -> None:
        """Check that a bank has the preset and that it fits the bank's shape.

        Every bank has its own preset library, Global one per output mode;
        a name from another bank raises FileNotFoundError. A Surround Global
        preset is selected for the speaker ``group`` whose filters it holds.
        """
        definition = resolve_bank(state["modes"][mode], bank_id, roles)
        payload = self._deps.preset_loader(mode, definition["id"], preset)
        if definition["id"] == "global":
            held = preset_group(payload)
            if mode == "surround" and preset != "Direct" and held not in (None, group):
                raise ValueError(f'Preset "{preset}" holds {held} filters, not {group or "ungrouped"} filters')
            return
        self.compile_layout({
            "sample_rate_hz": 48000,
            "sub_mode": "stereo" if definition["channel_mode"] == "stereo" else "mono",
            "outputs": [{"role": role, "bank": {"chain": payload["chain"], "bypass": preset == "Direct",
                                                "library": definition["id"]}}
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
