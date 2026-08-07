# SPDX-License-Identifier: AGPL-3.0-only

"""Single-owner playback transition coordination.

This module deliberately contains no FXRoute imports.  The application supplies
the runtime adapter, while this class owns transition serialization, the
hardware-output gate contract, commit ordering, and failure latching.  Keeping
the state machine independent makes the safety rules testable without MPV,
PipeWire, EasyEffects, or a live hardware sink.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Protocol
from uuid import uuid4


logger = logging.getLogger(__name__)


class PlaybackTransitionFailure(RuntimeError):
    """A transition failed before its readback contract was committed."""

    def __init__(self, message: str, *, transition_id: str, stage: str) -> None:
        super().__init__(message)
        self.transition_id = transition_id
        self.stage = stage
        self.failure_latched = True

    def as_status(self) -> dict[str, Any]:
        return {
            "ok": False,
            "transition_id": self.transition_id,
            "stage": self.stage,
            "failure_latched": True,
            "message": str(self),
        }


@dataclass
class OutputGateState:
    """Persistent ownership state for the FXRoute hardware-output gate."""

    closed: bool = False
    original_user_muted: bool | None = None
    owner: str | None = None
    transition_id: str | None = None
    failure_latched: bool = False
    closed_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "closed": self.closed,
            "original_user_muted": self.original_user_muted,
            "owner": self.owner,
            "transition_id": self.transition_id,
            "failure_latched": self.failure_latched,
            "closed_at": self.closed_at,
        }


@dataclass(frozen=True)
class TransitionRequest:
    """Immutable input shared by Local, Radio, Spotify and restore paths."""

    operation: str
    source: str
    target_rate: int | None = None
    target_url: str | None = None
    target_track: Mapping[str, Any] = field(default_factory=dict)
    should_play: bool = True
    rate_change: bool = False
    reload_source: bool = True
    graph_only: bool = False
    detail: str = ""
    # A homogeneous local MPV playlist is staged and committed as one
    # transition.  The playlist itself is still owned by MPV after commit;
    # the Coordinator continues to own rate, DSP, graph and output-gate state.
    native_queue: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    native_queue_index: int | None = None
    native_queue_jump: int | None = None
    native_queue_loop: bool = False
    native_queue_shuffle: bool = False


@dataclass(frozen=True)
class TransitionResult:
    transition_id: str
    committed: bool
    source: str
    target_rate: int | None
    state: Mapping[str, Any]


class TransitionRuntime(Protocol):
    """Application-owned operations invoked only by the coordinator."""

    async def read_hardware_mute(self) -> bool: ...

    async def set_hardware_mute(self, muted: bool, transition_id: str) -> None: ...

    async def read_transition_snapshot(self, request: TransitionRequest) -> Mapping[str, Any]: ...

    async def quiet_old_source(self, request: TransitionRequest) -> None: ...

    async def resolve_target_rate(self, request: TransitionRequest) -> int | None: ...

    async def establish_target_rate(self, request: TransitionRequest) -> None: ...

    async def establish_effects_and_helper(
        self, request: TransitionRequest
    ) -> Mapping[str, Any]: ...

    async def prepare_target_source(self, request: TransitionRequest) -> None: ...

    async def start_target_source(self, request: TransitionRequest) -> None: ...

    async def stabilize_effects_after_rate_change(
        self,
        request: TransitionRequest,
        *,
        dsp_reinitialized: bool = False,
    ) -> Mapping[str, Any]: ...

    async def set_source_volume(self, volume: int, transition_id: str) -> None: ...

    async def verify_committed_transition(self, request: TransitionRequest) -> Mapping[str, Any]: ...

    async def verify_transition_graph(self, request: TransitionRequest) -> Mapping[str, Any]: ...

    async def pause_source_after_failure(self, request: TransitionRequest) -> None: ...


class PlaybackTransitionCoordinator:
    """Serialize every playback transition and own the output-gate lifecycle.

    The adapter methods are intentionally coarse-grained.  They prevent a
    caller such as a status endpoint, peak watcher, or delayed repair task from
    directly changing the production graph: those callers can only submit a
    request, while this class controls the mutation order and commit point.
    """

    def __init__(
        self,
        runtime: TransitionRuntime,
        *,
        gate_settle_seconds: float = 0.25,
        gate_state_path: str | Path | None = None,
    ) -> None:
        self.runtime = runtime
        self.gate_settle_seconds = max(0.0, gate_settle_seconds)
        self.gate_state_path = Path(gate_state_path) if gate_state_path else None
        self.lock = asyncio.Lock()
        self.gate = OutputGateState()
        self.last_error: dict[str, Any] | None = None
        self.last_result: TransitionResult | None = None
        self._startup_gate_reconciled = self.gate_state_path is None
        self._startup_gate_error: str | None = None

    @property
    def transition_active(self) -> bool:
        return self.lock.locked()

    def status(self) -> dict[str, Any]:
        return {
            "active": self.transition_active,
            "gate": self.gate.as_dict(),
            "startup_gate_reconciled": self._startup_gate_reconciled,
            "startup_gate_error": self._startup_gate_error,
            "last_error": dict(self.last_error) if self.last_error else None,
            "last_transition_id": self.last_result.transition_id if self.last_result else None,
        }

    def _persist_gate_state(self) -> None:
        """Persist ownership before muting so a restart can resolve stale state."""
        if self.gate_state_path is None:
            return
        path = self.gate_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        payload = {"version": 1, "gate": self.gate.as_dict()}
        try:
            temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _load_gate_state(self) -> dict[str, Any] | None:
        if self.gate_state_path is None:
            return None
        try:
            payload = json.loads(self.gate_state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, TypeError, ValueError):
            self.gate_state_path.unlink(missing_ok=True)
            return None
        state = payload.get("gate") if isinstance(payload, dict) else None
        return state if isinstance(state, dict) else None

    def _clear_gate_state(self) -> None:
        if self.gate_state_path is None:
            return
        try:
            self.gate_state_path.unlink(missing_ok=True)
        except OSError:
            # The in-memory gate is still authoritative for this process; a
            # stale marker is harmless because startup reconciliation restores
            # the same user-mute value before clearing it on the next start.
            pass

    async def _reconcile_startup_gate_locked(self) -> bool:
        if self._startup_gate_reconciled:
            return True
        persisted = self._load_gate_state()
        if not persisted or persisted.get("owner") != "fxroute" or not persisted.get("closed"):
            self._clear_gate_state()
            self._startup_gate_reconciled = True
            return True

        transition_id = str(persisted.get("transition_id") or "startup-gate-reconcile")
        original_user_muted = bool(persisted.get("original_user_muted"))
        self.gate = OutputGateState(
            closed=True,
            original_user_muted=original_user_muted,
            owner="fxroute",
            transition_id=transition_id,
            failure_latched=bool(persisted.get("failure_latched")),
            closed_at=persisted.get("closed_at"),
        )
        try:
            await self.runtime.set_hardware_mute(original_user_muted, transition_id)
            if await self.runtime.read_hardware_mute() != original_user_muted:
                raise RuntimeError("startup output-gate restoration was not confirmed")
        except Exception as exc:
            self._startup_gate_error = str(exc)
            self.gate.failure_latched = True
            self.gate.closed = True
            self.gate.owner = "fxroute"
            try:
                self._persist_gate_state()
                await self.runtime.set_hardware_mute(True, transition_id)
            except Exception:
                pass
            return False

        self.gate = OutputGateState()
        self._clear_gate_state()
        self._startup_gate_error = None
        self._startup_gate_reconciled = True
        return True

    async def reconcile_startup_gate(self) -> bool:
        """Resolve an FXRoute-owned mute left by an earlier process."""
        async with self.lock:
            return await self._reconcile_startup_gate_locked()

    async def _close_gate(self, transition_id: str) -> None:
        observed_muted = await self.runtime.read_hardware_mute()
        # A mute left behind by an earlier FXRoute failure is owned by the
        # coordinator, not evidence of a newly user-muted sink.
        if not self.gate.closed:
            self.gate.original_user_muted = bool(observed_muted)
        elif self.gate.failure_latched and not observed_muted:
            # The user explicitly unmuted after the failure; begin a fresh
            # ownership interval without carrying the old latch forward.
            self.gate.original_user_muted = False
            self.gate.failure_latched = False

        self.gate.closed = True
        self.gate.owner = "fxroute"
        self.gate.transition_id = transition_id
        self.gate.closed_at = time.monotonic()
        self._persist_gate_state()
        await self.runtime.set_hardware_mute(True, transition_id)
        if not await self.runtime.read_hardware_mute():
            raise RuntimeError("hardware output gate could not be confirmed closed")

    async def ensure_output_gate_closed(
        self,
        transition_id: str,
        *,
        stage: str,
    ) -> None:
        """Confirm the physical sink mute while this transition owns the gate.

        The in-memory state is only an ownership record.  Every critical
        boundary reads the actual sink state and repairs a lost mute once.  A
        failed readback is fatal so a transition can never proceed on the
        assumption that an output gate is still closed.
        """
        if (
            not self.gate.closed
            or self.gate.owner != "fxroute"
            or self.gate.transition_id != transition_id
        ):
            raise RuntimeError(
                f"hardware output gate ownership missing at {stage}"
            )
        try:
            muted = bool(await self.runtime.read_hardware_mute())
        except Exception as exc:
            raise RuntimeError(
                f"hardware output gate readback failed at {stage}: {exc}"
            ) from exc
        if muted:
            return

        try:
            await self.runtime.set_hardware_mute(True, transition_id)
            muted = bool(await self.runtime.read_hardware_mute())
        except Exception as exc:
            raise RuntimeError(
                f"hardware output gate re-mute failed at {stage}: {exc}"
            ) from exc
        if not muted:
            raise RuntimeError(
                f"hardware output gate could not be confirmed closed at {stage}"
            )

    async def _hold_gate_after_verification(self) -> None:
        if self.gate.closed and self.gate_settle_seconds:
            await asyncio.sleep(self.gate_settle_seconds)

    async def _restore_gate(self, transition_id: str) -> None:
        if not self.gate.closed:
            return
        if self.gate.transition_id != transition_id or self.gate.owner != "fxroute":
            raise RuntimeError("hardware output gate ownership changed during transition")
        restore_muted = bool(self.gate.original_user_muted)
        await self.runtime.set_hardware_mute(restore_muted, transition_id)
        if (await self.runtime.read_hardware_mute()) != restore_muted:
            raise RuntimeError("hardware output gate restoration was not confirmed")
        self.gate.closed = False
        self.gate.owner = None
        self.gate.transition_id = None
        self.gate.closed_at = None
        self.gate.failure_latched = False
        self.gate.original_user_muted = None
        self._clear_gate_state()

    async def _latch_failure(self, transition_id: str) -> None:
        self.gate.failure_latched = True
        self.gate.closed = True
        self.gate.owner = "fxroute"
        self.gate.transition_id = transition_id
        try:
            self._persist_gate_state()
        except Exception:
            pass
        try:
            await self.runtime.set_hardware_mute(True, transition_id)
        except Exception:
            # The original transition error remains authoritative; the
            # structured status still records the output gate as latched.
            pass

    async def execute(self, request: TransitionRequest) -> TransitionResult:
        """Run one transition and commit only after complete readback."""

        transition_id = f"tr-{uuid4().hex}"
        async with self.lock:
            stage = "snapshot"
            transition_started = time.monotonic()
            stage_started = transition_started
            stage_timings: dict[str, float] = {}

            def enter_stage(name: str) -> None:
                nonlocal stage, stage_started
                now = time.monotonic()
                stage_timings[stage] = stage_timings.get(stage, 0.0) + (
                    now - stage_started
                )
                stage = name
                stage_started = now

            def log_timing(outcome: str) -> None:
                now = time.monotonic()
                stage_timings[stage] = stage_timings.get(stage, 0.0) + (
                    now - stage_started
                )
                details = ",".join(
                    f"{name}={duration * 1000:.1f}ms"
                    for name, duration in stage_timings.items()
                )
                total_ms = (now - transition_started) * 1000
                if outcome == "committed":
                    logger.info(
                        "Playback transition timing: transition_id=%s "
                        "outcome=committed total_ms=%.1f stages=%s",
                        transition_id,
                        total_ms,
                        details,
                    )
                else:
                    logger.warning(
                        "Playback transition timing: transition_id=%s "
                        "outcome=failed stage=%s stage_ms=%.1f total_ms=%.1f "
                        "stages=%s",
                        transition_id,
                        stage,
                        stage_timings.get(stage, 0.0) * 1000,
                        total_ms,
                        details,
                    )

            active_request = request
            effects_state: Mapping[str, Any] = {}
            dsp_state: Mapping[str, Any] = {}
            gate_required = bool(
                request.rate_change
                or request.reload_source
                or request.operation in {
                    "play",
                    "resume",
                    "replay",
                    "queue",
                    "spotify-play",
                    "spotify-toggle",
                    "measurement-restore",
                    "recovery",
                    "graph-reconcile",
                }
            )
            try:
                if not await self._reconcile_startup_gate_locked():
                    raise RuntimeError(
                        f"stale output gate could not be reconciled: {self._startup_gate_error or 'unknown error'}"
                    )
                snapshot = await self.runtime.read_transition_snapshot(request)
                if gate_required:
                    enter_stage("output-gate-close")
                    await self._close_gate(transition_id)

                enter_stage("quiet-old-source")
                await self.runtime.quiet_old_source(request)

                if gate_required and not active_request.graph_only:
                    # Radio streams expose their decoded rate only after a
                    # paused target stream exists.  The adapter may stage
                    # that target under the already-closed gate and return
                    # the authoritative rate; all following stages then use
                    # the resolved immutable request.
                    enter_stage("target-rate-resolve")
                    resolver = getattr(self.runtime, "resolve_target_rate", None)
                    if callable(resolver):
                        resolved_rate = await resolver(active_request)
                        if isinstance(resolved_rate, int) and resolved_rate > 0:
                            active_request = replace(
                                active_request,
                                target_rate=resolved_rate,
                                rate_change=(
                                    active_request.rate_change
                                    or resolved_rate != active_request.target_rate
                                ),
                            )
                    snapshot_active_rate = snapshot.get("active_rate") if isinstance(snapshot, Mapping) else None
                    snapshot_force_rate = snapshot.get("force_rate") if isinstance(snapshot, Mapping) else None
                    if (
                        isinstance(active_request.target_rate, int)
                        and isinstance(snapshot_active_rate, int)
                    ):
                        # A radio's initial configured fallback (usually
                        # 44.1 kHz) may be replaced by its decoded live rate
                        # while the target is staged.  Recompute the actual
                        # transition after that resolution so an already
                        # aligned 48 kHz stream does not rebuild EE/helper.
                        active_request = replace(
                            active_request,
                            rate_change=not (
                                snapshot_active_rate == active_request.target_rate
                                and snapshot_force_rate in {None, 0, active_request.target_rate}
                            ),
                        )
                    enter_stage("target-rate")
                    await self.runtime.establish_target_rate(active_request)
                    await self.ensure_output_gate_closed(
                        transition_id,
                        stage="after-target-rate",
                    )

                enter_stage("effects-helper-links")
                effects_result = await self.runtime.establish_effects_and_helper(
                    active_request
                )
                if isinstance(effects_result, Mapping):
                    effects_state = dict(effects_result)
                if gate_required:
                    await self.ensure_output_gate_closed(
                        transition_id,
                        stage="after-effects-helper-links",
                    )

                enter_stage("target-source-prepare")
                await self.runtime.prepare_target_source(active_request)

                if gate_required:
                    # The target starts muted at both boundaries: hardware is
                    # still gated and MPV source volume is explicitly zero.
                    enter_stage("before-target-source-start-gate")
                    await self.ensure_output_gate_closed(
                        transition_id,
                        stage="before-target-source-start",
                    )
                enter_stage("target-source-start")
                await self.runtime.start_target_source(active_request)

                if gate_required:
                    # The graph must be read back while the output gate is
                    # still closed and the target source is still at volume 0.
                    # Only after this staged commit succeeds may source
                    # volume and the hardware gate be restored.
                    enter_stage("staged-graph-readback")
                    staged_verifier = getattr(self.runtime, "verify_transition_graph", None)
                    if callable(staged_verifier):
                        state = await staged_verifier(active_request)
                    else:
                        state = await self.runtime.verify_committed_transition(active_request)
                    if not bool(state.get("committed", True)):
                        raise RuntimeError("staged transition readback did not satisfy graph contract")

                    if not active_request.graph_only and active_request.should_play:
                        enter_stage("before-source-volume-gate")
                        await self.ensure_output_gate_closed(
                            transition_id,
                            stage="before-source-volume-restore",
                        )
                        enter_stage("source-volume-restore")
                        await self.runtime.set_source_volume(100, transition_id)

                        dsp_required = bool(
                            active_request.rate_change
                            or effects_state.get("dsp_reinitialized")
                        )
                        if dsp_required:
                            enter_stage("effects-dsp-stabilize")
                            stabilizer = getattr(
                                self.runtime, "stabilize_effects_after_rate_change", None
                            )
                            if not callable(stabilizer):
                                raise RuntimeError(
                                    "post-start DSP stabilization is not available"
                                )
                            dsp_state = await stabilizer(
                                active_request,
                                dsp_reinitialized=bool(
                                    effects_state.get("dsp_reinitialized")
                                ),
                            )
                            if not isinstance(dsp_state, Mapping) or not dsp_state.get(
                                "stabilized", False
                            ):
                                raise RuntimeError(
                                    "post-start DSP stabilization was not confirmed"
                                )

                        enter_stage("after-dsp-stabilization-gate")
                        await self.ensure_output_gate_closed(
                            transition_id,
                            stage="after-dsp-stabilization",
                        )
                        enter_stage("commit-readback")
                        state = await self.runtime.verify_committed_transition(active_request)
                else:
                    if active_request.should_play:
                        if gate_required:
                            enter_stage("before-source-volume-gate")
                            await self.ensure_output_gate_closed(
                                transition_id,
                                stage="before-source-volume-restore",
                            )
                        enter_stage("source-volume-restore")
                        await self.runtime.set_source_volume(100, transition_id)

                    enter_stage("commit-readback")
                    state = await self.runtime.verify_committed_transition(active_request)
                if not bool(state.get("committed", True)):
                    raise RuntimeError("transition readback did not satisfy commit contract")

                if gate_required:
                    enter_stage("before-output-gate-restore")
                    await self.ensure_output_gate_closed(
                        transition_id,
                        stage="before-output-gate-restore",
                    )
                    enter_stage("output-gate-restore")
                    await self._hold_gate_after_verification()
                    await self._restore_gate(transition_id)

                result_state = dict(state)
                if effects_state:
                    result_state["effects_graph"] = dict(effects_state)
                if dsp_state:
                    result_state["effects_dsp"] = dict(dsp_state)
                result = TransitionResult(
                    transition_id=transition_id,
                    committed=True,
                    source=active_request.source,
                    target_rate=active_request.target_rate,
                    state=result_state,
                )
                self.last_result = result
                self.last_error = None
                log_timing("committed")
                return result
            except Exception as exc:
                try:
                    await self.runtime.set_source_volume(0, transition_id)
                except Exception:
                    pass
                try:
                    await self.runtime.pause_source_after_failure(active_request)
                except Exception:
                    pass
                if gate_required:
                    await self._latch_failure(transition_id)
                error = PlaybackTransitionFailure(
                    f"Playback transition failed at {stage}: {exc}",
                    transition_id=transition_id,
                    stage=stage,
                )
                self.last_error = error.as_status()
                log_timing("failed")
                raise error from exc

    async def restore_measurement(
        self,
        *,
        source: str,
        target_rate: int,
        target_url: str | None,
        target_track: Mapping[str, Any],
        should_play: bool,
    ) -> TransitionResult:
        """Restore playback after a measurement through the same state machine."""
        return await self.execute(TransitionRequest(
            operation="measurement-restore",
            source=source,
            target_rate=target_rate,
            target_url=target_url,
            target_track=target_track,
            should_play=should_play,
            rate_change=True,
            reload_source=True,
            detail="measurement-release",
        ))


__all__ = [
    "OutputGateState",
    "PlaybackTransitionCoordinator",
    "PlaybackTransitionFailure",
    "TransitionRequest",
    "TransitionResult",
]
