# SPDX-License-Identifier: AGPL-3.0-only
"""Own one frozen AutoSub start, runtime candidates and an explicit winner commit.

Construction is inert. Activation is deferred until the measurement session has
resolved its processing rate. No readiness check rebuilds persisted output state.
The caller still owns serialization against other graph owners and must decide
acoustic acceptance before requesting a winner commit.
"""

from __future__ import annotations

import asyncio
import copy
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from audio.output_service import OutputService
from audio.output_state_store import StateConflictError
from dsp.runtime import PlannedSyncTarget
from measurement.autosub.candidate_state import CandidateStager, sub_candidate_state
from measurement.autosub.roles import autosub_topology_from_state


@dataclass(frozen=True)
class AutoSubProposal:
    """Detached, absolute sub settings; routed-role validation happens in the owner.

    All four maps are mandatory. Frozen fields prevent replacement, while the
    owner takes another detached copy for validation before crossing any await.
    """

    sub_delays: dict
    sub_levels: dict
    sub_polarities: dict
    bass: dict

    def __post_init__(self) -> None:
        for name in ("sub_delays", "sub_levels", "sub_polarities", "bass"):
            object.__setattr__(self, name, copy.deepcopy(getattr(self, name)))

    def validated_knobs(self, sub_roles: tuple[str, ...]) -> dict:
        """Require exact role sets and both bass fields, returning detached maps.

        Completeness is enforced by the session, not sub_candidate_state: that
        lower-level helper checks only that supplied roles are actively routed
        and intentionally accepts partial maps. Values are normalized by it.
        """
        knobs = {}
        for name in ("sub_delays", "sub_levels", "sub_polarities"):
            values = getattr(self, name)
            if not isinstance(values, dict) or set(values) != set(sub_roles):
                raise ValueError(f"{name} must contain every routed sub role and no other roles")
            knobs[name] = copy.deepcopy(values)
        if (not isinstance(self.bass, dict)
                or set(self.bass) != {"frequency_hz", "main_highpass_enabled"}):
            raise ValueError("Candidate bass requires only frequency_hz and main_highpass_enabled")
        knobs["bass"] = copy.deepcopy(self.bass)
        return knobs


class AutoSubCandidateSession:
    """A rate-bound CandidateStager plus revision-checked winner ownership.

    Callback contracts are exactly CandidateStager's: build_target(plan, *,
    fingerprint) is synchronous and render-only, returning PlannedSyncTarget;
    guarded_stage honors the guarded_rebuild_rendered signature, revision vetoes,
    before-ramp verification hooks and explicit forward/rollback gain targets.
    Async readback must verify physical links/liveness and return ``active``,
    ``config.plan_fingerprint``, ``config.sample_rate`` and finite
    ``output_gain_db`` in [-80, 0]. It must never synchronize persisted state.

    The owner lock serializes its asynchronous operations, not external owners.
    Once committed the stager is retired: it must never restore the old start.
    """

    def __init__(self, *, service: OutputService, start_state: dict,
                 output_key: str, channels: int,
                 build_target: Callable[..., PlannedSyncTarget],
                 guarded_stage: Callable[..., Awaitable[None]],
                 readback: Callable[[], Awaitable[dict]], guard_db: float = -30):
        if (type(guard_db) not in (int, float) or not math.isfinite(guard_db)
                or not -80 <= guard_db <= 0):
            raise ValueError("Candidate guard must be finite and between -80 and 0 dB")
        if not all(callable(callback) for callback in (build_target, guarded_stage, readback)):
            raise ValueError("Candidate runtime boundaries must be callable")
        self._start_state = sub_candidate_state(
            start_state, output_key=output_key, channels=channels)
        self._sub_roles = autosub_topology_from_state(
            self._start_state, output_key=output_key, channels=channels).sub_roles
        self._revision = self._start_state["revision"]
        self._service = service
        self._context = dict(output_key=output_key, channels=channels)
        self._build_target = build_target
        self._guarded_stage = guarded_stage
        self._readback = readback
        self._guard_db = guard_db
        self._stager: CandidateStager | None = None
        self._fingerprint: str | None = None
        self._initial_fingerprint: str | None = None
        # One revision/rate per owner: retain initial + current, and at most one
        # pending render while staging. Reuse targets for identical fingerprints
        # so the stager's no-op path cannot report a newly resolved, unused IR.
        self._targets: dict[str, tuple[PlannedSyncTarget, list[dict]]] = {}
        self._sample_rate_hz: int | None = None
        self._current: dict | None = None
        self._committed = False
        self._lock = asyncio.Lock()

    @property
    def committed(self) -> bool:
        return self._committed

    def _check_revision(self) -> None:
        if self._service.load()["revision"] != self._revision:
            raise StateConflictError("AutoSub start revision is stale; no rebase or rollback allowed")

    def _require_uncommitted(self) -> None:
        if self._committed:
            raise RuntimeError("AutoSub candidate session is committed; its stager is retired")

    def _require_stager(self) -> CandidateStager:
        self._require_uncommitted()
        if self._stager is None:
            raise RuntimeError("AutoSub candidate session is not activated")
        return self._stager

    @staticmethod
    def _validate_rate(sample_rate_hz: int) -> None:
        if type(sample_rate_hz) is not int or sample_rate_hz <= 0:
            raise ValueError("AutoSub sample rate must be a positive integer")

    def activate(self, sample_rate_hz: int) -> None:
        """Bind once at the resolved rate; compile/render only, never change graph."""
        self._require_uncommitted()
        self._validate_rate(sample_rate_hz)
        if self._stager is not None:
            raise RuntimeError("AutoSub candidate session is already activated")
        self._check_revision()
        initial_fingerprint = None

        def build_target(plan, *, fingerprint):
            nonlocal initial_fingerprint
            initial_fingerprint = fingerprint
            if fingerprint not in self._targets:
                target = self._build_target(plan, fingerprint=fingerprint)
                layout = copy.deepcopy(list(target.config.layout))
                self._targets[fingerprint] = (target, layout)
            return self._targets[fingerprint][0]

        try:
            stager = CandidateStager(
                service=self._service, start_state=self._start_state,
                sample_rate_hz=sample_rate_hz, build_target=build_target,
                guarded_stage=self._guarded_stage, readback=self._readback,
                guard_db=self._guard_db, **self._context)
            self._check_revision()
        except BaseException:
            self._targets.clear()
            raise
        self._stager = stager
        self._initial_fingerprint = initial_fingerprint
        self._fingerprint = initial_fingerprint
        self._sample_rate_hz = sample_rate_hz

    def _proposal_knobs(self, proposal: AutoSubProposal) -> dict:
        if type(proposal) is not AutoSubProposal:
            raise ValueError("AutoSub requires a complete AutoSubProposal")
        return proposal.validated_knobs(self._sub_roles)

    def _record_result(self, result: dict) -> dict:
        self._current = copy.deepcopy(result)
        self._fingerprint = result["fingerprint"]
        return copy.deepcopy({
            **result,
            "expected_native_layout": self._targets[result["fingerprint"]][1],
            "expected_native_output_mode": result["plan"]["mode"],
        })

    def _prune_targets(self) -> None:
        retained = {self._initial_fingerprint, self._fingerprint}
        self._targets = {key: value for key, value in self._targets.items() if key in retained}

    async def stage(self, proposal: AutoSubProposal) -> dict:
        """Stage a complete start-relative proposal and return detached sweep context."""
        async with self._lock:
            stager = self._require_stager()
            self._check_revision()
            knobs = self._proposal_knobs(proposal)
            try:
                return self._record_result(await stager.stage(**knobs))
            finally:
                self._prune_targets()

    async def ensure_ready(self, rate: int) -> dict:
        """Verify the current graph and frozen revision without any synchronization."""
        async with self._lock:
            return await self._ensure_ready_unlocked(rate)

    async def _ensure_ready_unlocked(self, rate: int) -> dict:
        """Caller holds the owner lock, including through a subsequent commit."""
        self._require_stager()
        self._validate_rate(rate)
        self._check_revision()
        snapshot = await self._readback()
        self._check_revision()
        config = snapshot.get("config") if isinstance(snapshot, dict) else None
        if (not isinstance(config, dict) or snapshot.get("active") is not True
                or config.get("plan_fingerprint") != self._fingerprint):
            raise RuntimeError("AutoSub runtime readback is inactive or has a different fingerprint")
        if config.get("sample_rate") != rate or self._sample_rate_hz != rate:
            raise RuntimeError("AutoSub runtime sample rate does not match the resolved rate")
        gain = snapshot.get("output_gain_db")
        if (type(gain) not in (int, float) or not math.isfinite(gain)
                or not -80 <= gain <= 0):
            raise RuntimeError("AutoSub runtime output gain readback is unavailable or invalid")
        return copy.deepcopy(snapshot)

    async def commit_winner(self, proposal: AutoSubProposal) -> dict:
        """Commit only the normalized proposal currently staged; acceptance is external."""
        async with self._lock:
            self._require_stager()
            self._check_revision()
            if self._current is None:
                raise RuntimeError("AutoSub winner requires a successfully staged proposal")
            normalized = sub_candidate_state(
                self._start_state, **self._context, **self._proposal_knobs(proposal))
            # Only the three sub maps and bass can differ from the frozen start.
            # Comparing the normalized whole document also preserves every bank
            # and all untouched processing, without comparing raw numeric forms.
            if normalized != self._current["state"]:
                raise ValueError("AutoSub winner proposal does not match the current staged state")
            await self._ensure_ready_unlocked(self._sample_rate_hz)
            # No await between this final revision check and the synchronous commit.
            self._check_revision()
            committed = self._service.commit(
                copy.deepcopy(self._current["state"]), expected_revision=self._revision)
            self._committed = True
            return committed

    async def commit_staged(self, cancel_requested: Callable[[], bool] | None = None) -> dict:
        """Commit the verified currently staged state; equal-to-start is a no-op.

        Runners call this once, after every acoustic gate, for the final
        retained state. The proposal triplets were already validated when
        staged, so the guard is the runtime: re-readback, frozen revision,
        then the synchronous prepared-transaction commit. When nothing was
        ever staged (no candidate changed anything) the start document is
        re-verified and returned unchanged instead of bumping its revision.

        ``cancel_requested`` is the cooperative cancel probe (the HTTP
        endpoint only flips a flag; the worker task keeps running). It is
        consulted after every await inside the critical section, so a cancel
        observed during the runtime readback vetoes persistence; the owner
        stays uncommitted and cleanup restores the frozen start.
        """

        def _cancelled() -> bool:
            return bool(cancel_requested()) if cancel_requested is not None else False

        async with self._lock:
            self._require_stager()
            self._check_revision()
            if _cancelled():
                raise RuntimeError("AutoSub winner commit skipped: cancellation requested")
            if self._current is None or self._current["state"] == self._start_state:
                # Equal-to-start (or nothing ever staged): verify the runtime
                # still shows the frozen start graph, then return it unchanged
                # without bumping the revision.
                await self._ensure_ready_unlocked(self._sample_rate_hz)
                self._check_revision()
                if _cancelled():
                    raise RuntimeError("AutoSub winner commit skipped: cancellation requested")
                self._committed = True
                return copy.deepcopy(self._service.load())
            await self._ensure_ready_unlocked(self._sample_rate_hz)
            # No await between these final checks and the synchronous commit.
            self._check_revision()
            if _cancelled():
                raise RuntimeError("AutoSub winner commit skipped: cancellation requested")
            committed = self._service.commit(
                copy.deepcopy(self._current["state"]), expected_revision=self._revision)
            self._committed = True
            return committed

    async def restore(self) -> dict | None:
        """Inert is a no-op; activated restores start; committed/stale never restore."""
        async with self._lock:
            self._require_uncommitted()
            if self._stager is None:
                return None
            try:
                return self._record_result(await self._stager.restore())
            finally:
                self._prune_targets()
