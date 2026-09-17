# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded, in-memory AutoSub proposals; not a runner or winner-commit path.

A stager owns a detached start document and its original revision for its whole
lifetime. Loading the service later only checks that revision; it never rebases
onto a newer winner. No service mutation or persistence method is called here.

Integration is deliberately absent until peak safety/prearm and every AutoSub
persistence path are adapted. Runtime identity is not acoustic winner evidence:
NO winner commit is implemented. Callers must restore on session exit.
"""

from __future__ import annotations

import asyncio
import copy
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from audio.output_service import OutputService
from audio.output_state import validate_output_state
from audio.output_state_store import StateConflictError
from audio.output_topology import SUB_ROLES
from dsp.runtime import PlannedSyncTarget
from measurement.autosub.roles import autosub_topology_from_state, require_autosub_topology


class CandidateRestoreError(RuntimeError):
    """The previous graph could not be restored and independently verified."""


def sub_candidate_state(state: dict, *, output_key: str, channels: int,
                        sub_delays: dict | None = None,
                        sub_levels: dict | None = None,
                        sub_polarities: dict | None = None,
                        bass: dict | None = None) -> dict:
    """Copy a complete valid state, changing only active sub trims and bass.

    Device and channel context is mandatory: dormant stored banks and ports
    beyond the available hardware tier do not authorize candidate changes.
    Fan-out is one logical role, not a second optimizable subwoofer. Alignment
    values are absolute role offsets; speaker-way processing and all banks are
    retained exactly (the compiler supplies any common nonnegative delay).
    """
    result = validate_output_state(state)
    topology = require_autosub_topology(autosub_topology_from_state(
        result, output_key=output_key, channels=channels))
    config = result["modes"][result["active_mode"]]
    for proposals, field in ((sub_delays, "alignment_ms"), (sub_levels, "level_db"),
                             (sub_polarities, "polarity")):
        if proposals is None:
            continue
        if not isinstance(proposals, dict):
            raise ValueError("Sub candidates must be objects keyed by routed sub role")
        for role, value in proposals.items():
            if role not in SUB_ROLES or role not in topology.sub_roles:
                raise ValueError(f"AutoSub candidate role {role!r} is not an active routed sub")
            config["processing"][role][field] = value
    if bass is not None:
        if not isinstance(bass, dict) or set(bass) - {"frequency_hz", "main_highpass_enabled"}:
            raise ValueError("Candidate bass supports only frequency_hz and main_highpass_enabled")
        config["bass_management"].update(bass)
    return validate_output_state(result)


def compile_candidate(state: dict, *, service: OutputService, output_key: str,
                      channels: int, sample_rate_hz: int) -> tuple[str, dict]:
    """Compile and fingerprint through the authoritative service, without writes."""
    plan = service.compile_plan(state, output_key=output_key, channels=channels,
                                sample_rate_hz=sample_rate_hz)
    return service.fingerprint_plan(plan), plan


@dataclass(frozen=True)
class _Prepared:
    state: dict
    plan: dict
    fingerprint: str
    target: PlannedSyncTarget

    def result(self) -> dict:
        return copy.deepcopy({"state": self.state, "plan": self.plan,
                              "fingerprint": self.fingerprint})


class CandidateStager:
    """Stage proposals under an injected guarded runtime boundary.

    ``build_target(plan, *, fingerprint)`` is synchronous and must be render-only
    (no graph changes or candidate persistence), returning a PlannedSyncTarget.
    ``readback()`` is async and returns the runtime snapshot shape: ``active``
    plus ``config.plan_fingerprint`` and finite ``output_gain_db`` in [-80, 0].
    Its adapter should verify physical links as well as process liveness before
    reporting active. Staging and recovery preserve the gain captured before
    the transition, not the possibly attenuated gain observed during recovery.

    ``guarded_stage`` has DSPRuntime.guarded_rebuild_rendered's signature. It
    MUST honor apply_previous before rollback, and the before_*_ramp hooks
    before releasing the guard, and honor both explicit ramp targets. These
    hooks check revision and runtime identity;
    apply_candidate/apply_previous check revision ONLY, never persist anything.
    Do not replace this boundary with an unguarded sync_rendered call.

    The caller must serialize other graph owners/persistence paths across this
    session. Revision checks detect drift before/after awaits but cannot make
    disk and hardware atomic or undo an uncooperative callback's writes. On
    stale revision this object refuses further staging/restoration and never
    touches the newer runtime. Create a new session, not a rebased proposal.
    """

    def __init__(self, *, service: OutputService, start_state: dict,
                 output_key: str, channels: int, sample_rate_hz: int,
                 build_target: Callable[..., PlannedSyncTarget],
                 guarded_stage: Callable[..., Awaitable[None]],
                 readback: Callable[[], Awaitable[dict]], guard_db: float):
        if (type(guard_db) not in (int, float) or not math.isfinite(guard_db)
                or not -80 <= guard_db <= 0):
            raise ValueError("Candidate guard must be finite and between -80 and 0 dB")
        self._service = service
        self._start_state = sub_candidate_state(
            start_state, output_key=output_key, channels=channels)
        self._revision = self._start_state["revision"]
        self._context = dict(output_key=output_key, channels=channels,
                             sample_rate_hz=sample_rate_hz)
        self._build_target = build_target
        self._guarded_stage = guarded_stage
        self._readback = readback
        self._guard_db = guard_db
        self._lock = asyncio.Lock()
        self._check_revision()
        self._initial = self._prepare(self._start_state)
        self._current = self._initial

    def _check_revision(self) -> None:
        if self._service.load()["revision"] != self._revision:
            raise StateConflictError("AutoSub start revision is stale; no rebase or rollback allowed")

    def _prepare(self, state: dict) -> _Prepared:
        fingerprint, plan = compile_candidate(state, service=self._service, **self._context)
        target = self._build_target(copy.deepcopy(plan), fingerprint=fingerprint)
        if target.config.plan_fingerprint != fingerprint:
            raise ValueError("Rendered candidate target does not carry its compiled fingerprint")
        return _Prepared(state, plan, fingerprint, target)

    async def _matches(self, prepared: _Prepared) -> bool:
        self._check_revision()
        snapshot = await self._readback()
        self._check_revision()
        config = snapshot.get("config") if isinstance(snapshot, dict) else None
        return (isinstance(config, dict) and snapshot.get("active") is True
                and config.get("plan_fingerprint") == prepared.fingerprint)

    async def _verify(self, prepared: _Prepared) -> None:
        if not await self._matches(prepared):
            raise RuntimeError("AutoSub runtime readback is inactive or has a different fingerprint")

    async def _transition(self, new: _Prepared, previous: _Prepared,
                          operating_gain: float) -> None:
        self._check_revision()
        await self._guarded_stage(
            new.target, previous=previous.target,
            guard_db=min(self._guard_db, operating_gain),
            ramp_target_db=operating_gain, rollback_ramp_target_db=operating_gain,
            apply_candidate=self._check_revision, apply_previous=self._check_revision,
            before_ramp=lambda: self._verify(new),
            before_rollback_ramp=lambda: self._verify(previous))
        self._check_revision()
        await self._verify(new)
        if await self._read_gain() != operating_gain:
            raise RuntimeError("AutoSub operating output gain was not restored")

    async def _read_gain(self) -> float:
        self._check_revision()
        snapshot = await self._readback()
        self._check_revision()
        gain = snapshot.get("output_gain_db") if isinstance(snapshot, dict) else None
        if (type(gain) not in (int, float) or not math.isfinite(gain)
                or not -80 <= gain <= 0):
            raise RuntimeError("AutoSub runtime output gain readback is unavailable or invalid")
        return float(gain)

    async def _recover(self, previous: _Prepared, previous_gain: float) -> None:
        """Verify both rollback identity and the original pretransition gain."""
        self._check_revision()
        try:
            try:
                restored = (await self._matches(previous)
                            and await self._read_gain() == previous_gain)
            except StateConflictError:
                raise
            except Exception:
                # Unavailable readback is not evidence of successful rollback.
                # Attempt restoration, then require a fresh successful readback.
                restored = False
            if restored:
                return
            # Both targets are previous: a failed recovery must never roll back
            # to the unverified candidate. The same revision hooks still apply.
            await self._transition(previous, previous, previous_gain)
        except StateConflictError:
            raise
        except Exception as exc:
            raise CandidateRestoreError("AutoSub previous target restoration was not verified") from exc

    async def _recover_cancellation_safe(self, previous: _Prepared,
                                         previous_gain: float) -> None:
        recovery = asyncio.create_task(self._recover(previous, previous_gain))
        # Do not leave rollback running after returning cancellation to a caller
        # that may release measurement ownership. Repeated cancels cannot abort it.
        while not recovery.done():
            try:
                await asyncio.shield(recovery)
            except asyncio.CancelledError:
                continue
        recovery.result()

    async def _stage_prepared(self, prepared: _Prepared) -> dict:
        self._check_revision()
        previous = self._current
        if prepared.fingerprint == previous.fingerprint and await self._matches(prepared):
            return prepared.result()
        previous_gain = await self._read_gain()
        try:
            await self._transition(prepared, previous, previous_gain)
        except BaseException:
            await self._recover_cancellation_safe(previous, previous_gain)
            raise
        self._current = prepared
        return prepared.result()

    async def stage(self, *, sub_delays: dict | None = None,
                    sub_levels: dict | None = None, sub_polarities: dict | None = None,
                    bass: dict | None = None) -> dict:
        """Stage one start-relative proposal; return detached state/plan/fingerprint."""
        async with self._lock:
            self._check_revision()
            state = sub_candidate_state(
                self._start_state, output_key=self._context["output_key"],
                channels=self._context["channels"], sub_delays=sub_delays,
                sub_levels=sub_levels, sub_polarities=sub_polarities, bass=bass)
            # Compile/render before any runtime work. Render failures need no sync.
            return await self._stage_prepared(self._prepare(state))

    async def restore(self) -> dict:
        """Return to the frozen start target, only while its revision still owns it."""
        async with self._lock:
            return await self._stage_prepared(self._initial)
