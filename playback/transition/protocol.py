# SPDX-License-Identifier: AGPL-3.0-only

"""Application-owned runtime contract consumed by the coordinator."""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from .models import TransitionRequest


class TransitionRuntime(Protocol):
    """Application-owned operations invoked only by the coordinator."""

    async def read_hardware_mute(self) -> bool: ...

    async def set_hardware_mute(self, muted: bool, transition_id: str) -> None: ...

    async def read_sink_mute(self, sink_name: str) -> bool: ...

    async def set_sink_mute(
        self, sink_name: str, muted: bool, transition_id: str
    ) -> None: ...

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

    async def verify_same_graph_commit(self, request: TransitionRequest) -> Mapping[str, Any]: ...

    async def verify_transition_graph(self, request: TransitionRequest) -> Mapping[str, Any]: ...

    async def pause_source_after_failure(self, request: TransitionRequest) -> None: ...

    def target_source_staged(self, request: TransitionRequest) -> bool: ...

    async def abort_failed_transition(
        self,
        request: TransitionRequest,
        snapshot: Mapping[str, Any] | None,
        *,
        target_staged: bool,
    ) -> dict[str, Any] | None:
        """Decide the failed-handoff outcome.  None keeps the committed
        context unchanged; ``{"restore": <request>}`` asks the Coordinator to
        physically restore the committed source through its own stages;
        ``{"invalidate": True}`` reports a staged target that was already
        stopped and invalidated."""
        ...

    async def wait_for_pipewire_spotify_release(self) -> bool: ...

    async def publish_restored_source(self, request: TransitionRequest) -> None: ...

    async def normalize_queue_after_native_loss(self) -> None: ...

    async def verify_measurement_entry(
        self, request: TransitionRequest
    ) -> Mapping[str, Any]: ...

    async def verify_output_mode_runtime(
        self, request: TransitionRequest
    ) -> Mapping[str, Any]: ...

    async def commit_output_mode_runtime(
        self, request: TransitionRequest
    ) -> Mapping[str, Any]: ...

    async def commit_sample_rate_policy(
        self, request: TransitionRequest
    ) -> Mapping[str, Any]: ...

    async def finalize_output_mode_graph_after_gate_open(
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

    async def read_measurement_session_graph(
        self, target_rate: int
    ) -> Mapping[str, Any]: ...

    async def reconcile_measurement_session_graph(self, target_rate: int) -> None: ...

