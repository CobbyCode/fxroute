# SPDX-License-Identifier: AGPL-3.0-only
"""Output edits and mode-switch recovery using the existing graph owners.

Lock order is unchanged: coordinator transitions acquire their own ownership;
fast edits take the measurement rate lock before the graph-write lock. Mode
recovery takes rate, DSP mutation and graph-write ownership in that order.
This owner holds dependencies only, never a second copy of output state.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from contextlib import nullcontext
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable

from fastapi import HTTPException

from audio import hdmi
from audio.input_output_mode import remember_mode, restore_mode
from audio.output_api import build_output_state_mutation, require_state_mode
from audio.output_context import output_state_device, output_state_topology
from audio.output_service import MeasurementActiveError, OutputService
from audio.output_state import effective_crossover_enabled, routing_for_device, seed_device_routing
from audio.output_topology import MODES
from audio.output_state_store import StateConflictError
from dsp.output_render import OutputRenderer, build_plan_target
from http_errors import bad_request
from playback.transition import PlaybackTransitionFailure, TransitionRequest

logger = logging.getLogger(__name__)


def _live_topology_key(state: dict, *, output_key: str, channels: int | None, rate: int | None) -> tuple:
    """Equal keys permit coefficient/bank edits without rebuilding the port graph."""
    mode = state.get("active_mode") if isinstance(state, dict) else None
    assignments = routing_for_device(state, mode, output_key, channels) if mode else []
    crossover = effective_crossover_enabled(state, mode, channels, output_key=output_key) if mode else False
    return (mode, crossover, tuple(assignments[:channels or 0]), channels, rate)


def _plan_transition_guard(old_layout, new_layout, previous_gain: float) -> float:
    current_peak = max((float(channel.get("gain_db", 0.0)) for channel in old_layout), default=0.0)
    target_peak = max((float(channel.get("gain_db", 0.0)) for channel in new_layout), default=0.0)
    positive_gain_delta = max(0.0, target_peak - current_peak)
    return min(0.0, float(previous_gain or 0.0) - max(1.0, positive_gain_delta + 1.0))


@dataclass(frozen=True)
class OutputApplyDeps:
    renderer: OutputRenderer
    get_service: Callable[[], OutputService]
    get_manager: Callable[[], Any]
    get_runtime: Callable[[], Any]
    get_session: Callable[[], Any]
    get_overview: Callable[[], dict]
    get_status: Callable[[], dict]
    graph_write_lock: Callable[[], Any]
    mutation_lock: Callable[[], Any]
    mode_switch_guard: Callable[[], Any]
    get_carrier: Callable[[], tuple[bool, int | None]]
    set_carrier: Callable[..., Awaitable[None]]
    carrier_request: Callable[..., Awaitable[tuple[bool, int | None]]]
    disable_external_input: Callable[[], Awaitable[None]]
    sync_external_input: Callable[[], Awaitable[None]]
    resync_stdin: Callable[[], Awaitable[None]]
    mark_ingress_pending: Callable[[], Awaitable[None]]
    mark_policy_pending: Callable[[], Awaitable[None]]
    current_playback_context: Callable[[], Awaitable[dict]]
    run_transition: Callable[[TransitionRequest], Awaitable[Any]]
    transition_error_http: Callable[[PlaybackTransitionFailure], HTTPException]
    source_selection: Callable[[], dict]
    validate_level_result: Callable[[dict, dict], None]
    mark_level_applied: Callable[[dict], None]
    apply_hdmi_candidate: Callable[..., Awaitable[dict | None]] | None = None


class OutputApplication:
    def __init__(self, deps: OutputApplyDeps):
        self._deps = deps

    def _mutation(self, mutation, *, output_key, channels):
        return build_output_state_mutation(
            mutation, output_key=output_key, channels=channels,
            get_service=self._deps.get_service, validate_level_result=self._deps.validate_level_result)

    async def apply(self, body: dict, *, previous_input: dict | None = None,
                    sync_ingress: bool = True, input_modes_override: dict | None = None):
        mutation = body.get("mutation") if isinstance(body, dict) else None
        if not isinstance(mutation, dict) or mutation.get("kind") != "switch_mode":
            return await self.apply_candidate(body, previous_input=previous_input)
        mode = require_state_mode(mutation.get("mode"))
        selection = self._deps.source_selection()
        external = selection.get("mode") == "external-input"
        previous_owned, previous_clock_card = self._deps.get_carrier()
        previous_state = self._deps.get_service().load()
        self._mutation(mutation, output_key="", channels=None)(previous_state)
        if mode == previous_state["active_mode"]:
            return await self.apply_candidate(body, previous_input=previous_input,
                                              input_modes_override=input_modes_override)
        session = self._deps.get_session()
        if (type(body.get("expected_revision")) is not int
                or body["expected_revision"] != previous_state["revision"]
                or (session is not None and session.has_active_jobs)):
            return await self.apply_candidate(body, previous_input=previous_input)
        async with self._deps.mode_switch_guard():
            try:
                await self._deps.disable_external_input()
                await self._deps.set_carrier(*await self._deps.carrier_request(None, external=external, mode=mode))
                result = await self.apply_candidate(body, previous_input=previous_input,
                                                    input_modes_override=input_modes_override)
                if sync_ingress:
                    await self._deps.sync_external_input()
            except BaseException:
                # Reconcile the surviving head, never rewind a newer editor.
                recovery_failed = False
                recovered_mode = None
                measurement_owned = False
                service = self._deps.get_service()
                session = self._deps.get_session()
                rate_lock = session.lock if session is not None else nullcontext()
                try:
                    async with rate_lock:
                        session = self._deps.get_session()
                        measurement_owned = (session is not None and (
                            session.has_active_jobs or getattr(session, "entry_in_progress", False)))
                        if not measurement_owned:
                            async with self._deps.mutation_lock(), self._deps.graph_write_lock():
                                try:
                                    head = service.load()
                                    if head["active_mode"] == mode and head["revision"] == previous_state["revision"] + 1:
                                        service.revert(previous_state, expected_revision=head["revision"])
                                except StateConflictError:
                                    logger.warning("Output-mode rollback superseded by a newer edit")
                                except Exception:
                                    logger.exception("Output-mode state rollback failed; reconciling the surviving head")
                                overview = await asyncio.to_thread(self._deps.get_overview)
                                status = await asyncio.to_thread(self._deps.get_status)
                                rate = status.get("active_rate") or status.get("force_rate")
                                target = await self._deps.renderer.render_sync_target(rate, overview)
                                await self._deps.get_runtime().sync_rendered(target)
                                recovered_mode = target.config.output_mode
                except BaseException:
                    recovery_failed = True
                    logger.exception("Output-mode graph recovery failed")
                if measurement_owned:
                    # The newer owner also owns carrier/ingress restoration.
                    await self._deps.mark_ingress_pending()
                    await self._deps.mark_policy_pending()
                    raise
                try:
                    surviving_mode = recovered_mode or service.load()["active_mode"]
                    carrier = ((previous_owned, previous_clock_card)
                               if surviving_mode == previous_state["active_mode"] else
                               await self._deps.carrier_request(None, external=external, mode=surviving_mode))
                    await self._deps.set_carrier(*carrier)
                except BaseException:
                    recovery_failed = True
                    logger.exception("Output-mode carrier recovery failed")
                if sync_ingress:
                    try:
                        await self._deps.sync_external_input()
                    except BaseException:
                        recovery_failed = True
                        logger.exception("Output-mode ingress recovery failed")
                try:
                    await self._deps.resync_stdin()
                except BaseException:
                    recovery_failed = True
                    logger.exception("Output-mode STDIN recovery failed")
                if recovery_failed and self._deps.get_runtime() is not None:
                    try:
                        async with self._deps.graph_write_lock():
                            await self._deps.get_runtime().fail_closed("Output-mode recovery failed")
                    except BaseException:
                        logger.exception("Output-mode fail-closed cleanup failed")
                raise
            await self._deps.resync_stdin()
            return result

    async def apply_candidate(self, body: dict, *, previous_input: dict | None = None,
                              input_modes_override: dict | None = None):
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail='Invalid JSON body, expected {"expected_revision": <int>, "mutation": {...}}')
        expected_revision = body.get("expected_revision")
        if type(expected_revision) is bool or not isinstance(expected_revision, int) or expected_revision < 0:
            raise HTTPException(status_code=400, detail="expected_revision must be a non-negative integer")
        session = self._deps.get_session()
        if session is not None and session.has_active_jobs:
            raise HTTPException(status_code=423, detail="Measurement is active; output state is locked")

        service = self._deps.get_service()
        try:
            state = service.load()
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=f"Output state is unavailable: {exc}")
        if state["revision"] != expected_revision:
            raise HTTPException(status_code=409, detail={
                "code": "revision-conflict", "message": "Output state changed; refresh before applying",
                "revision": state["revision"]})
        overview = await asyncio.to_thread(self._deps.get_overview)
        output_key, channels = output_state_device(overview)
        capacity = hdmi.routing_capacity(overview)
        mutate = self._mutation(body.get("mutation"), output_key=output_key,
                                channels=capacity or channels)
        try:
            base = (seed_device_routing(state, output_key, {
                mode: hdmi.default_assignments(hdmi.overview_routing_ports(overview, capacity), mode, hdmi=True)
                for mode in MODES}, capacity=capacity)
                if capacity is not None and capacity > 2 else state)
            candidate = await asyncio.to_thread(mutate, base)
            if (body.get("mutation") or {}).get("kind") == "switch_mode":
                selection = self._deps.source_selection()
                if input_modes_override is not None:
                    candidate["input_modes"] = dict(input_modes_override)
                elif previous_input is not None:
                    candidate["input_modes"] = restore_mode(state, previous_input, selection)["input_modes"]
                if input_modes_override is None:
                    candidate = remember_mode(candidate, selection)
        except ValueError as exc:
            raise bad_request(exc)

        if self._deps.apply_hdmi_candidate is not None:
            result = await self._deps.apply_hdmi_candidate(body, state, candidate, overview)
            if result is not None:
                return result

        status = await asyncio.to_thread(self._deps.get_status)
        target_rate = status.get("active_rate")
        if not isinstance(target_rate, int) or target_rate <= 0:
            target_rate = status.get("force_rate")
        live_known = isinstance(target_rate, int) and target_rate > 0 and bool(channels)
        old_plan = new_plan = None
        fingerprints: dict[str, str | None] = {"old": None, "new": None}
        if live_known:
            # Incomplete drafts persist, but cannot activate or supply a fingerprint.
            for key, document in (("old", state), ("new", candidate)):
                try:
                    plan = service.compile_plan(
                        document, output_key=output_key, channels=channels, sample_rate_hz=target_rate)
                    fingerprints[key] = service.fingerprint_plan(plan)
                except (FileNotFoundError, ValueError):
                    plan = None
                if key == "old":
                    old_plan = plan
                else:
                    new_plan = plan

        old_fp, new_fp = fingerprints["old"], fingerprints["new"]
        topology_changed = _live_topology_key(
            state, output_key=output_key, channels=channels, rate=target_rate) != \
            _live_topology_key(candidate, output_key=output_key, channels=channels, rate=target_rate)
        coordinator_path = live_known and new_plan is not None and topology_changed

        if coordinator_path:
            ports = (overview.get("output_mode") or {}).get("hardware_playback_ports") or []
            if not ports:
                raise HTTPException(status_code=500, detail="Planned output has no discovered playback ports")
            manager = self._deps.get_manager()
            try:
                new_target = build_plan_target(service, manager, new_plan, output_key=output_key,
                                               rate=target_rate, hardware_ports=list(ports), fingerprint=new_fp)
                old_target = None
                if old_plan is not None:
                    old_target = build_plan_target(service, manager, old_plan, output_key=output_key,
                                                   rate=target_rate, hardware_ports=list(ports), fingerprint=old_fp)
            except (RuntimeError, ValueError) as exc:
                raise HTTPException(status_code=500, detail=f"Planned output cannot stage: {exc}")
            live_overview = copy.deepcopy(overview)
            live_overview["output_mode"] = {
                **(live_overview.get("output_mode") or {}), "mode": new_plan["mode"],
                "planned_routes": [[signal, port] for signal, port in new_target.config.route_pairs],
                "output_state_revision": "pending",
            }
            if service.load()["revision"] != expected_revision:
                raise HTTPException(status_code=409, detail={
                    "code": "revision-conflict", "message": "Output state changed during transition prepare",
                    "revision": service.load()["revision"]})
            context = await self._deps.current_playback_context()
            try:
                await self._deps.run_transition(TransitionRequest(
                    operation="output-mode-switch", source=str(context.get("source") or "local"),
                    target_rate=target_rate, target_url=context.get("target_url"),
                    target_track=dict(context.get("target_track") or {}),
                    should_play=bool(context.get("should_play")), rate_change=False, reload_source=False,
                    detail="api-audio-output-state", output_mode_target=live_overview,
                    output_state_transition={
                        "candidate_state": candidate, "previous_state": state,
                        "expected_revision": expected_revision, "fingerprint": new_fp,
                        "target": new_target, "previous_target": old_target,
                        "output_key": output_key, "channels": channels,
                    }))
            except StateConflictError as exc:
                raise HTTPException(status_code=409, detail={
                    "code": "revision-conflict", "message": str(exc),
                    "revision": service.load()["revision"]}) from exc
            except PlaybackTransitionFailure as exc:
                cause = exc.__cause__
                while cause is not None:
                    if isinstance(cause, StateConflictError):
                        raise HTTPException(status_code=409, detail={
                            "code": "revision-conflict", "message": str(cause),
                            "revision": service.load()["revision"]}) from exc
                    if isinstance(cause, MeasurementActiveError):
                        raise HTTPException(status_code=423, detail=str(cause)) from exc
                    cause = cause.__cause__
                raise self._deps.transition_error_http(exc) from exc
            committed = service.load()
            return {
                "status": "ok", "revision": committed["revision"], "active_mode": committed["active_mode"],
                "fingerprint": new_fp, "fingerprint_changed": True, "live_applied": True, "live_reason": None,
                "topology": output_state_topology(committed, committed["active_mode"], output_key, channels),
            }

        session = self._deps.get_session()
        rate_lock = session.lock if session is not None else nullcontext()
        async with rate_lock, self._deps.graph_write_lock():
            return await self._apply_fast(
                body, service=service, state=state, candidate=candidate, overview=overview,
                output_key=output_key, channels=channels, target_rate=target_rate,
                live_known=live_known, old_plan=old_plan, new_plan=new_plan,
                old_fp=old_fp, new_fp=new_fp, expected_revision=expected_revision)

    async def _apply_fast(self, body, *, service, state, candidate, overview,
                          output_key, channels, target_rate, live_known,
                          old_plan, new_plan, old_fp, new_fp, expected_revision):
        """Render, commit and reconcile one edit under graph-write ownership."""
        current_overview = await asyncio.to_thread(self._deps.get_overview)
        current_status = await asyncio.to_thread(self._deps.get_status)
        current_rate = current_status.get("active_rate") or current_status.get("force_rate")
        try:
            current_device = output_state_device(current_overview)
        except HTTPException:
            current_device = None
        if (current_device != (output_key, channels) or current_rate != target_rate
                or (current_overview.get("output_mode") or {}).get("hardware_playback_ports")
                != (overview.get("output_mode") or {}).get("hardware_playback_ports")):
            raise HTTPException(status_code=409, detail={
                "code": "output-context-conflict", "message": "Output device or rate changed; retry the edit",
                "revision": service.load()["revision"]})
        rendered_extras = None
        # Stage before commit: unrenderable candidates must not poison the head.
        staged_new_target = staged_old_target = None
        staged_guard = 0.0
        if (live_known and new_plan is not None and self._deps.get_runtime() is not None
                and (overview.get("output_mode") or {}).get("hardware_playback_ports")):
            manager = self._deps.get_manager()
            rendered_extras = manager.load_global_extras()
            # Refresh both identities under graph ownership after any extras writer.
            for key, document in (("old", state), ("new", candidate)):
                try:
                    plan = service.compile_plan(
                        document, output_key=output_key, channels=channels,
                        sample_rate_hz=target_rate, extras_override=rendered_extras)
                    fingerprint = service.fingerprint_plan(plan)
                except (FileNotFoundError, ValueError):
                    plan, fingerprint = None, None
                if key == "old":
                    old_plan, old_fp = plan, fingerprint
                else:
                    new_plan, new_fp = plan, fingerprint
        if (live_known and new_plan is not None and old_fp != new_fp
                and self._deps.get_runtime() is not None
                and (overview.get("output_mode") or {}).get("hardware_playback_ports")):
            ports = list((overview.get("output_mode") or {}).get("hardware_playback_ports") or [])
            try:
                staged_new_target = build_plan_target(
                    service, manager, new_plan, output_key=output_key,
                    rate=target_rate, hardware_ports=ports, fingerprint=new_fp)
                if old_plan is not None:
                    staged_old_target = build_plan_target(
                        service, manager, old_plan, output_key=output_key,
                        rate=target_rate, hardware_ports=ports, fingerprint=old_fp)
            except (RuntimeError, ValueError) as exc:
                logger.warning("Output-state staging failed before commit (head unchanged): %s", exc)
                raise HTTPException(status_code=500, detail=f"Planned output cannot stage: {exc}")
            if staged_old_target is not None:
                previous_gain = float((self._deps.get_runtime().snapshot() or {}).get("output_gain_db") or 0.0)
                staged_guard = _plan_transition_guard(
                    staged_old_target.config.layout, staged_new_target.config.layout, previous_gain)

        try:
            mutation = body.get("mutation") or {}
            if mutation.get("kind") == "set_level_calibration":
                await asyncio.to_thread(self._deps.validate_level_result, mutation, service.load())
            committed = service.commit_unowned(candidate, expected_revision=expected_revision)
            if mutation.get("kind") == "set_level_calibration":
                self._deps.mark_level_applied(mutation)
        except StateConflictError as exc:
            raise HTTPException(status_code=409, detail={
                "code": "revision-conflict", "message": str(exc),
                "revision": service.load()["revision"]}) from exc
        except MeasurementActiveError as exc:
            raise HTTPException(status_code=423, detail=str(exc)) from exc
        except ValueError as exc:
            raise bad_request(exc)

        token = service.activation_token()

        def require_revision(expected_revision, expected_token):
            if service.load()["revision"] != expected_revision:
                raise StateConflictError("Output state changed during output-state activation")
            service.require_activation_token(expected_token)
            if rendered_extras is not None and manager.load_global_extras() != rendered_extras:
                raise StateConflictError("Effects extras changed during output-state activation")

        def require_current():
            require_revision(committed["revision"], token)

        if staged_new_target is not None:
            staged_new_target = replace(staged_new_target, validate_current=require_current)
        if staged_old_target is not None:
            staged_old_target = replace(staged_old_target, validate_current=require_current)

        def draft_response(reason: str) -> dict:
            return {
                "status": "ok", "revision": committed["revision"], "active_mode": committed["active_mode"],
                "fingerprint": new_fp,
                "fingerprint_changed": None if old_fp is None or new_fp is None else old_fp != new_fp,
                "live_applied": False, "live_reason": reason,
                "topology": output_state_topology(committed, committed["active_mode"], output_key, channels),
            }

        if not live_known:
            return draft_response("rate-unknown" if not target_rate else "output-capacity-unknown")
        if new_plan is None:
            return draft_response("not-activatable")
        if staged_new_target is None:
            if old_fp is not None and old_fp == new_fp:
                return draft_response("nothing-to-apply")
            if self._deps.get_runtime() is None:
                return draft_response("dsp-runtime-unavailable")
            return draft_response("output-ports-undiscovered")
        if staged_old_target is None:
            # First activatable head: there is no previous valid graph to guard.
            try:
                await self._deps.get_runtime().sync_rendered(staged_new_target)
                service.remember_applied(committed, output_key=output_key, channels=channels,
                                         hardware_ports=ports, sample_rate_hz=target_rate, fingerprint=new_fp)
            except BaseException as exc:
                try:
                    require_current()
                    service.revert(state, expected_revision=committed["revision"])
                except BaseException:
                    logger.exception("Output-state activation rollback failed")
                logger.warning("Output-state activation failed after commit: %s", exc)
                raise HTTPException(status_code=500, detail={
                    "code": "live-apply-failed", "message": str(exc),
                    "revision": service.load()["revision"], "rollback": "committed"}) from exc
            return {
                "status": "ok", "revision": committed["revision"], "active_mode": committed["active_mode"],
                "fingerprint": new_fp, "fingerprint_changed": True, "live_applied": True, "live_reason": None,
                "topology": output_state_topology(committed, committed["active_mode"], output_key, channels),
            }
        try:
            await self._deps.get_runtime().guarded_rebuild_rendered(
                staged_new_target, previous=staged_old_target, guard_db=staged_guard,
                apply_candidate=require_current, apply_previous=require_current, settle_seconds=0.0)
            service.remember_applied(committed, output_key=output_key, channels=channels,
                                     hardware_ports=ports, sample_rate_hz=target_rate, fingerprint=new_fp)
        except BaseException as exc:
            rolled_back = False
            try:
                require_current()
                reverted = service.revert(state, expected_revision=committed["revision"])
                rolled_back = True
                reverted_token = service.activation_token()
                rollback_target = replace(staged_old_target, validate_current=lambda: require_revision(
                    reverted["revision"], reverted_token))
                await self._deps.get_runtime().sync_rendered(rollback_target, initial_output_gain_db=staged_guard)
            except BaseException:
                logger.exception("Output-state fast-path rollback failed")
            logger.warning("Output-state live apply failed (rollback=%s): %s",
                           "committed" if rolled_back else "conflicted", exc)
            raise HTTPException(status_code=500, detail={
                "code": "live-apply-failed", "message": str(exc),
                "revision": service.load()["revision"] if rolled_back else committed["revision"],
                "rollback": "committed" if rolled_back else "conflicted"}) from exc
        return {
            "status": "ok", "revision": committed["revision"], "active_mode": committed["active_mode"],
            "fingerprint": new_fp, "fingerprint_changed": True, "live_applied": True, "live_reason": None,
            "topology": output_state_topology(committed, committed["active_mode"], output_key, channels),
        }
