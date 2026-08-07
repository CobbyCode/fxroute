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
    # Output-mode changes are staged as one Coordinator transaction.  The
    # target overview/config are deliberately carried as immutable request
    # data so runtime mutations cannot escape the gate-owned state machine.
    output_mode_target: Mapping[str, Any] = field(default_factory=dict)
    output_mode_config: Mapping[str, Any] = field(default_factory=dict)
    # Measurement restore is still a normal Coordinator transition, but its
    # caller may carry a position and an intent token captured before the
    # measurement window.  The runtime validates that token immediately
    # before any old source can be resurrected.
    restore_position: float | None = None
    restore_intent: Mapping[str, Any] = field(default_factory=dict)
    # Watcher-triggered recovery is valid only for the committed context that
    # produced the observation.  The application revalidates these fields
    # immediately before handing the request to the Coordinator.
    recovery_commit_context_id: str | None = None
    recovery_source: str | None = None
    recovery_url: str | None = None


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

    async def validate_measurement_restore_intent(
        self,
        request: TransitionRequest,
        snapshot: Mapping[str, Any],
    ) -> bool: ...

    async def quiet_old_source(self, request: TransitionRequest) -> None: ...

    async def resolve_target_rate(self, request: TransitionRequest) -> int | None: ...

    async def establish_target_rate(self, request: TransitionRequest) -> None: ...

    async def establish_effects_and_helper(
        self, request: TransitionRequest
    ) -> Mapping[str, Any]: ...

    async def prepare_target_source(self, request: TransitionRequest) -> None: ...

    async def start_target_source(self, request: TransitionRequest) -> None: ...

    async def reconcile_post_start_graph(
        self, request: TransitionRequest
    ) -> Mapping[str, Any]: ...

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

    def target_source_staged(self, request: TransitionRequest) -> bool: ...

    async def abort_failed_transition(
        self,
        request: TransitionRequest,
        snapshot: Mapping[str, Any] | None,
        *,
        target_staged: bool,
    ) -> None: ...

    async def verify_measurement_entry(
        self, request: TransitionRequest
    ) -> Mapping[str, Any]: ...

    async def verify_output_mode_runtime(
        self, request: TransitionRequest
    ) -> Mapping[str, Any]: ...

    async def commit_output_mode_runtime(
        self, request: TransitionRequest
    ) -> Mapping[str, Any]: ...

    async def rollback_output_mode_runtime(
        self, request: TransitionRequest, snapshot: Mapping[str, Any] | None
    ) -> None: ...

    async def restore_output_mode_transport(
        self,
        request: TransitionRequest,
        snapshot: Mapping[str, Any] | None,
        transition_id: str,
    ) -> None: ...


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

    async def _cancel_cleanup(
        self,
        request: TransitionRequest,
        transition_id: str,
        *,
        gate_required: bool,
    ) -> None:
        """Best-effort safety cleanup executed outside the cancelled task."""
        # Latch first: the synchronous gate state and persistence happen
        # before the hardware write, so even a second cancellation leaves the
        # Coordinator in an explicitly unsafe/closed state.
        if gate_required:
            try:
                await self._latch_failure(transition_id)
            except BaseException as exc:
                logger.warning(
                    "Playback transition cancellation could not fully latch the output gate: %s",
                    exc,
                )
        try:
            await self.runtime.set_source_volume(0, transition_id)
        except BaseException as exc:
            logger.warning(
                "Playback transition cancellation could not attenuate the source: %s",
                exc,
            )
        try:
            await self.runtime.pause_source_after_failure(request)
        except BaseException as exc:
            logger.warning(
                "Playback transition cancellation could not pause the source: %s",
                exc,
            )

    async def _finish_cancel_cleanup(
        self,
        request: TransitionRequest,
        transition_id: str,
        *,
        gate_required: bool,
    ) -> None:
        """Drain cancellation cleanup before releasing the transition lock."""
        cleanup_task = asyncio.create_task(
            self._cancel_cleanup(
                request,
                transition_id,
                gate_required=gate_required,
            ),
            name="playback-transition-cancel-cleanup",
        )
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                # A second cancellation must not interrupt the safety drain.
                # The original cancellation is re-raised by execute() after
                # this method returns.
                continue
        try:
            await cleanup_task
        except BaseException as exc:
            logger.warning("Playback transition cancellation cleanup failed: %s", exc)

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
            post_start_graph_state: Mapping[str, Any] = {}
            dsp_state: Mapping[str, Any] = {}
            snapshot: Mapping[str, Any] = {}
            target_prepare_started = False
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
                    "measurement-entry",
                    "output-mode-switch",
                    "recovery",
                    "graph-reconcile",
                }
            )

            async def skip_measurement_restore(reason: str) -> TransitionResult:
                """Discard a stale measurement snapshot without mutating playback."""
                if gate_required and self.gate.closed:
                    await self._restore_gate(transition_id)
                result = TransitionResult(
                    transition_id=transition_id,
                    committed=False,
                    source=active_request.source,
                    target_rate=active_request.target_rate,
                    state={
                        "committed": False,
                        "skipped": True,
                        "reason": reason,
                    },
                )
                self.last_result = result
                self.last_error = None
                logger.info(
                    "Measurement playback restore skipped before source load: "
                    "transition_id=%s reason=%s",
                    transition_id,
                    reason,
                )
                return result

            try:
                if not await self._reconcile_startup_gate_locked():
                    raise RuntimeError(
                        f"stale output gate could not be reconciled: {self._startup_gate_error or 'unknown error'}"
                    )
                snapshot = await self.runtime.read_transition_snapshot(request)
                restore_validator = getattr(
                    self.runtime, "validate_measurement_restore_intent", None
                )
                if (
                    active_request.operation == "measurement-restore"
                    and active_request.restore_intent
                    and callable(restore_validator)
                    and not await restore_validator(active_request, snapshot)
                ):
                    return await skip_measurement_restore("intent-changed-before-gate")
                if gate_required:
                    enter_stage("output-gate-close")
                    await self._close_gate(transition_id)

                enter_stage("quiet-old-source")
                await self.runtime.quiet_old_source(request)

                if (
                    active_request.operation == "measurement-restore"
                    and active_request.restore_intent
                    and callable(restore_validator)
                    and not await restore_validator(active_request, snapshot)
                ):
                    return await skip_measurement_restore("intent-changed-after-quiet")

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

                if active_request.operation in {"measurement-entry", "output-mode-switch"}:
                    if active_request.operation == "measurement-entry":
                        enter_stage("measurement-entry-graph-readback")
                        verifier = getattr(self.runtime, "verify_measurement_entry", None)
                        if callable(verifier):
                            state = await verifier(active_request)
                        else:
                            state = await self.runtime.verify_transition_graph(active_request)
                        if not bool(state.get("committed", True)):
                            raise RuntimeError(
                                "measurement entry readback did not satisfy graph contract"
                            )
                    else:
                        # The target graph is not committed until the old
                        # transport has been put back under the still-closed
                        # gate.  Starting Spotify here is intentional: its
                        # newly-created sink ports are part of the same final
                        # source-graph commit.
                        enter_stage("output-mode-transport-restore")
                        restorer = getattr(self.runtime, "restore_output_mode_transport", None)
                        if not callable(restorer):
                            raise RuntimeError(
                                "Coordinator output-mode transport restore is unavailable"
                            )
                        await restorer(active_request, snapshot, transition_id)
                        await self.ensure_output_gate_closed(
                            transition_id,
                            stage="after-output-mode-transport-restore",
                        )

                        enter_stage("post-start-graph-reconcile")
                        post_start_reconciler = getattr(
                            self.runtime, "reconcile_post_start_graph", None
                        )
                        if not callable(post_start_reconciler):
                            raise RuntimeError(
                                "Coordinator output-mode graph reconciliation is unavailable"
                            )
                        post_start_state = await post_start_reconciler(active_request)
                        if (
                            not isinstance(post_start_state, Mapping)
                            or not post_start_state.get("graph_complete", False)
                        ):
                            raise RuntimeError(
                                "output-mode post-start graph reconciliation did not confirm a complete graph"
                            )
                        post_start_graph_state = dict(post_start_state)

                        enter_stage("output-mode-graph-readback")
                        verifier = getattr(self.runtime, "verify_output_mode_runtime", None)
                        if not callable(verifier):
                            raise RuntimeError(
                                "Coordinator output-mode runtime verifier is unavailable"
                            )
                        state = await verifier(active_request)
                        if not bool(state.get("committed", True)):
                            raise RuntimeError(
                                "output-mode graph readback did not satisfy commit contract"
                            )

                    if effects_state.get("dsp_reinitialized"):
                        enter_stage("effects-dsp-stabilize")
                        stabilizer = getattr(
                            self.runtime, "stabilize_effects_after_rate_change", None
                        )
                        if not callable(stabilizer):
                            raise RuntimeError(
                                "post-transition DSP stabilization is not available"
                            )
                        dsp_state = await stabilizer(
                            active_request,
                            dsp_reinitialized=True,
                        )
                        if not isinstance(dsp_state, Mapping) or not dsp_state.get(
                            "stabilized", False
                        ):
                            raise RuntimeError(
                                "post-transition DSP stabilization was not confirmed"
                            )

                    if active_request.operation == "output-mode-switch":
                        enter_stage("output-mode-persist")
                        committer = getattr(self.runtime, "commit_output_mode_runtime", None)
                        if not callable(committer):
                            raise RuntimeError(
                                "Coordinator output-mode persistence is unavailable"
                            )
                        committed_mode = await committer(active_request)
                        if isinstance(committed_mode, Mapping):
                            state = {**dict(state), **dict(committed_mode)}

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

                enter_stage("target-source-prepare")
                target_prepare_started = True
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
                    # Source creation can recreate PipeWire ports and lose a
                    # production edge after the earlier effects/helper stage.
                    # Reconcile that bounded link-only drift while the gate is
                    # still closed, before the existing staged commit readback.
                    enter_stage("post-start-graph-reconcile")
                    post_start_reconciler = getattr(
                        self.runtime, "reconcile_post_start_graph", None
                    )
                    if callable(post_start_reconciler):
                        post_start_state = await post_start_reconciler(active_request)
                        if (
                            not isinstance(post_start_state, Mapping)
                            or not post_start_state.get("graph_complete", False)
                        ):
                            raise RuntimeError(
                                "post-start graph reconciliation did not confirm a complete graph"
                            )
                        post_start_graph_state = dict(post_start_state)

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
                if post_start_graph_state:
                    result_state["post_start_graph"] = dict(post_start_graph_state)
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
            except asyncio.CancelledError:
                await self._finish_cancel_cleanup(
                    active_request,
                    transition_id,
                    gate_required=gate_required,
                )
                self.last_error = {
                    "ok": False,
                    "transition_id": transition_id,
                    "stage": stage,
                    "failure_latched": bool(gate_required),
                    "cancelled": True,
                    "message": f"Playback transition cancelled at {stage}",
                }
                log_timing("cancelled")
                raise
            except Exception as exc:
                try:
                    await self.runtime.set_source_volume(0, transition_id)
                except Exception:
                    pass
                try:
                    await self.runtime.pause_source_after_failure(active_request)
                except Exception:
                    pass
                if active_request.operation == "output-mode-switch":
                    rollback = getattr(self.runtime, "rollback_output_mode_runtime", None)
                    if callable(rollback):
                        try:
                            await rollback(active_request, snapshot)
                        except Exception:
                            logger.warning(
                                "Output-mode runtime rollback failed; keeping the failure gate latched",
                                exc_info=True,
                            )
                target_staged = False
                staged_detector = getattr(self.runtime, "target_source_staged", None)
                if callable(staged_detector):
                    try:
                        target_staged = bool(staged_detector(active_request))
                    except Exception:
                        target_staged = False
                elif (
                    target_prepare_started
                    and active_request.source in {"local", "radio"}
                    and active_request.reload_source
                ):
                    # A minimal test/runtime adapter may not expose the
                    # concrete staging marker. Once its mutating prepare stage
                    # started, prefer invalidation over old/new metadata mix.
                    target_staged = True
                aborter = getattr(self.runtime, "abort_failed_transition", None)
                if callable(aborter):
                    try:
                        await aborter(
                            active_request,
                            snapshot,
                            target_staged=target_staged,
                        )
                    except Exception:
                        logger.warning(
                            "Playback transition abort cleanup failed",
                            exc_info=True,
                        )
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
        restore_position: float | None = None,
        restore_intent: Mapping[str, Any] | None = None,
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
            restore_position=restore_position,
            restore_intent=dict(restore_intent or {}),
        ))


__all__ = [
    "OutputGateState",
    "PlaybackTransitionCoordinator",
    "PlaybackTransitionFailure",
    "TransitionRequest",
    "TransitionResult",
]
