# SPDX-License-Identifier: AGPL-3.0-only
"""Plan-derived output targets, activation recovery and effects reconciliation.

No application globals or locks are owned here. OutputService owns the head and
checkpoint; the supplied runtime and lock getters resolve the current owners.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Mapping

from fastapi import HTTPException

from audio.output_context import output_state_device
from audio.output_routing import device_key
from audio.output_service import OutputService
from audio.output_state_store import StateConflictError
from audio.system_volume import volume_percent_to_db
from dsp.runtime import DSPRuntimeConfig, PlannedSyncTarget

logger = logging.getLogger(__name__)


def build_plan_target(service, manager, plan, *, output_key: str, rate: int,
                      hardware_ports: list, fingerprint: str | None):
    """Render the resolved plan without rereading mutable extras or output state."""
    layout = service.compile_layout(plan)
    config = DSPRuntimeConfig.from_plan(
        plan, layout=layout, output_key=output_key, sample_rate_hz=rate,
        hardware_ports=hardware_ports, plan_fingerprint=fingerprint)
    text = manager.compile_engine_text(
        [dict(entry) for entry in layout], preset_name=plan["global"]["preset"], mode=plan["mode"],
        sample_rate_hz=rate, extras_override=plan["global"]["extras"])
    return PlannedSyncTarget(config=config, text=text)


@dataclass(frozen=True)
class OutputRenderDeps:
    get_service: Callable[[], OutputService]
    get_manager: Callable[[], Any]
    get_runtime: Callable[[], Any]
    get_overview: Callable[[], dict]
    get_status: Callable[[], dict]
    graph_write_lock: Callable[[], Any]
    mutation_lock: Callable[[], Any]
    volume_state_for_manager: Callable[..., Awaitable[Any]]


class OutputRenderer:
    def __init__(self, deps: OutputRenderDeps):
        self._deps = deps

    async def render_sync_target(self, rate: int, overview: dict):
        targets = await self.render_recovery_targets(rate, overview, (None,))
        return targets[0]

    async def render_recovery_targets(self, rate: int, overview: dict, extras_snapshots: tuple):
        """Render one production state for all targets, preserving editable drafts."""
        output_key = ""
        ports = []
        channels = None
        service = self._deps.get_service()
        try:
            output_key, channels = output_state_device(overview)
            ports = list((overview.get("output_mode") or {}).get("hardware_playback_ports") or [])
            if type(rate) is not int or rate <= 0:
                raise ValueError("Output sample rate is unknown")
            if not channels:
                raise ValueError("Output channel capacity is unknown")
            if len(ports) != channels:
                raise ValueError("Output playback ports are undiscovered or incomplete")
            manager = self._deps.get_manager()
            token = service.activation_token()
            # The extras are read for the output being rendered, not for the one applied now.
            device = device_key(output_key)
            base_extras = manager.load_global_extras(device_id=device)

            def validate_current():
                service.require_activation_token(token)
                if manager.load_global_extras(device_id=device) != base_extras:
                    raise StateConflictError("Effects extras changed while preparing the DSP graph")
            native = self._deps.get_runtime()
            if native is None:
                raise RuntimeError("Native DSP runtime is unavailable")

            def render(state):
                targets = []
                for extras in extras_snapshots:
                    plan = service.compile_plan(
                        state, output_key=output_key, channels=channels, sample_rate_hz=rate,
                        extras_override=base_extras if extras is None else extras)
                    targets.append(build_plan_target(
                        service, manager, plan, output_key=output_key, rate=rate,
                        hardware_ports=ports, fingerprint=service.fingerprint_plan(plan)))
                return tuple(targets)

            state, targets = await service.render_for_activation(
                render, preflight=native.preflight_rendered, output_key=output_key,
                channels=channels, hardware_ports=ports)
            validate_current()

            def with_checkpoint(target):
                def remember():
                    service.remember_applied(state, output_key=output_key, channels=channels,
                                             hardware_ports=ports, sample_rate_hz=rate,
                                             fingerprint=target.config.plan_fingerprint)
                return replace(target, on_applied=remember, validate_current=validate_current)
            return tuple(with_checkpoint(target) for target in targets)
        except StateConflictError:
            raise
        except Exception as exc:
            logger.error("V2 output render failed: %s", exc)
            native = self._deps.get_runtime()
            snapshot = native.snapshot() if native is not None else {}
            keep_running = service.applied_graph_matches(
                snapshot, output_key=output_key, channels=channels,
                hardware_ports=ports, sample_rate_hz=rate)
            config = snapshot.get("config") or {}
            if not keep_running and config.get("output_key") != output_key:
                # Rendering a new device has not touched the confirmed source graph.
                source_ports = config.get("hardware_ports") or []
                keep_running = service.applied_graph_matches(
                    snapshot, output_key=config.get("output_key"), channels=len(source_ports),
                    hardware_ports=source_ports, sample_rate_hz=config.get("sample_rate"))
            if native is not None and not keep_running:
                await native.fail_closed(str(exc), output_key=output_key)
            raise RuntimeError(f"V2 output unavailable: {exc}") from exc

    async def render_effects_targets(self, previous, candidate):
        overview = await asyncio.to_thread(self._deps.get_overview)
        status = await asyncio.to_thread(self._deps.get_status)
        rate = status.get("active_rate")
        if type(rate) is not int or rate <= 0:
            rate = status.get("force_rate")
        return await self.render_recovery_targets(rate, overview, (candidate, previous))

    async def temporary_effects_transition(self, previous, candidate):
        """Apply temporary SPL helpers without replacing v2 routing or filters."""
        async with self._deps.mutation_lock(), self._deps.graph_write_lock():
            new_target, old_target = await self.render_effects_targets(previous, candidate)
            await self._deps.get_runtime().guarded_rebuild_rendered(
                new_target, previous=old_target, guard_db=-18.0,
                apply_candidate=lambda: None, apply_previous=lambda: None,
                settle_seconds=self._deps.get_manager().LOUDNESS_STRENGTH_VOLUME_SETTLE_SECONDS)

    async def guarded_effects_transition(self, previous, candidate, persist_all_presets):
        """Apply extras through a guarded DSP rebuild without touching the master."""
        async with self._deps.graph_write_lock():
            return await self._guarded_effects_transition_locked(previous, candidate, persist_all_presets)

    async def _guarded_effects_transition_locked(self, previous, candidate, persist_all_presets):
        result_holder = {}
        new_target, old_target = await self.render_effects_targets(previous, candidate)
        manager = self._deps.get_manager()
        start = await self._deps.volume_state_for_manager(manager)
        if any("loud_comp_stereo" in target.text for target in (new_target, old_target)):
            guard_db = manager.loudness_transition_guard_db(previous, candidate)
        else:
            guard_db = min(0.0, start.dsp_guard_db, volume_percent_to_db(start.master_percent))
        settle = float(getattr(manager, "LOUDNESS_STRENGTH_VOLUME_SETTLE_SECONDS", 0.0) or 0.0)

        def persist_candidate():
            normalized = manager.save_global_extras(candidate)
            # The v2 graph is already applied; do not load or notify a legacy preset.
            result_holder["result"] = {
                "extras": normalized,
                "updated": (len([p for p in manager.list_presets()
                                 if p["name"] not in manager.EXCLUDED_GLOBAL_EXTRAS_PRESETS])
                            if persist_all_presets else 1),
                "skipped": sorted(manager.EXCLUDED_GLOBAL_EXTRAS_PRESETS),
            }

        await self._deps.get_runtime().guarded_rebuild_rendered(
            new_target, previous=old_target, guard_db=guard_db,
            apply_candidate=persist_candidate, apply_previous=lambda: manager.save_global_extras(previous),
            settle_seconds=settle)
        result = result_holder["result"]
        result["runtime_applied"] = True
        return result

    async def sync_head_after_bank_assign(self) -> dict:
        """Best-effort live sync; a failed assignment activation stays committed."""
        async with self._deps.graph_write_lock():
            return await self._sync_head_locked()

    async def _sync_head_locked(self) -> dict:
        try:
            overview = await asyncio.to_thread(self._deps.get_overview)
            try:
                output_key, channels = output_state_device(overview)
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, str) else "No audio output device"
                return {"live_applied": False, "live_reason": detail}
            status = await asyncio.to_thread(self._deps.get_status)
            rate = status.get("active_rate")
            if not isinstance(rate, int) or rate <= 0:
                rate = status.get("force_rate")
            if not isinstance(rate, int) or rate <= 0 or not channels:
                return {"live_applied": False, "live_reason": "rate-unknown"}
            if self._deps.get_runtime() is None:
                return {"live_applied": False, "live_reason": "dsp-runtime-unavailable"}
            service = self._deps.get_service()
            try:
                intended_fingerprint = service.fingerprint(
                    service.load_for_activation(), output_key=output_key,
                    channels=channels, sample_rate_hz=rate)
            except (FileNotFoundError, ValueError):
                return {"live_applied": False, "live_reason": "not-activatable"}
            target = await self.render_sync_target(rate, overview)
            if target.config.plan_fingerprint != intended_fingerprint:
                return {"live_applied": False, "live_reason": "not-activatable"}
            await self._deps.get_runtime().sync_rendered(target)
            return {"live_applied": True, "live_reason": None}
        except Exception as exc:
            logger.warning("Bank-assign live sync failed: %s", exc)
            return {"live_applied": False, "live_reason": "live-apply-failed"}

    async def sync_extras_runtime(self) -> None:
        """Strict extras reconciliation, also on the last applied recovery state."""
        async with self._deps.graph_write_lock():
            target, _ = await self.render_effects_targets(None, None)
            await self._deps.get_runtime().sync_rendered(target)

    async def sync_plan_runtime(self, target, *, reason: str = "output-state-transition") -> None:
        del reason
        if isinstance(target, Mapping):
            target = PlannedSyncTarget(config=target["config"], text=target["text"])
        if self._deps.get_runtime() is None:
            raise RuntimeError("Native DSP runtime is unavailable")
        await self._deps.get_runtime().sync_rendered(target)

    def refresh_transition(self, request) -> None:
        """Re-render coordinator targets after configuration and graph ownership."""
        v2 = request.output_state_transition
        service = self._deps.get_service()
        if service.load()["revision"] != v2["expected_revision"]:
            raise StateConflictError("Output state changed during transition prepare")
        manager = self._deps.get_manager()
        device = device_key(v2["output_key"])
        extras = manager.load_global_extras(device_id=device)
        token = service.activation_token()
        ports = list(v2["target"].config.hardware_ports)

        def validate_current():
            service.require_activation_token(token)
            if manager.load_global_extras(device_id=device) != extras:
                raise StateConflictError("Effects extras changed during transition prepare")

        for key, document in (("target", v2["candidate_state"]), ("previous_target", v2["previous_state"])):
            if key == "previous_target" and v2.get(key) is None:
                continue
            plan = service.compile_plan(document, output_key=v2["output_key"],
                                        channels=v2["channels"], sample_rate_hz=request.target_rate,
                                        extras_override=extras)
            target = build_plan_target(
                service, manager, plan, output_key=v2["output_key"], rate=request.target_rate,
                hardware_ports=ports, fingerprint=service.fingerprint_plan(plan))
            v2[key] = replace(target, validate_current=validate_current) if key == "target" else target
        v2["fingerprint"] = v2["target"].config.plan_fingerprint
