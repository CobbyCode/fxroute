"""Test adapter for exercising the production handoff core through the coordinator.

The production runtime adapter owns the same boundary.  The test adapter keeps
the PipeWire/MPV operations mocked while making the coordinator's gate,
ordering, and commit contract part of every shared-handoff test.
"""

from __future__ import annotations

from typing import Any

import main
from playback_transition import PlaybackTransitionCoordinator, TransitionRequest


class MainCoreTransitionRuntime:
    """Coordinator runtime whose graph stage delegates to main's mocked core."""

    def __init__(
        self,
        *,
        target_rate: int | None,
        generation: int,
        source: str = "local",
        target_url: str = "/music/target.flac",
        operation: str = "play",
        detail: str = "test-handoff",
        ee_port_timeout_ms: int | None = None,
        preserve_easyeffects_output_graph: bool = False,
        live_rate: int | None = None,
        failure: BaseException | None = None,
        resolver: Any = None,
        use_core: bool = True,
        events: list[str] | None = None,
    ) -> None:
        self.target_rate = target_rate
        self.generation = generation
        self.source = source
        self.target_url = target_url
        self.operation = operation
        self.detail = detail
        self.ee_port_timeout_ms = ee_port_timeout_ms
        self.preserve_easyeffects_output_graph = preserve_easyeffects_output_graph
        self.live_rate = live_rate
        self.failure = failure
        self.resolver = resolver
        self.use_core = use_core
        self.events = events if events is not None else []
        self.muted = False
        try:
            self.previous_force_rate = main._get_current_pipewire_force_rate()
        except Exception:
            self.previous_force_rate = None

    async def read_hardware_mute(self) -> bool:
        self.events.append("gate.read")
        return self.muted

    async def set_hardware_mute(self, muted: bool, transition_id: str) -> None:
        self.muted = bool(muted)
        self.events.append(f"gate.set:{self.muted}")

    async def read_transition_snapshot(self, request: Any) -> dict[str, Any]:
        self.events.append("snapshot")
        try:
            self.previous_force_rate = main._get_current_pipewire_force_rate()
        except Exception:
            self.previous_force_rate = None
        try:
            status = dict(main.get_samplerate_status())
        except Exception:
            status = {}
        return {
            "active_rate": status.get("active_rate"),
            "force_rate": status.get("force_rate"),
        }

    async def quiet_old_source(self, request: Any) -> None:
        self.events.append("quiet")

    async def resolve_target_rate(self, request: Any) -> int | None:
        self.events.append("resolve-rate")
        if self.resolver is not None:
            return await self.resolver(request)
        return self.live_rate if self.live_rate is not None else request.target_rate

    async def establish_target_rate(self, request: Any) -> None:
        self.events.append("rate")
        if request.target_rate is None:
            return
        if self.generation != main.playback_transition_generation:
            raise RuntimeError("stale transition generation")
        try:
            status = main.get_samplerate_status()
        except Exception:
            status = {}
        if (
            status.get("active_rate") == request.target_rate
            and status.get("force_rate") in {None, 0, request.target_rate}
        ):
            return
        aligned = await main._ensure_playback_samplerate_force(
            request.target_rate,
            self.detail,
            policy=main.samplerate_orchestration.RADIO_POLICY,
        )
        if not aligned:
            raise RuntimeError("test target-rate alignment failed")

    async def establish_effects_and_helper(self, request: Any) -> None:
        self.events.append("effects-helper-links")
        if self.failure is not None:
            raise self.failure
        if request.target_rate is None:
            return
        if not self.use_core:
            return
        await main._coordinator_establish_effects_and_helper(
            request,
            previous_force_rate=self.previous_force_rate,
            ee_port_timeout_ms=(
                self.ee_port_timeout_ms
                if self.ee_port_timeout_ms is not None
                else main.PLAYBACK_HANDOFF_EE_PORT_TIMEOUT_MS
            ),
        )

    async def prepare_target_source(self, request: Any) -> None:
        self.events.append("prepare")

    async def start_target_source(self, request: Any) -> None:
        self.events.append("start")

    async def stabilize_effects_after_rate_change(self, request: Any) -> dict[str, Any]:
        self.events.append("dsp-stabilize")
        return {
            "stabilized": True,
            "no_op": not request.rate_change,
            "active_rate": request.target_rate,
            "force_rate": request.target_rate,
            "graph_complete": True,
        }

    async def set_source_volume(self, volume: int, transition_id: str) -> None:
        self.events.append(f"source-volume:{volume}")

    async def verify_committed_transition(self, request: Any) -> dict[str, Any]:
        self.events.append("commit-readback")
        return {"committed": True, "active_rate": request.target_rate}

    async def verify_transition_graph(self, request: Any) -> dict[str, Any]:
        """Model the staged graph readback before source-volume restore."""
        self.events.append("graph-readback")
        return {"committed": True, "active_rate": request.target_rate}

    async def pause_source_after_failure(self, request: Any) -> None:
        self.events.append("pause-after-failure")


async def run_main_handoff_through_coordinator(
    *,
    target_rate: int | None,
    generation: int,
    source: str = "local",
    target_url: str = "/music/target.flac",
    operation: str = "play",
    detail: str = "test-handoff",
    ee_port_timeout_ms: int | None = None,
    preserve_easyeffects_output_graph: bool = False,
    live_rate: int | None = None,
    failure: BaseException | None = None,
    resolver: Any = None,
    use_core: bool = True,
    rate_change: bool | None = None,
    events: list[str] | None = None,
):
    """Run the shared production handoff core behind a coordinator instance."""
    runtime = MainCoreTransitionRuntime(
        target_rate=target_rate,
        generation=generation,
        source=source,
        target_url=target_url,
        operation=operation,
        detail=detail,
        ee_port_timeout_ms=ee_port_timeout_ms,
        preserve_easyeffects_output_graph=preserve_easyeffects_output_graph,
        live_rate=live_rate,
        failure=failure,
        resolver=resolver,
        use_core=use_core,
        events=events,
    )
    if rate_change is None:
        try:
            status = main.get_samplerate_status()
        except Exception:
            status = {}
        rate_change = not (
            status.get("active_rate") == target_rate
            and status.get("force_rate") in {None, 0, target_rate}
        )
    request = TransitionRequest(
        operation=operation,
        source=source,
        target_rate=target_rate,
        target_url=target_url,
        target_track={"source": source, "url": target_url},
        should_play=True,
        rate_change=rate_change,
        reload_source=True,
        detail=detail,
    )
    coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
    result = await coordinator.execute(request)
    return result, runtime
