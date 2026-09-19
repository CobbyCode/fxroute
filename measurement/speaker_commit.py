# SPDX-License-Identifier: AGPL-3.0-only
"""Revision-checked Speaker Align commit session and release rebuild.

A session owns one detached frozen start and its revision for its whole
lifetime. It trial-stages the acoustically confirmed speaker candidate on
the runtime, commits it exactly once through the authoritative service, and
retires afterwards: a committed session never restores the old start over
the winner. Equal-to-start is a verified no-op without a revision bump.

The shapes mirror the proven AutoSub session/stager contracts (frozen start,
revision hooks before and after every await, readback-verified guarded
transitions with gain preservation, cancellation-safe recovery, retire after
commit) but operate on full speaker candidate states. The AutoSub
``CandidateStager``/``AutoSubCandidateSession`` stay untouched: their
``sub_candidate_state`` rejects speaker ways, and generalizing a SHIP'd
AutoSub contract is out of scope.

``build_target(plan, *, fingerprint)`` is synchronous and render-only,
returning a ``PlannedSyncTarget``. ``guarded_stage`` has
``DSPRuntime.guarded_rebuild_rendered``'s signature and must honor
``apply_previous`` before rollback, the ``before_*_ramp`` hooks, and both
explicit ramp targets. ``readback()`` is async and returns ``active`` plus
``config.plan_fingerprint``, ``config.sample_rate`` and finite
``output_gain_db`` in [-80, 0]; its adapter should verify physical links as
well as process liveness before reporting active.

The caller must serialize other graph owners and persistence paths across
the session. Revision checks detect drift before and after awaits but cannot
make disk and hardware atomic. On stale revision the session refuses further
staging, restoration and commits and never touches the newer runtime. Create
a new session, not a rebased proposal.
"""

from __future__ import annotations

import asyncio
import copy
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from audio.output_service import OutputService
from audio.output_state import routing_for_device, validate_output_state
from audio.output_state_store import StateConflictError
from audio.output_topology import derive_topology
from dsp.runtime import DSPRuntimeConfig, PlannedSyncTarget
from measurement.release_device import check_release_device
from measurement.speaker_apply import verify_confirmation


class SpeakerAlignRestoreError(RuntimeError):
    """The previous graph could not be restored and independently verified."""


def compile_speaker_candidate(state: dict, *, service: OutputService, output_key: str,
                              channels: int, sample_rate_hz: int) -> tuple[str, dict]:
    """Compile and fingerprint through the authoritative service, without writes."""
    plan = service.compile_plan(state, output_key=output_key, channels=channels,
                                sample_rate_hz=sample_rate_hz)
    return service.fingerprint_plan(plan), plan


def require_speaker_candidate(start_state: dict, candidate_state: dict, *,
                              output_key: str, channels: int) -> dict:
    """Return a detached validated candidate that changes only alignment.

    Every other field — routing, filters, levels, polarities, subs, bass,
    banks, extras, mode — must equal the frozen start, and changed ways must
    be actively routed (dormant stored banks authorize nothing). The revision
    must still be the start revision: no rebase. Bounds come from
    ``validate_output_state``; an equal-to-start candidate is accepted so the
    session can verify it as a no-op.
    """
    start = validate_output_state(start_state)
    candidate = validate_output_state(candidate_state)
    if candidate["revision"] != start["revision"]:
        raise ValueError("Speaker Align candidate is rebased onto another start revision")
    start_processing = start["modes"][start["active_mode"]]["processing"]
    candidate_processing = candidate["modes"][candidate["active_mode"]]["processing"]
    if set(candidate_processing) != set(start_processing):
        raise ValueError("Speaker Align candidate changes more than speaker alignment")
    masked_start = copy.deepcopy(start)
    masked_candidate = copy.deepcopy(candidate)
    for document in (masked_start, masked_candidate):
        for settings in document["modes"][document["active_mode"]]["processing"].values():
            settings["alignment_ms"] = 0.0
    if masked_start != masked_candidate:
        raise ValueError("Speaker Align candidate changes more than speaker alignment")
    changed = [role for role in candidate_processing
               if candidate_processing[role]["alignment_ms"] != start_processing[role]["alignment_ms"]]
    if changed:
        topology = derive_topology(
            candidate["active_mode"],
            routing_for_device(candidate, candidate["active_mode"], output_key),
            channels=channels,
            crossover_enabled=candidate["modes"][candidate["active_mode"]]["crossover_enabled"])
        topology.require_activatable()
        unknown = [role for role in changed if role not in topology.roles]
        if unknown:
            raise ValueError(
                "Speaker Align candidate aligns unrouted roles: " + ", ".join(sorted(unknown)))
    return candidate


@dataclass(frozen=True)
class _Prepared:
    state: dict
    plan: dict
    fingerprint: str
    target: PlannedSyncTarget

    def result(self) -> dict:
        return copy.deepcopy({"state": self.state, "plan": self.plan,
                              "fingerprint": self.fingerprint})


class SpeakerAlignSession:
    """One frozen speaker start, guarded trial staging and a single commit."""

    def __init__(self, *, service: OutputService, start_state: dict,
                 output_key: str, channels: int, sample_rate_hz: int,
                 build_target: Callable[..., PlannedSyncTarget],
                 guarded_stage: Callable[..., Awaitable[None]],
                 readback: Callable[[], Awaitable[dict]], guard_db: float = -30.0):
        if (type(guard_db) not in (int, float) or not math.isfinite(guard_db)
                or not -80 <= guard_db <= 0):
            raise ValueError("Candidate guard must be finite and between -80 and 0 dB")
        if type(sample_rate_hz) is not int or sample_rate_hz <= 0:
            raise ValueError("Speaker Align sample rate must be a positive integer")
        if not all(callable(callback) for callback in (build_target, guarded_stage, readback)):
            raise ValueError("Speaker Align runtime boundaries must be callable")
        self._service = service
        self._start_state = validate_output_state(start_state)
        self._revision = self._start_state["revision"]
        self._context = dict(output_key=output_key, channels=channels,
                             sample_rate_hz=sample_rate_hz)
        self._build_target = build_target
        self._guarded_stage = guarded_stage
        self._readback = readback
        self._guard_db = guard_db
        self._lock = asyncio.Lock()
        self._check_revision()
        self._start_prepared = self._prepare(self._start_state)
        self._staged: _Prepared | None = None
        self._committed = False

    @property
    def committed(self) -> bool:
        return self._committed

    def _check_revision(self) -> None:
        if self._service.load()["revision"] != self._revision:
            raise StateConflictError(
                "Speaker Align start revision is stale; no rebase or rollback allowed")

    def _require_uncommitted(self) -> None:
        if self._committed:
            raise RuntimeError("Speaker Align session is committed; its stager is retired")

    def _prepare(self, state: dict) -> _Prepared:
        fingerprint, plan = compile_speaker_candidate(state, service=self._service, **self._context)
        target = self._build_target(copy.deepcopy(plan), fingerprint=fingerprint)
        if target.config.plan_fingerprint != fingerprint:
            raise ValueError("Rendered speaker target does not carry its compiled fingerprint")
        return _Prepared(state, plan, fingerprint, target)

    async def _matches(self, prepared: _Prepared) -> bool:
        self._check_revision()
        snapshot = await self._readback()
        self._check_revision()
        config = snapshot.get("config") if isinstance(snapshot, dict) else None
        return (isinstance(config, dict) and snapshot.get("active") is True
                and config.get("plan_fingerprint") == prepared.fingerprint
                and config.get("sample_rate") == self._context["sample_rate_hz"])

    async def _verify(self, prepared: _Prepared) -> None:
        if not await self._matches(prepared):
            raise RuntimeError("Speaker Align runtime readback is inactive or has a different fingerprint")

    async def _read_gain(self) -> float:
        self._check_revision()
        snapshot = await self._readback()
        self._check_revision()
        gain = snapshot.get("output_gain_db") if isinstance(snapshot, dict) else None
        if (type(gain) not in (int, float) or not math.isfinite(gain)
                or not -80 <= gain <= 0):
            raise RuntimeError("Speaker Align runtime output gain readback is unavailable or invalid")
        return float(gain)

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
            raise RuntimeError("Speaker Align operating output gain was not restored")

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
            raise SpeakerAlignRestoreError(
                "Speaker Align previous target restoration was not verified") from exc

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

    async def _stage_prepared_unlocked(self, prepared: _Prepared) -> dict:
        self._check_revision()
        previous = self._staged if self._staged is not None else self._start_prepared
        if prepared.fingerprint == previous.fingerprint and await self._matches(prepared):
            self._staged = prepared
            return prepared.result()
        previous_gain = await self._read_gain()
        try:
            await self._transition(prepared, previous, previous_gain)
        except BaseException:
            await self._recover_cancellation_safe(previous, previous_gain)
            raise
        self._staged = prepared
        return prepared.result()

    async def _stage_unlocked(self, candidate_state: dict) -> dict:
        self._require_uncommitted()
        required = require_speaker_candidate(
            self._start_state, candidate_state, output_key=self._context["output_key"],
            channels=self._context["channels"])
        # Compile/render before any runtime work. Render failures need no sync.
        return await self._stage_prepared_unlocked(self._prepare(required))

    async def stage_candidate(self, candidate_state: dict) -> dict:
        """Stage one start-relative speaker proposal; return detached context."""
        async with self._lock:
            return await self._stage_unlocked(candidate_state)

    def measurement_context(self) -> dict:
        """Pin each sweep's preflight to the actually staged candidate plan."""
        self._check_revision()
        prepared = self._staged or self._start_prepared
        return {"expected_native_layout": self._service.compile_layout(prepared.plan),
                "expected_native_output_mode": prepared.plan["mode"],
                "expected_plan_fingerprint": prepared.fingerprint}

    async def _restore_unlocked(self) -> dict:
        self._require_uncommitted()
        return await self._stage_prepared_unlocked(self._start_prepared)

    async def restore_start(self) -> dict:
        """Return to the frozen start rendering, only while it still owns it."""
        async with self._lock:
            return await self._restore_unlocked()

    async def _ensure_ready_unlocked(self, prepared: _Prepared) -> dict:
        self._require_uncommitted()
        self._check_revision()
        snapshot = await self._readback()
        self._check_revision()
        config = snapshot.get("config") if isinstance(snapshot, dict) else None
        if (not isinstance(config, dict) or snapshot.get("active") is not True
                or config.get("plan_fingerprint") != prepared.fingerprint):
            raise RuntimeError("Speaker Align runtime readback is inactive or has a different fingerprint")
        if config.get("sample_rate") != self._context["sample_rate_hz"]:
            raise RuntimeError("Speaker Align runtime sample rate does not match the resolved rate")
        gain = snapshot.get("output_gain_db")
        if (type(gain) not in (int, float) or not math.isfinite(gain)
                or not -80 <= gain <= 0):
            raise RuntimeError("Speaker Align runtime output gain readback is unavailable or invalid")
        return copy.deepcopy(snapshot)

    async def _commit_unlocked(self, candidate_state: dict,
                               cancel_requested: Callable[[], bool] | None = None) -> dict:
        self._require_uncommitted()
        self._check_revision()
        if cancel_requested is not None and cancel_requested():
            raise RuntimeError("Speaker Align winner commit skipped: cancellation requested")
        if self._staged is None:
            raise RuntimeError("Speaker Align winner requires a successfully staged candidate")
        required = require_speaker_candidate(
            self._start_state, candidate_state, output_key=self._context["output_key"],
            channels=self._context["channels"])
        if required == self._start_state:
            # Equal-to-start: verify the runtime still shows the frozen start
            # graph, then return it unchanged without bumping the revision.
            await self._ensure_ready_unlocked(self._start_prepared)
            self._check_revision()
            if cancel_requested is not None and cancel_requested():
                raise RuntimeError("Speaker Align winner commit skipped: cancellation requested")
            self._committed = True
            return copy.deepcopy(self._service.load())
        if self._staged.state != required:
            raise ValueError("Speaker Align winner candidate does not match the current staged state")
        await self._ensure_ready_unlocked(self._staged)
        # No await between these final checks and the synchronous commit.
        self._check_revision()
        if cancel_requested is not None and cancel_requested():
            raise RuntimeError("Speaker Align winner commit skipped: cancellation requested")
        committed = self._service.commit(
            copy.deepcopy(self._staged.state), expected_revision=self._revision)
        self._committed = True
        return committed

    async def commit_candidate(self, candidate_state: dict,
                               cancel_requested: Callable[[], bool] | None = None) -> dict:
        """Commit only the currently staged candidate; acceptance is external.

        The runtime must still show the staged rendering: re-readback, frozen
        revision, then the synchronous prepared-transaction commit. A
        cancel-vetoed commit leaves the session uncommitted with the candidate
        still staged: the caller must ``restore_start()`` on that path.
        """
        async with self._lock:
            return await self._commit_unlocked(candidate_state, cancel_requested)

    async def confirm_and_commit(self, *, acquire: Callable[[], Awaitable[list[dict]]],
                                 alignment: Any, proposal: dict,
                                 live_target: dict,
                                 cancel_requested: Callable[[], bool] | None = None,
                                 acquire_timeout_seconds: float | None = None,
                                 max_residual_ms: float | None = None) -> dict:
        """Trial-stage, acoustically confirm, and commit — or restore.

        This session's lock covers stage, acquisition, verification and
        commit, so two trials on the same session cannot interleave (graph
        ownership across sessions stays the caller's, per the module
        contract). A failed measurement restores the start rendering and
        returns ``confirmed: False`` without committing; errors restore
        (shielded against cancellation) and re-raise. A cancel-vetoed
        commit leaves the session uncommitted with the candidate still
        staged: the caller must ``restore_start()`` on that path. After
        commit the runtime shows the committed rendering and the session
        retires.
        """
        async with self._lock:
            self._require_uncommitted()
            if not callable(acquire):
                raise ValueError("Speaker Align trial requires an acquire boundary")
            if not isinstance(proposal, dict):
                raise ValueError("Speaker Align trial requires a proposal")
            if not isinstance(proposal.get("candidate_state"), dict):
                raise ValueError("Speaker Align trial proposal carries no candidate state")
            if not isinstance(live_target, dict):
                raise ValueError("Speaker Align trial requires a frozen live target")
            live_identity = (
                live_target.get("revision"), live_target.get("processing_fingerprint"))
            proposal_identity = (
                proposal.get("start_revision"), proposal.get("processing_fingerprint"))
            if (not isinstance(live_identity[0], int) or not live_identity[1]
                    or not isinstance(proposal_identity[0], int) or not proposal_identity[1]
                    or live_identity != proposal_identity):
                raise ValueError(
                    "Speaker Align live target is stale; revision and processing must "
                    "match the proposal before trial staging"
                )
            if acquire_timeout_seconds is not None:
                if (type(acquire_timeout_seconds) not in (int, float)
                        or not math.isfinite(acquire_timeout_seconds)
                        or not 0 < acquire_timeout_seconds <= 600):
                    raise ValueError("Speaker Align acquire timeout must be within (0, 600] seconds")
                inner = acquire

                async def acquire():  # noqa: F811 — timeout wrapper replaces the boundary
                    return await asyncio.wait_for(inner(), acquire_timeout_seconds)
            verify_options = {}
            if max_residual_ms is not None:
                verify_options["max_residual_ms"] = max_residual_ms
            await self._stage_unlocked(proposal["candidate_state"])
            try:
                if cancel_requested is not None and cancel_requested():
                    raise asyncio.CancelledError("Speaker Align trial was cancelled")
                captures = await acquire()
                if cancel_requested is not None and cancel_requested():
                    raise asyncio.CancelledError("Speaker Align trial was cancelled")
                confirmation = alignment.propose(captures, live_target=live_target)
                if cancel_requested is not None and cancel_requested():
                    raise asyncio.CancelledError("Speaker Align trial was cancelled")
                check = verify_confirmation(proposal, confirmation, **verify_options)
            except BaseException:
                await asyncio.shield(self._restore_unlocked())
                raise
            if not check["confirmed"]:
                await asyncio.shield(self._restore_unlocked())
                return {"confirmed": False, "check": check, "confirmation": confirmation,
                        "restored": True}
            committed = await self._commit_unlocked(
                proposal["candidate_state"], cancel_requested)
            return {"confirmed": True, "check": check, "confirmation": confirmation,
                    "committed": committed, "restored": False}


def create_speaker_release_adapter(
    *,
    service: Any,
    dsp_manager: Any,
    hardware_ports: list,
    get_native_runtime: Callable[[], Any],
    output_key: str,
    channels: int,
    resolve_live_device: Callable[[], dict] | None = None,
) -> Callable[[int], Awaitable[dict]]:
    """Build the release adapter for one committed speaker device context.

    After a speaker session commits its winner, a session release that
    re-syncs the runtime from a stale overview would rebuild the old graph
    and audibly lose the commit. The adapter renders the *current*
    ``OutputService.load()`` at the restore rate and syncs the prebuilt
    target; the measurement session invokes it instead of the legacy sync.
    Rendering is always live, never rebased: a later concurrent writer is
    consumed as-is. All device context is validated eagerly so a miswired
    finalizer fails before the session unregister, not mid-release.
    ``resolve_live_device`` optionally resolves the live output selection
    at invoke time: when the device moved since the commit — the narrow
    idle-window orphan edge — the adapter refuses fail-closed instead of
    rebuilding the pinned graph over the live selection. Without a
    resolver the pinned context is used as before.
    """
    if not isinstance(output_key, str) or not output_key:
        raise ValueError("Speaker Align release requires a non-empty output key")
    if type(channels) is not int or channels <= 0:
        raise ValueError("Speaker Align release requires a positive integer channel count")
    ports = list(hardware_ports or [])
    if len(ports) < channels:
        raise ValueError("Speaker Align release has insufficient discovered playback ports")
    if not callable(getattr(dsp_manager, "compile_engine_text", None)):
        raise ValueError("Speaker Align release requires a DSP manager")
    if not callable(get_native_runtime):
        raise ValueError("Speaker Align release requires a native runtime accessor")
    if resolve_live_device is not None and not callable(resolve_live_device):
        raise ValueError("Speaker Align release requires a callable live device resolver")

    async def adapter(restore_rate_hz: int) -> dict:
        if type(restore_rate_hz) is not int or restore_rate_hz <= 0:
            raise ValueError("Speaker Align release rate must be a positive integer")
        if resolve_live_device is not None:
            check_release_device(
                output_key=output_key, channels=channels,
                hardware_ports=ports, live=resolve_live_device(),
                owner="Speaker Align")
        state = service.load()
        plan = service.compile_plan(
            state, output_key=output_key, channels=channels,
            sample_rate_hz=restore_rate_hz)
        fingerprint = service.fingerprint_plan(plan)
        layout = service.compile_layout(plan)
        config = DSPRuntimeConfig.from_plan(
            plan, layout=layout, output_key=output_key,
            sample_rate_hz=restore_rate_hz, hardware_ports=list(ports),
            plan_fingerprint=fingerprint)
        text = dsp_manager.compile_engine_text(
            [dict(entry) for entry in layout],
            preset_name=plan["global"]["preset"],
            sample_rate_hz=restore_rate_hz,
            extras_override=plan["global"]["extras"])
        native_runtime = get_native_runtime()
        if native_runtime is None:
            raise RuntimeError("Native DSP runtime is unavailable for release rebuild")
        await native_runtime.sync_rendered(PlannedSyncTarget(config, text))
        return {"revision": state["revision"], "plan_fingerprint": fingerprint,
                "sample_rate_hz": restore_rate_hz}

    return adapter
