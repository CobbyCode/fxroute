# SPDX-License-Identifier: AGPL-3.0-only

"""Playback transition coordinator: serialization, staging, commit order."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping
from uuid import uuid4

from .cleanup import _TransitionCleanupMixin
from .gate import _OutputGateMixin
from audio.samplerate.constants import FXROUTE_MAX_PROCESSING_RATE
import playback.source_policy as source_policy

from .models import (
    OutputGateState,
    PlaybackTransitionFailure,
    RecoveryExecutor,
    RecoveryValidator,
    TransitionRequest,
    TransitionResult,
    UnsupportedTransitionRateError,
)
from .protocol import TransitionRuntime
from .readbacks import stable_graph_readbacks
from .stages import _TransitionStages

logger = logging.getLogger(__name__)


class PlaybackTransitionCoordinator(_TransitionCleanupMixin, _OutputGateMixin):
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
        self._last_successful_commit_id: str | None = None
        self._recovery_lock = asyncio.Lock()
        self._recovery_inflight_key: tuple[str, str] | None = None
        self._recovery_last_key: tuple[str, str] | None = None
        self._startup_gate_reconciled = self.gate_state_path is None
        self._startup_gate_error: str | None = None

    @property
    def transition_active(self) -> bool:
        return self.lock.locked()

    def status(self) -> dict[str, Any]:
        return {
            "active": self.transition_active,
            "transition_blocked": self.transition_blocked,
            "gate": self.gate.as_dict(),
            "startup_gate_reconciled": self._startup_gate_reconciled,
            "startup_gate_error": self._startup_gate_error,
            "last_error": dict(self.last_error) if self.last_error else None,
            "last_transition_id": self.last_result.transition_id if self.last_result else None,
        }

    @property
    def transition_blocked(self) -> bool:
        """Return whether the Coordinator blocks normal playback admission."""
        return bool(
            self.transition_active
            or self.gate.failure_latched
            or self.gate.closed
        )

    @property
    def last_successful_commit_id(self) -> str | None:
        """Return the latest committed transition identity."""
        return self._last_successful_commit_id

    def recovery_context_is_current(self, commit_context_id: str | None) -> bool:
        """Return whether a watcher still targets the latest committed context."""
        return bool(
            commit_context_id
            and self.last_successful_commit_id == commit_context_id
            and not self.transition_active
            and not self.gate.failure_latched
        )

    async def run_recovery(
        self,
        *,
        signature: str,
        commit_context_id: str,
        validate: RecoveryValidator,
        execute: RecoveryExecutor,
    ) -> Any:
        """Coalesce one watcher recovery within its committed context.

        Live transport validation remains in the application callback.  This
        method owns only serialization, context-specific deduplication, and
        the attempt lifecycle around the actual Coordinator request.
        """
        dedupe_key = (str(signature), str(commit_context_id))
        async with self._recovery_lock:
            if (
                dedupe_key == self._recovery_inflight_key
                or dedupe_key == self._recovery_last_key
            ):
                logger.info(
                    "Coordinator recovery deduplicated: signature=%s commit_context=%s",
                    signature,
                    commit_context_id,
                )
                return None
            if self.transition_active:
                logger.info(
                    "Coordinator recovery deferred while another transition is active: signature=%s",
                    signature,
                )
                return None
            if not await validate():
                return None
            self._recovery_inflight_key = dedupe_key
            try:
                result = await execute()
            finally:
                self._recovery_inflight_key = None
            # Only a committed recovery is remembered for deduplication.  A
            # failed or uncommitted attempt must not permanently suppress
            # legitimate retries of the same signature/context.
            if result is not None and getattr(result, "committed", False):
                self._recovery_last_key = dedupe_key
            return result

    def _record_result(self, result: TransitionResult) -> None:
        self.last_result = result
        if result.committed:
            self._last_successful_commit_id = result.transition_id

    @staticmethod
    def _request_expects_audible_output(request: TransitionRequest) -> bool:
        """Return whether a committed request must leave the output audible."""
        return bool(request.should_play or request.operation == "measurement-entry")

    @staticmethod
    def _validate_transition_target_rate(request: TransitionRequest) -> None:
        """Reject a target rate the selected output or FXRoute cannot carry.

        Runs before any transition state is mutated so the rejection can
        propagate to the API caller as a clean HTTP 400.  Fails open when the
        capability list is unavailable so a discovery hiccup never blocks
        playback.  Rate-neutral operations (measurement, output-mode switch,
        graph repair) carry the current committed rate by contract and are
        not rate-targeted, so they are exempt.
        """
        if request.operation in {
            "measurement-entry",
            "measurement-restore",
            "output-mode-switch",
            "graph-reconcile",
        }:
            return
        target_rate = request.target_rate
        if not isinstance(target_rate, int) or target_rate <= 0:
            return
        if target_rate > FXROUTE_MAX_PROCESSING_RATE:
            raise UnsupportedTransitionRateError(
                f"FXRoute supports sample rates up to {FXROUTE_MAX_PROCESSING_RATE} Hz; "
                f"cannot switch to {target_rate} Hz"
            )
        overview = request.audio_overview or {}
        selected = overview.get("selected_output") or overview.get("current_output") or {}
        supported = [
            rate
            for rate in (selected.get("supported_rates") or [])
            if isinstance(rate, int) and rate > 0
        ]
        if not supported:
            # Capability unknown (no selected output or enumeration failed):
            # do not block on a list we cannot trust.
            return
        if target_rate not in supported:
            maximum = max(supported)
            raise UnsupportedTransitionRateError(
                f"Selected output does not support sample rate {target_rate} Hz "
                f"(maximum {maximum} Hz)"
            )

    async def _stage(
        self,
        stages: _TransitionStages,
        name: str,
        action: Callable[[], Awaitable[Any]],
        *,
        gate_check: str | None = None,
    ) -> Any:
        """Run one transition stage, record its timing and optionally confirm
        the output gate afterwards (``gate_check`` names the confirmation
        boundary)."""
        stages.enter(name)
        result = await action()
        if gate_check is not None:
            await self.ensure_output_gate_closed(stages.transition_id, stage=gate_check)
        return result

    async def _skip_claim_noop(
        self,
        stages: _TransitionStages,
        request: TransitionRequest,
    ) -> TransitionResult:
        """Discard a stale external-renderer claim without mutating playback.

        The claim's pre-lock owner guard can race an FXRoute-initiated start
        of the same source; re-validation inside the lock turns the queued
        claim into a no-op so it never closes the output gate over live audio.
        """
        result = TransitionResult(
            transition_id=stages.transition_id,
            committed=False,
            source=request.source,
            target_rate=request.target_rate,
            state={
                "committed": False,
                "skipped": True,
                "reason": "claim-owner-already-committed",
            },
        )
        self._record_result(result)
        self.last_error = None
        logger.info(
            "Playback transition claim skipped as no-op: source=%s operation=%s transition_id=%s",
            request.source,
            request.operation,
            stages.transition_id,
        )
        return result

    async def _skip_measurement_restore(
        self,
        stages: _TransitionStages,
        request: TransitionRequest,
        reason: str,
    ) -> TransitionResult:
        """Discard a stale measurement snapshot without mutating playback."""
        if stages.gate_required and self.gate.closed:
            await self._restore_gate(stages.transition_id)
        result = TransitionResult(
            transition_id=stages.transition_id,
            committed=False,
            source=request.source,
            target_rate=request.target_rate,
            state={
                "committed": False,
                "skipped": True,
                "reason": reason,
            },
        )
        self._record_result(result)
        self.last_error = None
        logger.info(
            "Measurement playback restore skipped before source load: "
            "transition_id=%s reason=%s",
            stages.transition_id,
            reason,
        )
        return result

    def _finish_committed(
        self,
        stages: _TransitionStages,
        request: TransitionRequest,
        state: Mapping[str, Any],
        *,
        effects_state: Mapping[str, Any] | None = None,
        post_start_graph_state: Mapping[str, Any] | None = None,
        dsp_state: Mapping[str, Any] | None = None,
    ) -> TransitionResult:
        """Record and return the committed result of one transition run."""
        result_state = dict(state)
        if effects_state:
            result_state["effects_graph"] = dict(effects_state)
        if post_start_graph_state:
            result_state["post_start_graph"] = dict(post_start_graph_state)
        if dsp_state:
            result_state["effects_dsp"] = dict(dsp_state)
        result = TransitionResult(
            transition_id=stages.transition_id,
            committed=True,
            source=request.source,
            target_rate=request.target_rate,
            state=result_state,
        )
        self._record_result(result)
        self.last_error = None
        stages.log("committed")
        return result

    async def _evaluate_same_graph_fast_path(
        self, request: TransitionRequest, snapshot: Mapping[str, Any]
    ) -> bool:
        """Return whether this play can switch inside the unchanged graph.

        The gate may stay open only when the previous transition committed,
        the gate is open and not latched, and the runtime confirms the switch
        keeps source, rate, output mode, DSP state and graph topology intact.
        Any uncertainty falls back to the full transition.
        """
        if self.gate.closed or self.gate.failure_latched:
            return False
        if self.last_error is not None:
            return False
        if self.last_result is None or not self.last_result.committed:
            return False
        evaluator = getattr(self.runtime, "evaluate_same_graph_fast_path", None)
        if not callable(evaluator):
            return False
        try:
            return bool(await evaluator(request, snapshot))
        except Exception as exc:
            logger.warning(
                "Same-graph fast-path evaluation failed; using full transition: %s",
                exc,
            )
            return False

    async def _execute_same_graph_fast_path(
        self,
        stages: _TransitionStages,
        request: TransitionRequest,
    ) -> TransitionResult:
        """Switch the transport inside the unchanged open graph (fast path).

        A semantically identical-graph track switch: committed source, rate,
        output mode, DSP state and graph topology are unchanged, so the
        transport switches without the output gate or graph re-verification.
        A failure here still runs the shared cleanup with gate_required=False
        (no gate was ever closed).
        """
        stages.gate_required = False
        await self._stage(stages, "quiet-old-source", lambda: self.runtime.quiet_old_source(request))
        await self._stage(stages, "target-source-prepare", lambda: self.runtime.prepare_target_source(request))
        if request.should_play:
            # The fast path never closes the hardware output gate, so MPV
            # source volume is the only mute.  Restore it before unpausing so
            # the track does not start at volume 0 and silently consume its
            # opening frames.
            await self._stage(stages, "source-volume-restore", lambda: self.runtime.set_source_volume(100, stages.transition_id))
        await self._stage(stages, "target-source-start", lambda: self.runtime.start_target_source(request))
        verifier = self.runtime.verify_same_graph_commit
        state = await self._stage(stages, "commit-readback", lambda: verifier(request))
        if not bool(state.get("committed", True)):
            raise RuntimeError("fast-path readback did not satisfy commit contract")
        return self._finish_committed(stages, request, state)

    async def _execute_graph_commit_path(
        self,
        stages: _TransitionStages,
        request: TransitionRequest,
        snapshot: Mapping[str, Any],
        effects_state: Mapping[str, Any],
        *,
        audible_output: bool,
    ) -> TransitionResult:
        """Run the graph-commit-only branches (measurement entry, output-mode
        switch, sample-rate policy without source reload)."""
        if request.operation == "measurement-entry":
            verifier = getattr(self.runtime, "verify_measurement_entry", None)
            if callable(verifier):
                state = await self._stage(stages, "measurement-entry-graph-readback", lambda: verifier(request))
            else:
                state = await self._stage(stages, "measurement-entry-graph-readback", lambda: self.runtime.verify_transition_graph(request))
            if not bool(state.get("committed", True)):
                raise RuntimeError("measurement entry readback did not satisfy graph contract")
        else:
            # The target graph is not committed until the old transport has
            # been put back under the still-closed gate.  Starting Spotify
            # here is intentional: its newly-created sink ports are part of
            # the same final source-graph commit.
            restorer = getattr(self.runtime, "restore_output_mode_transport", None)
            if not callable(restorer):
                raise RuntimeError("Coordinator output-mode transport restore is unavailable")
            await self._stage(
                stages,
                "output-mode-transport-restore",
                lambda: restorer(request, snapshot, stages.transition_id),
                gate_check="after-output-mode-transport-restore",
            )
            post_start_reconciler = getattr(self.runtime, "reconcile_post_start_graph", None)
            if not callable(post_start_reconciler):
                raise RuntimeError("Coordinator output-mode graph reconciliation is unavailable")
            post_start_state = await self._stage(stages, "post-start-graph-reconcile", lambda: post_start_reconciler(request))
            if not isinstance(post_start_state, Mapping) or not post_start_state.get("graph_complete", False):
                raise RuntimeError("output-mode post-start graph reconciliation did not confirm a complete graph")
            post_start_graph_state = dict(post_start_state)
            verifier = getattr(self.runtime, "verify_output_mode_runtime", None)
            if not callable(verifier):
                raise RuntimeError("Coordinator output-mode runtime verifier is unavailable")
            state = await self._stage(stages, "output-mode-graph-readback", lambda: verifier(request))
            if not bool(state.get("committed", True)):
                raise RuntimeError("output-mode graph readback did not satisfy commit contract")

        if effects_state.get("dsp_reinitialized") or request.operation == "output-mode-switch":
            stabilizer = getattr(self.runtime, "stabilize_effects_after_rate_change", None)
            if not callable(stabilizer):
                raise RuntimeError("post-transition DSP stabilization is not available")
            dsp_state = await self._stage(stages, "effects-dsp-stabilize", lambda: stabilizer(request, dsp_reinitialized=True))
            if not isinstance(dsp_state, Mapping) or not dsp_state.get("stabilized", False):
                raise RuntimeError("post-transition DSP stabilization was not confirmed")

        if request.operation == "output-mode-switch":
            committer = getattr(self.runtime, "commit_output_mode_runtime", None)
            if not callable(committer):
                raise RuntimeError("Coordinator output-mode persistence is unavailable")
            committed_mode = await self._stage(stages, "output-mode-persist", lambda: committer(request))
            if isinstance(committed_mode, Mapping):
                state = {**dict(state), **dict(committed_mode)}
        elif request.operation == "sample-rate-policy":
            committed_policy = await self._stage(stages, "sample-rate-policy-persist", lambda: self.runtime.commit_sample_rate_policy(request))
            if isinstance(committed_policy, Mapping):
                state = {**dict(state), **dict(committed_policy)}

        after_physical_restore = None
        if request.operation == "output-mode-switch":
            finalizer = getattr(self.runtime, "finalize_output_mode_graph_after_gate_open", None)
            if callable(finalizer):
                async def finalize_graph() -> None:
                    final_graph = await self._stage(stages, "post-gate-output-mode-graph", lambda: finalizer(request))
                    if not isinstance(final_graph, Mapping) or not final_graph.get("graph_complete", False):
                        raise RuntimeError("output-mode graph changed when the output gate opened")

                after_physical_restore = finalize_graph
        await self._restore_output_gate(
            stages,
            audible_output=audible_output,
            after_physical_restore=after_physical_restore,
        )
        return self._finish_committed(stages, request, state, effects_state=effects_state)

    async def _execute_standard_path(
        self,
        stages: _TransitionStages,
        request: TransitionRequest,
        effects_state: Mapping[str, Any],
        *,
        audible_output: bool,
    ) -> TransitionResult:
        """Run the standard source handoff path (Local, Radio, Spotify,
        recovery, restore)."""
        await self._stage(stages, "target-source-prepare", lambda: self.runtime.prepare_target_source(request))

        if stages.gate_required:
            # The target starts muted at both boundaries: hardware is still
            # gated and MPV source volume is explicitly zero.
            await self._stage(
                stages,
                "before-target-source-start-gate",
                lambda: self.ensure_output_gate_closed(stages.transition_id, stage="before-target-source-start"),
            )
        await self._stage(stages, "target-source-start", lambda: self.runtime.start_target_source(request))

        post_start_graph_state: Mapping[str, Any] = {}
        dsp_state: Mapping[str, Any] = {}
        if stages.gate_required:
            # Source creation can recreate PipeWire ports and lose a
            # production edge after the earlier effects/helper stage.
            # Reconcile that bounded link-only drift while the gate is still
            # closed, before the existing staged commit readback.
            post_start_reconciler = getattr(self.runtime, "reconcile_post_start_graph", None)
            if callable(post_start_reconciler):
                post_start_state = await self._stage(stages, "post-start-graph-reconcile", lambda: post_start_reconciler(request))
                if not isinstance(post_start_state, Mapping) or not post_start_state.get("graph_complete", False):
                    raise RuntimeError("post-start graph reconciliation did not confirm a complete graph")
                post_start_graph_state = dict(post_start_state)

            # The graph must be read back while the output gate is still
            # closed and the target source is still at volume 0.  Only after
            # this staged commit succeeds may source volume and the hardware
            # gate be restored.
            staged_verifier = getattr(self.runtime, "verify_transition_graph", None)
            if callable(staged_verifier):
                state = await self._stage(stages, "staged-graph-readback", lambda: staged_verifier(request))
            else:
                state = await self._stage(stages, "staged-graph-readback", lambda: self.runtime.verify_committed_transition(request))
            if not bool(state.get("committed", True)):
                raise RuntimeError("staged transition readback did not satisfy graph contract")

            restore_source_volume = bool(
                not request.graph_only
                and (
                    request.should_play
                    or (
                        request.operation == "measurement-restore"
                        and source_policy.is_mpv_source(request.source)
                    )
                )
            )
            if restore_source_volume:
                await self._stage(
                    stages,
                    "before-source-volume-gate",
                    lambda: self.ensure_output_gate_closed(stages.transition_id, stage="before-source-volume-restore"),
                )
                await self._stage(stages, "source-volume-restore", lambda: self.runtime.set_source_volume(100, stages.transition_id))

                dsp_required = bool(request.rate_change or effects_state.get("dsp_reinitialized"))
                if dsp_required:
                    stabilizer = getattr(self.runtime, "stabilize_effects_after_rate_change", None)
                    if not callable(stabilizer):
                        raise RuntimeError("post-start DSP stabilization is not available")
                    dsp_state = await self._stage(
                        stages,
                        "effects-dsp-stabilize",
                        lambda: stabilizer(request, dsp_reinitialized=bool(effects_state.get("dsp_reinitialized"))),
                    )
                    if not isinstance(dsp_state, Mapping) or not dsp_state.get("stabilized", False):
                        raise RuntimeError("post-start DSP stabilization was not confirmed")

                await self._stage(
                    stages,
                    "after-dsp-stabilization-gate",
                    lambda: self.ensure_output_gate_closed(stages.transition_id, stage="after-dsp-stabilization"),
                )
                state = await self._stage(stages, "commit-readback", lambda: self.runtime.verify_committed_transition(request))
        else:
            if request.should_play:
                await self._stage(stages, "source-volume-restore", lambda: self.runtime.set_source_volume(100, stages.transition_id))
            state = await self._stage(stages, "commit-readback", lambda: self.runtime.verify_committed_transition(request))
        if not bool(state.get("committed", True)):
            raise RuntimeError("transition readback did not satisfy commit contract")

        if request.operation == "sample-rate-policy":
            committed_policy = await self._stage(stages, "sample-rate-policy-persist", lambda: self.runtime.commit_sample_rate_policy(request))
            if isinstance(committed_policy, Mapping):
                state = {**dict(state), **dict(committed_policy)}

        await self._restore_output_gate(stages, audible_output=audible_output)
        return self._finish_committed(
            stages,
            request,
            state,
            effects_state=effects_state,
            post_start_graph_state=post_start_graph_state,
            dsp_state=dsp_state,
        )

    async def _restore_output_gate(
        self,
        stages: _TransitionStages,
        *,
        audible_output: bool,
        after_physical_restore: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Run the shared output-gate restore tail after a staged commit.

        Both commit paths finish the same way: re-confirm the still-closed
        gate, hold the settled state, then physically restore the gate.  A
        transition that never closed the gate (``gate_required`` False) is a
        no-op, matching the fast path that runs without gate ownership.
        """
        if not stages.gate_required:
            return
        await self._stage(
            stages,
            "before-output-gate-restore",
            lambda: self.ensure_output_gate_closed(stages.transition_id, stage="before-output-gate-restore"),
        )
        stages.enter("output-gate-restore")
        await self._hold_gate_after_verification()
        await self._restore_gate(
            stages.transition_id,
            audible_output=audible_output,
            after_physical_restore=after_physical_restore,
        )

    async def execute(self, request: TransitionRequest) -> TransitionResult:
        """Run one transition and commit only after complete readback."""
        transition_id = f"tr-{uuid4().hex}"
        async with self.lock:
            stages = _TransitionStages(transition_id)
            active_request = request
            snapshot: Mapping[str, Any] = {}
            stages.gate_required = bool(
                request.rate_change
                or request.reload_source
                or request.operation in {
                    "play",
                    "resume",
                    "replay",
                    "queue",
                    "spotify-play",
                    "spotify-toggle",
                    "qobuz-play",
                    "qobuz-toggle",
                    "measurement-restore",
                    "measurement-entry",
                    "output-mode-switch",
                    "sample-rate-policy",
                    "recovery",
                    "graph-reconcile",
                }
            )
            audible_output = self._request_expects_audible_output(request)
            try:
                if not await self._reconcile_startup_gate_locked():
                    raise RuntimeError(
                        f"stale output gate could not be reconciled: {self._startup_gate_error or 'unknown error'}"
                    )
                if active_request.skip_if_committed_owner is not None:
                    try:
                        should_skip = await active_request.skip_if_committed_owner()
                    except Exception as exc:
                        logger.warning(
                            "Playback transition claim revalidation failed; running claim: %s",
                            exc,
                        )
                        should_skip = False
                    if should_skip:
                        return await self._skip_claim_noop(stages, active_request)
                snapshot = await self.runtime.read_transition_snapshot(request)
                if (
                    not active_request.audio_overview
                    and isinstance(snapshot, Mapping)
                    and snapshot.get("audio_overview")
                ):
                    active_request = replace(
                        active_request,
                        audio_overview=dict(snapshot["audio_overview"]),
                    )
                self._validate_transition_target_rate(active_request)
                if (
                    active_request.operation == "sample-rate-policy"
                    and active_request.reload_source
                    and active_request.source == "local"
                    and active_request.restore_position is None
                ):
                    previous_position = (snapshot.get("player") or {}).get("position")
                    if isinstance(previous_position, (int, float)) and previous_position > 0:
                        active_request = replace(
                            active_request,
                            restore_position=float(previous_position),
                        )
                restore_validator = getattr(
                    self.runtime, "validate_measurement_restore_intent", None
                )
                if (
                    active_request.operation == "measurement-restore"
                    and active_request.restore_intent
                    and callable(restore_validator)
                    and not await restore_validator(active_request, snapshot)
                ):
                    return await self._skip_measurement_restore(
                        stages, active_request, "intent-changed-before-gate"
                    )
                fast_path = await self._evaluate_same_graph_fast_path(
                    active_request, snapshot
                )
                if fast_path:
                    return await self._execute_same_graph_fast_path(stages, active_request)

                if stages.gate_required:
                    await self._stage(
                        stages,
                        "output-gate-close",
                        lambda: self._close_gate(stages.transition_id, audible_output=audible_output),
                    )

                await self._stage(stages, "quiet-old-source", lambda: self.runtime.quiet_old_source(request))

                if (
                    active_request.operation == "measurement-restore"
                    and active_request.restore_intent
                    and callable(restore_validator)
                    and not await restore_validator(active_request, snapshot)
                ):
                    return await self._skip_measurement_restore(
                        stages, active_request, "intent-changed-after-quiet"
                    )

                if stages.gate_required and not active_request.graph_only:
                    # Radio streams expose their decoded rate only after a
                    # paused target stream exists.  The adapter may stage that
                    # target under the already-closed gate and return the
                    # authoritative rate; all following stages then use the
                    # resolved immutable request.
                    resolver = getattr(self.runtime, "resolve_target_rate", None)
                    if callable(resolver):
                        resolved_rate = await self._stage(stages, "target-rate-resolve", lambda: resolver(active_request))
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
                        # A radio's initial configured fallback (usually 44.1
                        # kHz) may be replaced by its decoded live rate while
                        # the target is staged.  Recompute the actual
                        # transition after that resolution so an already
                        # aligned 48 kHz stream does not rebuild DSP/helper.
                        active_request = replace(
                            active_request,
                            rate_change=not (
                                snapshot_active_rate == active_request.target_rate
                                and snapshot_force_rate in {None, 0, active_request.target_rate}
                            ),
                        )
                    # Re-validate against the resolved rate.  The output gate
                    # is already closed and the old source quieted at this
                    # point, so a rejection must run the failure-restore
                    # machinery instead of propagating raw.
                    try:
                        self._validate_transition_target_rate(active_request)
                    except UnsupportedTransitionRateError as exc:
                        raise RuntimeError(str(exc)) from exc
                    await self._stage(
                        stages,
                        "target-rate",
                        lambda: self.runtime.establish_target_rate(active_request),
                        gate_check="after-target-rate",
                    )

                effects_state: Mapping[str, Any] = {}
                effects_result = await self._stage(
                    stages,
                    "effects-helper-links",
                    lambda: self.runtime.establish_effects_and_helper(active_request),
                    gate_check="after-effects-helper-links" if stages.gate_required else None,
                )
                if isinstance(effects_result, Mapping):
                    effects_state = dict(effects_result)

                if active_request.operation in {"measurement-entry", "output-mode-switch"} or (
                    active_request.operation == "sample-rate-policy"
                    and not active_request.reload_source
                ):
                    return await self._execute_graph_commit_path(
                        stages, active_request, snapshot, effects_state, audible_output=audible_output
                    )
                return await self._execute_standard_path(
                    stages, active_request, effects_state, audible_output=audible_output
                )
            except UnsupportedTransitionRateError:
                # Rejected before any transition state was mutated: surface
                # the clean rate/policy error to the caller instead of
                # running the failure-restore machinery.
                raise
            except asyncio.CancelledError as exc:
                raise await self._fail_transition(
                    stages,
                    active_request,
                    snapshot,
                    transition_id=transition_id,
                    cancelled=True,
                    failure=exc,
                )
            except Exception as exc:
                raise await self._fail_transition(
                    stages,
                    active_request,
                    snapshot,
                    transition_id=transition_id,
                    cancelled=False,
                    failure=exc,
                ) from exc

    async def restore_measurement(
        self,
        *,
        source: str,
        target_rate: int,
        target_url: str | None,
        target_track: Mapping[str, Any],
        should_play: bool,
        rate_change: bool = True,
        reload_source: bool = True,
        restore_position: float | None = None,
        restore_intent: Mapping[str, Any] | None = None,
        attempt_epoch: int | None = None,
    ) -> TransitionResult:
        """Restore playback after a measurement through the same state machine."""
        return await self.execute(TransitionRequest(
            operation="measurement-restore",
            source=source,
            target_rate=target_rate,
            target_url=target_url,
            target_track=target_track,
            should_play=should_play,
            rate_change=rate_change,
            reload_source=reload_source,
            detail="measurement-release",
            restore_position=restore_position,
            restore_intent=dict(restore_intent or {}),
            attempt_epoch=attempt_epoch,
        ))

    async def reconcile_measurement_session(
        self,
        *,
        target_rate: int,
        initial_graph: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Repair one active-measurement link loss without touching transport.

        An already-active measurement session has already completed its normal
        rate/source handoff.  Its entry preflight may therefore use this
        narrower Coordinator transaction when only the 2.1/2.2 production
        links drifted.  No source, rate, preset, or helper lifecycle stage is
        entered here; the adapter's reconcile hook is link-only by contract.
        """
        if not isinstance(target_rate, int) or target_rate <= 0:
            raise RuntimeError("measurement session reconcile has no valid target rate")
        if bool(initial_graph.get("links_complete")):
            return {
                "committed": True,
                "reconciled": False,
                "graph_complete": True,
                "graph_signature": initial_graph.get("signature"),
            }

        reader = getattr(self.runtime, "read_measurement_session_graph", None)
        reconciler = getattr(
            self.runtime,
            "reconcile_measurement_session_graph",
            None,
        )
        if not callable(reader) or not callable(reconciler):
            raise RuntimeError(
                "measurement session graph reconciliation is unavailable"
            )

        transition_id = f"tr-{uuid4().hex}"
        async with self.lock:
            if not await self._reconcile_startup_gate_locked():
                raise RuntimeError(
                    "stale output gate could not be reconciled: "
                    f"{self._startup_gate_error or 'unknown error'}"
                )
            if self.gate.failure_latched or self.gate.closed:
                raise RuntimeError(
                    "measurement session reconcile is blocked by a latched output gate"
                )

            stage = "measurement-session-readonly"
            current = await reader(target_rate)
            if not isinstance(current, Mapping):
                raise RuntimeError(
                    "measurement session graph readback was not a mapping"
                )
            if current.get("links_complete"):
                return {
                    "committed": True,
                    "reconciled": False,
                    "graph_complete": True,
                    "graph_signature": current.get("signature"),
                }
            if not current.get("repairable_link_loss"):
                raise RuntimeError(
                    "measurement session graph is not a repairable link-only loss"
                )

            gate_owned = False
            try:
                stage = "measurement-session-gate-close"
                await self._close_gate(transition_id, audible_output=True)
                gate_owned = True
                await self.ensure_output_gate_closed(
                    transition_id,
                    stage="measurement-session-after-gate-close",
                )

                stage = "measurement-session-readonly-under-gate"
                gated = await reader(target_rate)
                if not isinstance(gated, Mapping):
                    raise RuntimeError(
                        "measurement session gated graph readback was not a mapping"
                    )
                if gated.get("links_complete"):
                    stage = "measurement-session-gate-restore"
                    await self._restore_gate(transition_id, audible_output=True)
                    gate_owned = False
                    return {
                        "committed": True,
                        "reconciled": False,
                        "graph_complete": True,
                        "graph_signature": gated.get("signature"),
                    }
                if not gated.get("repairable_link_loss"):
                    raise RuntimeError(
                        "measurement session gated graph is not a repairable link-only loss"
                    )

                stage = "measurement-session-link-reconcile"
                await reconciler(target_rate)

                stage = "measurement-session-stable-readback"
                readbacks, signatures, stable = await stable_graph_readbacks(
                    lambda: reader(target_rate)
                )
                if not stable:
                    raise RuntimeError(
                        "measurement session graph did not reach two stable canonical readbacks"
                    )

                await self.ensure_output_gate_closed(
                    transition_id,
                    stage="measurement-session-before-gate-restore",
                )
                stage = "measurement-session-gate-restore"
                await self._restore_gate(transition_id, audible_output=True)
                gate_owned = False

                final = dict(readbacks[-1])
                final.update(
                    {
                        "committed": True,
                        "reconciled": True,
                        "graph_complete": True,
                        "stable_readbacks": 2,
                        "graph_signature": signatures[-1],
                    }
                )
                result = TransitionResult(
                    transition_id=transition_id,
                    committed=True,
                    source="measurement",
                    target_rate=target_rate,
                    state=final,
                )
                self._record_result(result)
                self.last_error = None
                logger.info(
                    "Measurement session graph reconcile committed: "
                    "transition_id=%s target_rate=%s signature=%s stable_readbacks=2",
                    transition_id,
                    target_rate,
                    signatures[-1],
                )
                return final
            except Exception as exc:
                if gate_owned or (
                    self.gate.closed and self.gate.transition_id == transition_id
                ):
                    await self._latch_failure(transition_id)
                failure = PlaybackTransitionFailure(
                    "measurement session graph reconcile failed at "
                    f"{stage}: {exc}",
                    transition_id=transition_id,
                    stage=stage,
                )
                self.last_error = failure.as_status()
                logger.warning(
                    "Measurement session graph reconcile failed: "
                    "transition_id=%s stage=%s error=%s",
                    transition_id,
                    stage,
                    exc,
                )
                raise failure from exc
