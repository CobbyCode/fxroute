# SPDX-License-Identifier: AGPL-3.0-only

"""Uncommitted-transition cleanup contract and its cancellation-safe drain.

Extracted from :class:`PlaybackTransitionCoordinator` so the shared failure
and cancellation tail lives in one cohesive module.  The mixin runs on the
composing coordinator instance; it relies on the coordinator for ``_stage``
and on the gate mixin for the gate ownership methods.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping

from .models import PlaybackTransitionFailure, TransitionRequest
from .protocol import TransitionRuntime
from .stages import _TransitionStages
import playback.source_policy as source_policy

logger = logging.getLogger(__name__)


class _TransitionCleanupMixin:
    """Attributes provided by the composing coordinator instance."""
    runtime: TransitionRuntime
    gate: OutputGateState
    last_error: dict[str, Any] | None
    lock: asyncio.Lock

    async def _restore_committed_source(
        self,
        failed_request: TransitionRequest,
        restore_request: TransitionRequest,
        *,
        transition_id: str,
        gate_required: bool,
    ) -> bool:
        """Physically restore the previously committed source through the
        Coordinator's own transition stages (never a nested transition).

        Runs the same bounded stage sequence as a normal Local/Radio handoff
        under the still-closed output gate: old rate, effects and helper for
        the old rate, source/queue transport (including a committed native
        MPV playlist), post-start graph reconcile, staged graph readback,
        source volume 100, DSP stabilization when the failed Spotify
        transition reinitialized the DSP, and a final commit readback that
        must positively confirm source volume 100.  Between the critical
        stages the physical output gate is re-confirmed.  Returns True only
        when the old source is confirmed in its previous transport state on
        the complete old graph; any stage failure keeps the failure latch.
        """
        stages = _TransitionStages(transition_id)
        stages.gate_required = gate_required

        def boundary(stage: str) -> str | None:
            """Return the gate-confirmation label only when the gate is owned."""
            return stage if gate_required else None

        try:
            # A verify failure after a successful Spotify start can leave the
            # Spotify sink input active while the old rate and graph are
            # restored.  Quiesce it through the existing bounded release
            # helper before any old-graph stage touches the graph; if the
            # active Spotify source does not release within the existing
            # bound, the restore fails and the failure latch stays.
            if failed_request.source == "spotify":
                if not await self.runtime.wait_for_pipewire_spotify_release():
                    logger.warning(
                        "Failed-transition source restore aborted: active Spotify sink "
                        "input did not quiesce before the old source restore"
                    )
                    return False
            # The Coordinator-owned hardware gate must be physically closed
            # before ANY mutating restore stage (rate, effects/helper, MPV,
            # graph, volume) runs: the original Spotify transition may itself
            # have failed at output-gate-close, leaving the gate unverified.
            if gate_required:
                await self.ensure_output_gate_closed(
                    transition_id,
                    stage="failed-transition-restore-before-rate",
                )
            await self._stage(
                stages,
                "restore-rate",
                lambda: self.runtime.establish_target_rate(restore_request),
                gate_check=boundary("failed-transition-restore-after-rate"),
            )
            effects_state: Mapping[str, Any] = {}
            effects_result = await self._stage(
                stages,
                "restore-effects-helper",
                lambda: self.runtime.establish_effects_and_helper(restore_request),
                gate_check=boundary("failed-transition-restore-after-effects-helper"),
            )
            if isinstance(effects_result, Mapping):
                effects_state = dict(effects_result)
            await self._stage(
                stages,
                "restore-prepare",
                lambda: self.runtime.prepare_target_source(restore_request),
            )
            if gate_required:
                await self.ensure_output_gate_closed(
                    transition_id,
                    stage="failed-transition-restore-before-start",
                )
            await self._stage(
                stages,
                "restore-start",
                lambda: self.runtime.start_target_source(restore_request),
            )
            # Source creation can recreate PipeWire ports and lose a
            # production edge after the earlier effects/helper stage.
            # Reconcile that bounded link-only drift while the gate is still
            # closed, before the existing staged commit readback.
            post_state = await self._stage(
                stages,
                "restore-post-start-reconcile",
                lambda: self.runtime.reconcile_post_start_graph(restore_request),
            )
            if not isinstance(post_state, Mapping) or not post_state.get(
                "graph_complete", False
            ):
                logger.warning(
                    "Failed-transition source restore aborted: post-start graph "
                    "reconciliation did not confirm a complete graph"
                )
                return False
            graph_state = await self._stage(
                stages,
                "restore-staged-readback",
                lambda: self.runtime.verify_transition_graph(restore_request),
            )
            if not bool(graph_state.get("committed", True)):
                logger.warning(
                    "Failed-transition source restore aborted: staged graph readback "
                    "did not satisfy the graph contract"
                )
                return False
            # The source-volume invariant holds for Local/Radio regardless of
            # the pre-transition transport state: after a successful restore
            # MPV source volume is always 100 (also for a previously paused
            # source, which the failed handoff left at volume 0).  The volume
            # restore happens only under a confirmed closed gate.  After the
            # volume and the optional DSP stabilization the gate is confirmed
            # again (same sequence as the normal Coordinator: before-volume
            # gate -> volume 100 -> optional DSP -> gate re-check -> final
            # commit readback).
            if gate_required:
                await self.ensure_output_gate_closed(
                    transition_id,
                    stage="failed-transition-restore-before-volume",
                )
            await self._stage(
                stages,
                "restore-volume",
                lambda: self.runtime.set_source_volume(100, transition_id),
            )
            # DSP stabilization is not artificially forced for paused
            # restores; it keeps its existing rate/DSP-reinit condition.
            if restore_request.should_play and bool(
                restore_request.rate_change or effects_state.get("dsp_reinitialized")
            ):
                dsp_state = await self._stage(
                    stages,
                    "restore-dsp-stabilize",
                    lambda: self.runtime.stabilize_effects_after_rate_change(
                        restore_request,
                        dsp_reinitialized=bool(effects_state.get("dsp_reinitialized")),
                    ),
                )
                if not isinstance(dsp_state, Mapping) or not dsp_state.get(
                    "stabilized", False
                ):
                    logger.warning(
                        "Failed-transition source restore aborted: DSP stabilization "
                        "was not confirmed"
                    )
                    return False
            if gate_required:
                await self.ensure_output_gate_closed(
                    transition_id,
                    stage="failed-transition-restore-after-dsp",
                )
            final_state = await self._stage(
                stages,
                "restore-commit-readback",
                lambda: self.runtime.verify_committed_transition(restore_request),
            )
            if not bool(final_state.get("committed", True)):
                logger.warning(
                    "Failed-transition source restore aborted: final commit readback "
                    "did not satisfy the commit contract"
                )
                return False
            try:
                source_volume = int(final_state.get("source_volume"))
            except (TypeError, ValueError):
                source_volume = None
            if source_volume != 100:
                logger.warning(
                    "Failed-transition source restore aborted: final commit readback "
                    "did not positively confirm source volume 100: volume=%s",
                    final_state.get("source_volume"),
                )
                return False
            return True
        except Exception as exc:
            logger.warning("Failed-transition source restore failed: %s", exc)
            return False

    async def _cleanup_uncommitted_transition(
        self,
        request: TransitionRequest,
        snapshot: Mapping[str, Any] | None,
        *,
        transition_id: str,
        gate_required: bool,
    ) -> bool:
        """Run the authoritative uncommitted-transition cleanup contract.

        Shared by the stage-failure and cancellation paths so both leave the
        same terminal state: attenuate and pause the source, roll back the
        output-mode runtime, restore the pre-transition local/radio volume,
        detect a staged target, abort the failed handoff (the adapter may
        physically restore the previously committed source), and finally
        either restore the output gate (recovered source) or latch a failure.
        Returns the actual final failure_latched state.
        """
        try:
            await self.runtime.set_source_volume(0, transition_id)
        except Exception:
            pass
        try:
            await self.runtime.pause_source_after_failure(request)
        except Exception:
            pass
        if request.operation == "output-mode-switch":
            try:
                await self.runtime.rollback_output_mode_runtime(request, snapshot)
            except Exception:
                logger.warning(
                    "Output-mode runtime rollback failed; keeping the failure gate latched",
                    exc_info=True,
                )
        if source_policy.is_mpv_source(request.source):
            # The source was attenuated to 0 during the quiet stage.
            # A failed transition must not leave it muted forever:
            # restore the pre-transition source volume from the snapshot.
            previous_player = dict((snapshot or {}).get("player") or {})
            previous_volume = previous_player.get("volume")
            if isinstance(previous_volume, (int, float)):
                try:
                    await self.runtime.set_source_volume(int(round(previous_volume)), transition_id)
                except Exception:
                    pass
        target_staged = False
        try:
            target_staged = bool(self.runtime.target_source_staged(request))
        except Exception:
            target_staged = False
        abort_recovered_source = False
        verdict: Any = None
        try:
            # Strict verdict contract: only an explicit restore request
            # from the abort hook means the previously committed source
            # must be physically restored (test adapters and None returns
            # must keep the failure latch).  A transition that never
            # closed the output gate (same-graph fast path) cannot
            # re-confirm a gate it does not own: the restore then runs
            # without boundary re-checks, exactly like the fast path
            # itself ran.
            verdict = await self.runtime.abort_failed_transition(
                request,
                snapshot,
                target_staged=target_staged,
            )
        except Exception:
            logger.warning(
                "Playback transition abort cleanup failed",
                exc_info=True,
            )
            verdict = None
        restore_request = (
            verdict.get("restore")
            if isinstance(verdict, Mapping)
            and isinstance(verdict.get("restore"), TransitionRequest)
            else None
        )
        if restore_request is not None:
            abort_recovered_source = await self._restore_committed_source(
                request,
                restore_request,
                transition_id=transition_id,
                gate_required=gate_required,
            )
            restored_track = dict(restore_request.target_track or {})
            if abort_recovered_source:
                try:
                    await self.runtime.publish_restored_source(restore_request)
                except Exception:
                    logger.warning(
                        "Restored-source metadata publish failed",
                        exc_info=True,
                    )
                logger.warning(
                    "Failed %s transition restored committed %s source for retry: "
                    "track_id=%s url=%s",
                    request.operation,
                    restored_track.get("source"),
                    restored_track.get("id"),
                    restored_track.get("url"),
                )
            else:
                logger.warning(
                    "Failed %s transition could not restore the committed source; "
                    "keeping the failure gate latched: source=%s",
                    request.operation,
                    restored_track.get("source"),
                )
                try:
                    await self.runtime.normalize_queue_after_native_loss()
                except Exception:
                    logger.warning(
                        "Restore-failure queue normalization failed",
                        exc_info=True,
                    )
        if not gate_required:
            # No output gate was ever closed by this transition; nothing is
            # latched and the reported failure state reflects that.
            return False
        if not abort_recovered_source:
            await self._latch_failure(transition_id)
            return True
        # The abort fully restored the previously committed source (e.g.
        # after a failed Spotify handoff).  Open the output gate so the
        # restored source is audible instead of latching a failure for a
        # working source, using the same final gate sequence as a normal
        # commit: confirm the gate is still physically closed, hold the
        # settled state, then restore.  If the gate itself cannot be
        # restored, fall back to the failure latch as the safe state and
        # report it latched.
        try:
            await self.ensure_output_gate_closed(
                transition_id,
                stage="before-recovered-gate-restore",
            )
            await self._hold_gate_after_verification()
            await self._restore_gate(transition_id, audible_output=True)
            return False
        except Exception:
            logger.warning(
                "Playback transition gate restore after recovered abort "
                "failed; latching failure",
                exc_info=True,
            )
            await self._latch_failure(transition_id)
            return True

    def _start_cleanup_task(
        self,
        request: TransitionRequest,
        snapshot: Mapping[str, Any] | None,
        *,
        transition_id: str,
        gate_required: bool,
    ) -> asyncio.Task:
        """Start the shared cleanup contract as an independently cancellable task."""
        return asyncio.create_task(
            self._cleanup_uncommitted_transition(
                request,
                snapshot,
                transition_id=transition_id,
                gate_required=gate_required,
            ),
            name="playback-transition-cleanup",
        )

    async def _drain_cleanup_task(
        self,
        cleanup_task: asyncio.Task,
        *,
        transition_id: str,
        gate_required: bool,
    ) -> tuple[bool, BaseException | None]:
        """Drain a cleanup task to its terminal state, surviving cancellation.

        The caller owns the transition lock; a cancelled caller must not
        release that section while cleanup is still mutating state.  Every
        delivered CancelledError is recorded and the drain continues until the
        cleanup task actually finished; the caller then decides whether the
        cancellation wins over the original failure.  ``task.result()`` is
        read only after the task is done, so no additional cancellation
        boundary exists after the state cleanup already completed.  A failed
        cleanup task falls back to the output gate failure latch.  Returns
        (final failure_latched, caught CancelledError or None).
        """
        cancelled_exc: BaseException | None = None
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError as exc:
                if cancelled_exc is None:
                    cancelled_exc = exc
                continue
        try:
            failure_latched = bool(cleanup_task.result())
        except asyncio.CancelledError:
            logger.warning(
                "Playback transition cleanup task was cancelled before "
                "finishing; latching the output gate"
            )
            failure_latched = await self._latch_cleanup_fallback(
                transition_id, gate_required=gate_required
            )
        except Exception as exc:
            logger.warning(
                "Playback transition cleanup failed; latching the output gate: %s",
                exc,
            )
            failure_latched = await self._latch_cleanup_fallback(
                transition_id, gate_required=gate_required
            )
        return failure_latched, cancelled_exc

    async def _latch_cleanup_fallback(
        self, transition_id: str, *, gate_required: bool
    ) -> bool:
        """Best-effort failure latch after a broken cleanup task."""
        if not gate_required:
            return False
        try:
            await self._latch_failure(transition_id)
        except Exception:
            logger.warning(
                "Playback transition cleanup latch fallback failed",
                exc_info=True,
            )
        return True

    async def _fail_transition(
        self,
        stages: _TransitionStages,
        request: TransitionRequest,
        snapshot: Mapping[str, Any],
        *,
        transition_id: str,
        cancelled: bool,
        failure: BaseException,
    ) -> BaseException:
        """Run the shared uncommitted-transition cleanup and record the
        terminal error; returns the exception the caller must re-raise.

        The stage-failure and cancellation paths converge here so both leave
        the same terminal state: start the independent cleanup task, drain it
        to completion while surviving further cancellation, then record the
        error and stage timing.  A cancellation that arrived while the
        cleanup was still draining wins over the original stage failure as
        the caller control flow, never a rewritten failure.
        """
        cleanup_task = self._start_cleanup_task(
            request,
            snapshot,
            transition_id=transition_id,
            gate_required=stages.gate_required,
        )
        failure_latched, cancelled_exc = await self._drain_cleanup_task(
            cleanup_task,
            transition_id=transition_id,
            gate_required=stages.gate_required,
        )
        if cancelled:
            self.last_error = {
                "ok": False,
                "transition_id": transition_id,
                "stage": stages.stage,
                "failure_latched": bool(failure_latched),
                "cancelled": True,
                "message": f"Playback transition cancelled at {stages.stage}",
            }
            stages.log("cancelled")
            return failure
        if cancelled_exc is not None:
            self.last_error = {
                "ok": False,
                "transition_id": transition_id,
                "stage": stages.stage,
                "failure_latched": bool(failure_latched),
                "cancelled": True,
                "message": (
                    "Playback transition cancelled during failure "
                    f"cleanup at {stages.stage}"
                ),
            }
            stages.log("cancelled")
            return cancelled_exc
        error = PlaybackTransitionFailure(
            f"Playback transition failed at {stages.stage}: {failure}",
            transition_id=transition_id,
            stage=stages.stage,
            failure_latched=bool(failure_latched),
        )
        self.last_error = error.as_status()
        stages.log("failed")
        return error

