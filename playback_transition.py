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
import time
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Protocol
from uuid import uuid4


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
    detail: str = ""


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

    async def establish_effects_and_helper(self, request: TransitionRequest) -> None: ...

    async def prepare_target_source(self, request: TransitionRequest) -> None: ...

    async def start_target_source(self, request: TransitionRequest) -> None: ...

    async def set_source_volume(self, volume: int, transition_id: str) -> None: ...

    async def verify_committed_transition(self, request: TransitionRequest) -> Mapping[str, Any]: ...

    async def pause_source_after_failure(self, request: TransitionRequest) -> None: ...


class PlaybackTransitionCoordinator:
    """Serialize every playback transition and own the output-gate lifecycle.

    The adapter methods are intentionally coarse-grained.  They prevent a
    caller such as a status endpoint, peak watcher, or delayed repair task from
    directly changing the production graph: those callers can only submit a
    request, while this class controls the mutation order and commit point.
    """

    def __init__(self, runtime: TransitionRuntime, *, gate_settle_seconds: float = 0.25) -> None:
        self.runtime = runtime
        self.gate_settle_seconds = max(0.0, gate_settle_seconds)
        self.lock = asyncio.Lock()
        self.gate = OutputGateState()
        self.last_error: dict[str, Any] | None = None
        self.last_result: TransitionResult | None = None

    @property
    def transition_active(self) -> bool:
        return self.lock.locked()

    def status(self) -> dict[str, Any]:
        return {
            "active": self.transition_active,
            "gate": self.gate.as_dict(),
            "last_error": dict(self.last_error) if self.last_error else None,
            "last_transition_id": self.last_result.transition_id if self.last_result else None,
        }

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
        await self.runtime.set_hardware_mute(True, transition_id)
        if not await self.runtime.read_hardware_mute():
            raise RuntimeError("hardware output gate could not be confirmed closed")

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

    async def _latch_failure(self, transition_id: str) -> None:
        self.gate.failure_latched = True
        self.gate.closed = True
        self.gate.owner = "fxroute"
        self.gate.transition_id = transition_id
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
            active_request = request
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
                }
            )
            try:
                await self.runtime.read_transition_snapshot(request)
                if gate_required:
                    stage = "output-gate-close"
                    await self._close_gate(transition_id)

                stage = "quiet-old-source"
                await self.runtime.quiet_old_source(request)

                if gate_required:
                    # Radio streams expose their decoded rate only after a
                    # paused target stream exists.  The adapter may stage
                    # that target under the already-closed gate and return
                    # the authoritative rate; all following stages then use
                    # the resolved immutable request.
                    stage = "target-rate-resolve"
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
                    stage = "target-rate"
                    await self.runtime.establish_target_rate(active_request)

                stage = "effects-helper-links"
                await self.runtime.establish_effects_and_helper(active_request)

                stage = "target-source-prepare"
                await self.runtime.prepare_target_source(active_request)

                if gate_required:
                    # The target starts muted at both boundaries: hardware is
                    # still gated and MPV source volume is explicitly zero.
                    stage = "target-source-start"
                    await self.runtime.start_target_source(active_request)
                else:
                    stage = "target-source-start"
                    await self.runtime.start_target_source(active_request)

                stage = "source-volume-restore"
                await self.runtime.set_source_volume(100, transition_id)

                stage = "commit-readback"
                state = await self.runtime.verify_committed_transition(active_request)
                if not bool(state.get("committed", True)):
                    raise RuntimeError("transition readback did not satisfy commit contract")

                if gate_required:
                    stage = "output-gate-restore"
                    await self._hold_gate_after_verification()
                    await self._restore_gate(transition_id)

                result = TransitionResult(
                    transition_id=transition_id,
                    committed=True,
                    source=active_request.source,
                    target_rate=active_request.target_rate,
                    state=dict(state),
                )
                self.last_result = result
                self.last_error = None
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
