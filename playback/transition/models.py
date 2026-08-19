# SPDX-License-Identifier: AGPL-3.0-only

"""Core types and shared constants of the playback transition state machine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping


DSP_TRANSPORT_SINK = "fxroute_dsp_sink"

RecoveryValidator = Callable[[], Awaitable[bool]]
RecoveryExecutor = Callable[[], Awaitable[Any]]


class UnsupportedTransitionRateError(ValueError):
    """A transition target rate exceeds the selected output or FXRoute capability.

    Raised before any transition state is mutated, so it propagates to the
    API caller unchanged (mapped to HTTP 400 like the other rate/policy
    errors) without running the failure-restore machinery.
    """


class PlaybackTransitionFailure(RuntimeError):
    """A transition failed before its readback contract was committed."""

    def __init__(
        self,
        message: str,
        *,
        transition_id: str,
        stage: str,
        failure_latched: bool = True,
    ) -> None:
        super().__init__(message)
        self.transition_id = transition_id
        self.stage = stage
        self.failure_latched = failure_latched

    def as_status(self) -> dict[str, Any]:
        return {
            "ok": False,
            "transition_id": self.transition_id,
            "stage": self.stage,
            "failure_latched": self.failure_latched,
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
    # Assigned by the application before entering the Coordinator.  Unlike
    # the old odd/even generation, this identifies one attempted entry even
    # when a successor is already waiting for the transition lock.
    attempt_epoch: int | None = None
    native_queue: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    native_queue_index: int | None = None
    native_queue_loop: bool = False
    native_queue_shuffle: bool = False
    # Output-mode changes are staged as one Coordinator transaction.  The
    # target overview/config are deliberately carried as immutable request
    # data so runtime mutations cannot escape the gate-owned state machine.
    output_mode_target: Mapping[str, Any] = field(default_factory=dict)
    output_mode_config: Mapping[str, Any] = field(default_factory=dict)
    sample_rate_policy: Mapping[str, Any] = field(default_factory=dict)
    # Runtime-captured output overview, frozen at transition start and reused
    # by every graph-diagnosis stage so the expensive pactl/pw-cli enumeration
    # runs once per transition instead of once per readback.
    audio_overview: Mapping[str, Any] = field(default_factory=dict)
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
    # External-renderer claims (Spotify/Qobuz Connect) are guarded by the
    # caller against an already-committed owner before the transition lock,
    # which a queued claim can lose: an FXRoute-initiated start of the same
    # source commits while the claim waits.  The Coordinator re-validates
    # inside the lock; when this callback returns True the transition is
    # skipped as a no-op without touching the output gate or playback state.
    skip_if_committed_owner: Callable[[], Awaitable[bool]] | None = None

@dataclass(frozen=True)
class TransitionResult:
    transition_id: str
    committed: bool
    source: str
    target_rate: int | None
    state: Mapping[str, Any]

