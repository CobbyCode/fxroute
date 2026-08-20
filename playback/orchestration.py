# SPDX-License-Identifier: AGPL-3.0-only
"""Playback transition and production-graph orchestration.

This module deliberately does not import ``main``.  Runtime state and the
low-level PipeWire/DSP operations are supplied through late-bound callbacks so
the application can keep its existing patch points while this module owns the
transition policy.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Mapping

import playback.queue
import playback.source_policy as source_policy
import playback.state
import audio.samplerate as samplerate
from playback.transition import PlaybackTransitionFailure, TransitionRequest, stable_graph_readbacks

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlaybackOrchestrationDeps:
    """Late-bound application services used by :class:`PlaybackOrchestrator`."""

    get_coordinator: Callable[[], Any]
    set_coordinator: Callable[[Any], None]
    make_transition_coordinator: Callable[[], Any]
    begin_transition_attempt: Callable[[], int]
    end_transition_attempt: Callable[[], None]
    run_transition: Callable[[TransitionRequest], Awaitable[Any]]
    get_playback_state: Callable[[], Any]
    get_runtime_player: Callable[[], Any]
    get_dsp_runtime: Callable[[], Any]
    get_dsp_manager: Callable[[], Any]
    get_dsp_preset_load_lock: Callable[[], Any]
    get_measurement_session: Callable[[], Any]
    get_samplerate_status: Callable[[], Mapping[str, Any]]
    get_audio_output_overview: Callable[[], dict]
    get_spotify_ui_state: Callable[..., Awaitable[dict]]
    get_player_audio_samplerate: Callable[[], int | None]
    is_local_playback_active: Callable[[dict | None], bool]
    is_spotify_playback_active: Callable[[dict | None], bool]
    spotify_target_track: Callable[[Mapping[str, Any]], dict[str, Any]]
    sample_rate_policy_is_auto: Callable[[], bool]
    get_player_queue_fields: Callable[[], dict]
    run_pw_link_command: Callable[..., Awaitable[str]]
    connect_ports: Callable[..., Awaitable[None]]
    contains_link: Callable[[str, str, str], bool]
    helper_argument_sample_rate: Callable[[dict | None], int | None]
    sync_preset_for_samplerate: Callable[..., Awaitable[Any]]
    sync_runtime: Callable[..., Awaitable[Any]]
    reconcile_sink_rate: Callable[..., Awaitable[bool]]
    load_dsp_preset: Callable[..., Awaitable[Any]]
    sleep: Callable[[float], Awaitable[Any]]
    pipewire_poll_interval_ms: int
    dsp_port_timeout_ms: int
    post_start_readbacks: int
    output_mode_subwoofer_modes: frozenset[str]
    output_mode_stereo: str
    mpv_source_ports_present: Callable[[], Awaitable[bool]] | None = None
    mpv_link_repair_timeout_ms: int = 1500
    source_port_readiness_timeout_ms: int = 4500
    get_dsp_snapshot: Callable[[], Mapping[str, Any]] | None = None
    repair_stereo_output_links: Callable[[dict], Awaitable[None]] | None = None
    # Resolves the concrete producer ports for a source at verification time
    # (after the source has started).  For Spotify this must derive the ports
    # from the live sink-input identity, never a static desktop fallback.
    resolve_source_producer_ports: Callable[[str], tuple[str, str] | None] | None = None


class PlaybackOrchestrator:
    """Own playback transition policy and graph recovery decisions."""

    def __init__(self, deps: PlaybackOrchestrationDeps):
        self._deps = deps

    def coordinator_source_rate(self, source: str, track: Mapping[str, Any] | None = None) -> int | None:
        track = track or {}
        if source == "spotify":
            return 44100
        if source == "radio":
            return int(track.get("sample_rate_hz") or 44100)
        value = track.get("sample_rate_hz")
        return int(value) if isinstance(value, int) and value > 0 else None

    def coordinator_target_rate(self, source: str, track: Mapping[str, Any] | None = None) -> int | None:
        return samplerate.effective_playback_rate(self.coordinator_source_rate(source, track))

    def sample_rate_policy_is_auto(self) -> bool:
        return samplerate.load_sample_rate_policy().get("mode") == "auto"

    def _qobuz_playback_active(self, qobuz: Mapping[str, Any]) -> bool:
        return bool(qobuz.get("available") and qobuz.get("status") == "Playing")

    def _qobuz_target_track(self, qobuz: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "source": "qobuz",
            "id": qobuz.get("trackId") or qobuz.get("id"),
            "url": qobuz.get("trackId") or qobuz.get("id"),
            "title": qobuz.get("title"),
            "artist": qobuz.get("artist"),
            "album": qobuz.get("album"),
            "artUrl": qobuz.get("artUrl"),
            "sample_rate_hz": qobuz.get("sample_rate"),
        }

    async def current_playback_context(self) -> dict[str, Any]:
        state = self._deps.get_playback_state()
        player = self._deps.get_runtime_player()
        local_state = dict(player.state if player else {})
        local_track = dict(state.current_track_info or {})
        spotify = await self._deps.get_spotify_ui_state()
        local_active = self._deps.is_local_playback_active(local_state)
        spotify_active = self._deps.is_spotify_playback_active(spotify)
        if local_active and source_policy.is_mpv_source(local_track.get("source")):
            return {"source": local_track.get("source"), "target_url": local_state.get("current_file"),
                    "target_track": local_track, "should_play": True, "spotify": spotify}
        if spotify_active:
            track_id = spotify.get("trackId") or spotify.get("url")
            return {"source": "spotify", "target_url": str(track_id or "") or None,
                    "target_track": self._deps.spotify_target_track(spotify), "should_play": True, "spotify": spotify}
        qobuz = dict(state.latest_qobuz_state or {})
        if self._qobuz_playback_active(qobuz):
            track_id = qobuz.get("trackId") or qobuz.get("id")
            return {"source": "qobuz", "target_url": str(track_id or "") or None,
                    "target_track": self._qobuz_target_track(qobuz), "should_play": True, "spotify": spotify}
        if source_policy.is_mpv_source(local_track.get("source")) and local_state.get("current_file"):
            return {"source": local_track.get("source"), "target_url": local_state.get("current_file"),
                    "target_track": local_track,
                    "should_play": bool(local_state.get("playing") and not local_state.get("paused")),
                    "spotify": spotify}
        owner = state.current_playback_owner
        if owner == "spotify" and spotify.get("trackId"):
            return {"source": "spotify", "target_url": str(spotify["trackId"]),
                    "target_track": self._deps.spotify_target_track(spotify), "should_play": False, "spotify": spotify}
        if owner == "qobuz" and qobuz.get("trackId"):
            return {"source": "qobuz", "target_url": str(qobuz["trackId"]),
                    "target_track": self._qobuz_target_track(qobuz), "should_play": False, "spotify": spotify}
        return {"source": "local", "target_url": None, "target_track": {}, "should_play": False, "spotify": spotify}

    def coordinator_rate_change(self, target_rate: int | None) -> bool:
        if not isinstance(target_rate, int) or target_rate <= 0:
            return False
        try:
            status = self._deps.get_samplerate_status()
        except Exception:
            return True
        return not samplerate.playback_rate_aligned(status, target_rate)

    def transition_is_active(self) -> bool:
        coordinator = self._deps.get_coordinator()
        return bool(coordinator is not None and coordinator.transition_active)

    def coordinator_commit_context_id(self) -> str | None:
        """Return the Coordinator's latest committed transition identity.

        The Coordinator is the single owner of the commit token (it advances
        on every committed operation).  Watcher recovery requests read it
        directly instead of caching a mirror in the playback state.
        """
        return getattr(self._deps.get_coordinator(), "last_successful_commit_id", None)

    def measurement_audio_graph_owned(self) -> bool:
        session = self._deps.get_measurement_session()
        return bool(session is not None and getattr(session, "owns_audio_graph", False))

    def playback_transition_context_is_current(self, generation: int | None) -> bool:
        return self._deps.get_playback_state().transition_context_is_current(generation)

    async def run_coordinated_transition(self, request: TransitionRequest):
        coordinator = self._deps.get_coordinator()
        if coordinator is None:
            coordinator = self._deps.make_transition_coordinator()
            self._deps.set_coordinator(coordinator)
        epoch = self._deps.begin_transition_attempt()
        request = replace(request, attempt_epoch=epoch)
        try:
            return await coordinator.execute(request)
        finally:
            self._deps.end_transition_attempt()

    async def recovery_context_is_valid(self, request: TransitionRequest) -> bool:
        expected_context = request.recovery_commit_context_id
        expected_source = request.recovery_source or request.source
        expected_url = request.recovery_url or request.target_url
        if not expected_context or expected_source != request.source or not expected_url:
            return False
        coordinator = self._deps.get_coordinator()
        validator = getattr(coordinator, "recovery_context_is_current", None)
        if callable(validator) and not validator(expected_context):
            gate = getattr(coordinator, "gate", None)
            latch_reentry = bool(
                request.detail == "subwoofer-link-watcher"
                and getattr(coordinator, "last_successful_commit_id", None) == expected_context
                and getattr(gate, "failure_latched", False)
                and not getattr(coordinator, "transition_active", False)
            )
            if not latch_reentry:
                return False
        elif not callable(validator) and (
            self.coordinator_commit_context_id() != expected_context or self.transition_is_active()
        ):
            return False
        runtime = self._deps.get_dsp_runtime()
        if runtime is not None and runtime.sync_in_progress:
            return False
        if expected_source == "spotify":
            try:
                state = await self._deps.get_spotify_ui_state()
            except Exception:
                return False
            return state.get("status") == "Playing" and str(state.get("trackId") or state.get("url") or "") == str(expected_url)
        player = self._deps.get_runtime_player()
        state = dict(player.state if player else {})
        if state.get("current_file") != expected_url or state.get("ended"):
            return False
        track = self._deps.get_playback_state().current_track_info or {}
        return not track or (track.get("source") == expected_source and track.get("url") == expected_url)

    async def request_coordinated_recovery(self, track: Mapping[str, Any], reason: str, *, reload_source: bool = False,
                                           graph_only: bool = False, diagnosis: Mapping[str, Any] | None = None) -> None:
        if self.measurement_audio_graph_owned() or self._deps.get_coordinator() is None or not track:
            return
        source = str(track.get("source") or "")
        if not source_policy.is_known_source(source):
            return
        target_rate = self.coordinator_target_rate(source, track)
        if not isinstance(target_rate, int) or target_rate <= 0:
            return
        if source == "spotify":
            try:
                should_play = (await self._deps.get_spotify_ui_state()).get("status") == "Playing"
            except Exception:
                should_play = False
        elif source == "qobuz":
            qobuz = dict(self._deps.get_playback_state().latest_qobuz_state or {})
            should_play = bool(qobuz.get("available") and qobuz.get("status") == "Playing")
        else:
            player = self._deps.get_runtime_player()
            state = dict(player.state if player else {})
            should_play = bool(state.get("current_file") and state.get("playing") and not state.get("paused") and not state.get("ended"))
        rate_change = False if graph_only else self.coordinator_rate_change(target_rate)
        observed_url = str(track.get("url") or track.get("id") or "") or None
        commit_context = self.coordinator_commit_context_id()
        if not commit_context:
            return
        recovery_track = dict(track)
        if source == "spotify" and observed_url:
            recovery_track.setdefault("url", observed_url)
        request = TransitionRequest(
            operation="graph-reconcile" if graph_only else "recovery", source=source,
            target_rate=target_rate, target_url=observed_url, target_track=recovery_track,
            should_play=should_play, rate_change=rate_change, reload_source=False if graph_only else reload_source,
            graph_only=graph_only, detail=reason, recovery_commit_context_id=commit_context,
            recovery_source=source, recovery_url=observed_url,
            **(self._deps.get_player_queue_fields() if source == "local" else {}),
        )
        signature = json.dumps({"source": source, "url": observed_url, "target_rate": target_rate,
                                "should_play": should_play, "rate_change": rate_change,
                                "reload_source": request.reload_source, "graph_only": graph_only,
                                "graph": (diagnosis or {}).get("signature")}, sort_keys=True)

        async def validate() -> bool:
            return not self.measurement_audio_graph_owned() and await self.recovery_context_is_valid(request)

        async def execute():
            try:
                result = await self._deps.run_transition(request)
            except PlaybackTransitionFailure as exc:
                logger.warning("Coordinator recovery failed: %s", exc.as_status())
                return None
            if (
                getattr(result, "committed", False)
                and self._deps.sample_rate_policy_is_auto()
                and source_policy.is_mpv_source(source)
                and isinstance(getattr(result, "target_rate", None), int)
                and result.target_rate > 0
            ):
                track["sample_rate_hz"] = result.target_rate
                state = self._deps.get_playback_state()
                current = state.current_track_info
                if (
                    current
                    and current.get("source") == source
                    and current.get("url") == track.get("url")
                ):
                    current["sample_rate_hz"] = result.target_rate
            return result

        await self._deps.get_coordinator().run_recovery(
            signature=signature, commit_context_id=commit_context, validate=validate, execute=execute,
        )

    async def transition_sample_rate_policy(self, policy: Mapping[str, Any], *, detail: str) -> None:
        overview = self._deps.get_audio_output_overview()
        selected = overview.get("selected_output") or overview.get("current_output") or {}
        if policy.get("mode") == "fixed" and policy.get("rate") not in (selected.get("supported_rates") or []):
            raise ValueError("Selected output does not support this sample rate")
        context = await self.current_playback_context()
        source = str(context.get("source") or "local")
        source_rate = self.coordinator_source_rate(source, context.get("target_track"))
        if source_policy.is_mpv_source(source) and context.get("target_url"):
            live_rate = self._deps.get_player_audio_samplerate()
            if isinstance(live_rate, int) and live_rate > 0:
                source_rate = live_rate
        target_rate = samplerate.effective_playback_rate(source_rate, policy)
        if not isinstance(target_rate, int) or target_rate <= 0:
            status = self._deps.get_samplerate_status()
            target_rate = status.get("active_rate") or status.get("force_rate")
        if not isinstance(target_rate, int) or target_rate <= 0:
            raise RuntimeError("current hardware sample rate is unavailable")
        rate_change = self.coordinator_rate_change(target_rate)
        request = TransitionRequest(
            operation="sample-rate-policy", source=source, target_rate=target_rate,
            target_url=context.get("target_url"), target_track=dict(context.get("target_track") or {}),
            should_play=bool(context.get("should_play")), rate_change=rate_change,
            reload_source=bool(rate_change and source_policy.is_known_source(source) and context.get("target_url")),
            detail=detail, output_mode_target=dict(overview), sample_rate_policy=dict(policy),
            **(self._deps.get_player_queue_fields() if source == "local" else {}),
        )
        await self._deps.run_transition(request)

    async def _dsp_output_ports_present(self) -> bool:
        try:
            text = await self._deps.run_pw_link_command("-io")
        except Exception:
            return False
        return all(port in text for port in ("fxroute_dsp:input_1", "fxroute_dsp:input_2", "fxroute_dsp:output_1", "fxroute_dsp:output_2"))

    async def wait_for_dsp_output_ports(self, timeout_ms: int) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(timeout_ms, 0) / 1000
        while True:
            if await self._dsp_output_ports_present():
                return True
            if loop.time() >= deadline:
                return False
            await self._deps.sleep(self._deps.pipewire_poll_interval_ms / 1000)

    async def playback_graph_diagnosis(self, audio_overview: dict | None = None, *, source: str | None = None,
                                       target_rate: int | None = None, require_source: bool = False) -> dict:
        result = {"mode": None, "output_key": "", "ee_ports": False, "helper_ports": None,
                  "helper_active": None, "helper_rate": None, "helper_rate_matches": None,
                  "links": {}, "source_links": {}, "source_links_complete": None,
                  "direct_ee_to_hw_present": False, "direct_source_to_hw_present": False,
                  "links_complete": False, "bypass_only": False,
                  "port_identities": {"source": (), "source_target": (), "ee": (), "helper": (), "output": ()},
                  "signature": "unreadable"}
        try:
            overview = audio_overview or self._deps.get_audio_output_overview()
            output_mode = overview.get("output_mode") or {}
            mode = output_mode.get("mode")
            output_key = str(output_mode.get("effective_output_key") or "").strip()
            result.update(mode=mode, output_key=output_key)
            if not output_key:
                return result
            io_text = await self._deps.run_pw_link_command("-io")
            link_text = await self._deps.run_pw_link_command("-l")
        except Exception:
            return result
        source_ports = ()
        resolver = getattr(self._deps, "resolve_source_producer_ports", None)
        if callable(resolver):
            resolved = resolver(source)
            source_ports = resolved if resolved is not None else ()
        else:
            source_ports = source_policy.graph_port_names(source) or ()
        source_targets = ("fxroute_dsp_sink:playback_FL", "fxroute_dsp_sink:playback_FR")
        snapshot = dict(self._deps.get_dsp_snapshot() or {}) if self._deps.get_dsp_snapshot else {}
        output_count = 4 if mode in self._deps.output_mode_subwoofer_modes else 2
        channels = ("FL", "FR", "RL", "RR")[:output_count]
        dsp_ports = tuple(f"fxroute_dsp:output_{i + 1}" for i in range(output_count))
        ingress_sources = ("fxroute_dsp_sink:monitor_FL", "fxroute_dsp_sink:monitor_FR")
        ingress_targets = ("fxroute_dsp:input_1", "fxroute_dsp:input_2")
        result["ee_ports"] = all(port in io_text for port in (*ingress_targets, *dsp_ports))
        result["helper_ports"] = result["ee_ports"]
        result["helper_active"] = bool(snapshot.get("active"))
        result["helper_rate"] = self._deps.helper_argument_sample_rate(snapshot)
        result["helper_rate_matches"] = bool(result["helper_active"] and (target_rate is None or result["helper_rate"] == target_rate))
        result["source_links"] = {f"{p} -> {t}": self._deps.contains_link(link_text, p, t) for p, t in zip(source_ports, source_targets)}
        result["source_links_complete"] = all(result["source_links"].values()) if source_ports else (False if require_source else None)
        # A source that reaches the hardware directly (not via the DSP) is a
        # bypass. Check every modeled source node and both port naming schemes
        # (MPV/Spotify use ``output_<ch>``, the qbzd ALSA node uses
        # ``playback_<ch>``).
        bypass_ports: list[str] = []
        for node in dict.fromkeys(source_policy.GRAPH_NODE_BY_SOURCE.values()):
            for channel in ("FL", "FR", "RL", "RR"):
                bypass_ports.extend((f"{node}:output_{channel}", f"{node}:playback_{channel}"))
        result["direct_source_to_hw_present"] = any(
            self._deps.contains_link(link_text, port, f"{output_key}:playback_{channel}")
            for port in bypass_ports
            for channel in ("FL", "FR", "RL", "RR")
        )
        result["links"] = {
            **{f"{p} -> {t}": self._deps.contains_link(link_text, p, t) for p, t in zip(ingress_sources, ingress_targets)},
            **{f"{p} -> {output_key}:playback_{c}": self._deps.contains_link(link_text, p, f"{output_key}:playback_{c}") for p, c in zip(dsp_ports, channels)},
        }
        result["port_identities"] = {"source": tuple(p for p in source_ports if p in io_text), "source_target": tuple(p for p in source_targets if p in io_text),
                                      "ee": tuple(p for p in ingress_targets if p in io_text), "helper": tuple(p for p in dsp_ports if p in io_text),
                                      "output": tuple(f"{output_key}:playback_{c}" for c in channels if f"{output_key}:playback_{c}" in io_text)}
        native = bool(result["source_links_complete"] is not False and result["ee_ports"] and result["helper_rate_matches"] and all(result["links"].values()))
        result["bypass_only"] = bool(native and result["direct_source_to_hw_present"])
        result["links_complete"] = bool(native and not result["direct_source_to_hw_present"])
        result["signature"] = json.dumps(result, sort_keys=True, default=list)
        return result

    def missing_playback_graph_links(self, diagnosis: Mapping[str, Any], *, include_source: bool = False) -> list[str]:
        missing = [link for link, present in (diagnosis.get("links") or {}).items() if not present]
        if include_source:
            missing = [link for link, present in (diagnosis.get("source_links") or {}).items() if not present] + missing
        return missing

    def measurement_session_link_loss_is_repairable(self, diagnosis: Mapping[str, Any], *, target_rate: int) -> bool:
        if diagnosis.get("links_complete") or diagnosis.get("ee_ports") is not True or diagnosis.get("measurement_rate_aligned") is not True:
            return False
        output_key = str(diagnosis.get("output_key") or "").strip()
        if not output_key or diagnosis.get("mode") not in {self._deps.output_mode_stereo, *self._deps.output_mode_subwoofer_modes}:
            return False
        if diagnosis.get("helper_ports") is not True or diagnosis.get("helper_active") is not True or diagnosis.get("helper_rate_matches") is not True or diagnosis.get("helper_rate") != target_rate:
            return False
        missing = set(self.missing_playback_graph_links(diagnosis))
        channels = ("FL", "FR", "RL", "RR")[:4 if diagnosis.get("mode") in self._deps.output_mode_subwoofer_modes else 2]
        repairable = {"fxroute_dsp_sink:monitor_FL -> fxroute_dsp:input_1", "fxroute_dsp_sink:monitor_FR -> fxroute_dsp:input_2", *(f"fxroute_dsp:output_{i + 1} -> {output_key}:playback_{c}" for i, c in enumerate(channels))}
        return bool(missing) and missing.issubset(repairable)

    def log_playback_graph_diagnosis(self, diagnosis: dict, *, target_rate: int, reason: str, detail: str) -> None:
        logger.warning("Playback handoff graph incomplete: mode=%s output_key=%s target_rate=%s ee_ports=%s helper_ports=%s helper_active=%s helper_rate=%s direct_bypass=%s source_links=%s missing_links=%s reason=%s detail=%s", diagnosis.get("mode"), diagnosis.get("output_key"), target_rate, diagnosis.get("ee_ports"), diagnosis.get("helper_ports"), diagnosis.get("helper_active"), diagnosis.get("helper_rate"), diagnosis.get("direct_ee_to_hw_present"), diagnosis.get("source_links_complete"), self.missing_playback_graph_links(diagnosis), reason, detail)

    async def repair_stereo_output_links_once(self, diagnosis: dict) -> None:
        if self._deps.repair_stereo_output_links is not None:
            await self._deps.repair_stereo_output_links(diagnosis)
            return
        output_key = str(diagnosis.get("output_key") or "").strip()
        if not output_key:
            raise RuntimeError("Playback handoff repair failed: missing stereo output target")
        links = await self._deps.run_pw_link_command("-l")
        for source, target in (("fxroute_dsp:output_1", f"{output_key}:playback_FL"), ("fxroute_dsp:output_2", f"{output_key}:playback_FR")):
            if not self._deps.contains_link(links, source, target):
                await self._deps.connect_ports((source,), target)

    async def reconcile_subwoofer_links_only(self) -> None:
        runtime = self._deps.get_dsp_runtime()
        reconcile = getattr(runtime, "reclean_direct_dsp_links", None) if runtime is not None else None
        if not callable(reconcile):
            raise RuntimeError("subwoofer runtime has no link-only reconciliation")
        await reconcile()

    def post_start_graph_links_are_repairable(self, diagnosis: Mapping[str, Any], *, include_source: bool = False, require_source: bool = True) -> bool:
        if not diagnosis.get("output_key") or not diagnosis.get("ee_ports") or (require_source and diagnosis.get("source_links_complete") is not True and not include_source) or diagnosis.get("direct_ee_to_hw_present"):
            return False
        if diagnosis.get("helper_ports") is not True or diagnosis.get("helper_active") is not True or diagnosis.get("helper_rate_matches") is not True:
            return False
        identities = {str(port) for ports in (diagnosis.get("port_identities") or {}).values() if isinstance(ports, (tuple, list, set, frozenset)) for port in ports}
        for link in self.missing_playback_graph_links(diagnosis, include_source=include_source):
            try: source, target = link.split(" -> ", 1)
            except ValueError: return False
            if source not in identities or target not in identities: return False
        return True

    async def relink_missing_production_links(self, diagnosis: Mapping[str, Any], *, include_source: bool = False, require_source: bool = True) -> bool:
        missing = self.missing_playback_graph_links(diagnosis, include_source=include_source)
        if not missing: return False
        if not self.post_start_graph_links_are_repairable(diagnosis, include_source=include_source, require_source=require_source):
            raise RuntimeError("production graph was not link-only drift with stable current ports")
        for link in missing:
            source, target = link.split(" -> ", 1)
            await self._deps.connect_ports((source,), target)
        return True

    async def reconcile_post_start_graph(self, request: TransitionRequest) -> dict[str, Any]:
        target_rate = request.target_rate
        if not isinstance(target_rate, int) or target_rate <= 0:
            return {"graph_complete": True, "post_start_graph_reconciled": False, "post_start_graph_links_relinked": False}
        overview = (
            copy.deepcopy(request.output_mode_target)
            if request.operation == "output-mode-switch" and request.output_mode_target
            else (dict(request.audio_overview) if request.audio_overview else None)
        )
        graph_source = request.source if request.target_url or request.should_play else None
        include_source = graph_source is not None
        diagnosis = await self.playback_graph_diagnosis(overview, source=graph_source, target_rate=target_rate, require_source=include_source)
        if diagnosis.get("direct_source_to_hw_present"):
            await self.reconcile_subwoofer_links_only()
            diagnosis = await self.playback_graph_diagnosis(overview, source=graph_source, target_rate=target_rate, require_source=include_source)
        missing = self.missing_playback_graph_links(diagnosis, include_source=include_source)
        if not diagnosis.get("links_complete") and not missing:
            if diagnosis.get("bypass_only"):
                await self.reconcile_subwoofer_links_only()
            else:
                self.log_playback_graph_diagnosis(diagnosis, target_rate=target_rate, reason=f"post-start-{request.operation}", detail=request.detail)
                raise RuntimeError("post-start graph readback was incomplete without link-only drift")
        relinked = await self.relink_missing_production_links(diagnosis, include_source=include_source)
        readbacks, signatures, stable = await stable_graph_readbacks(
            lambda: self.playback_graph_diagnosis(overview, source=graph_source, target_rate=target_rate, require_source=include_source),
            count=self._deps.post_start_readbacks,
        )
        if not stable:
            final = readbacks[-1] if readbacks else diagnosis
            self.log_playback_graph_diagnosis(final, target_rate=target_rate, reason=f"post-start-{request.operation}", detail=request.detail)
            raise RuntimeError("post-start production graph did not reach two stable canonical readbacks")
        return {"graph_complete": True, "post_start_graph_reconciled": True, "post_start_graph_links_relinked": relinked, "graph_signature": signatures[-1]}

    async def establish_effects_and_helper(self, request: TransitionRequest, *, ee_port_timeout_ms: int | None = None) -> dict[str, Any]:
        target_rate = request.target_rate
        empty = {"dsp_reinitialized": False, "preset_reloaded": False, "helper_rebuilt": False, "links_reconciled": False}
        if not isinstance(target_rate, int) or target_rate <= 0:
            return empty
        timeout = self._deps.dsp_port_timeout_ms if ee_port_timeout_ms is None else ee_port_timeout_ms
        overview = (
            copy.deepcopy(request.output_mode_target)
            if request.output_mode_target
            else (dict(request.audio_overview) if request.audio_overview else self._deps.get_audio_output_overview())
        )
        mode = (overview.get("output_mode") or {}).get("mode")
        diagnosis = await self.playback_graph_diagnosis(overview, target_rate=target_rate)
        preset_reloaded = helper_rebuilt = links_reconciled = False
        manager = self._deps.get_dsp_manager()
        if request.graph_only:
            if not diagnosis.get("bypass_only"):
                raise RuntimeError(f"graph-only reconciliation requested for a non-bypass graph: signature={diagnosis.get('signature')}")
            await self.reconcile_subwoofer_links_only()
            links_reconciled = True
        else:
            needs_preset = not diagnosis.get("ee_ports")
            if request.operation == "output-mode-switch" and manager is not None:
                compare = manager.load_compare_state(); side = compare.get("activeSide") if compare.get("activeSide") in {"A", "B"} else None
                target = compare.get("presetA") if side == "A" else compare.get("presetB") if side == "B" else None
                if target and manager.get_active_preset() != target:
                    await self._deps.load_dsp_preset(target, convolver_sample_rate_hz=target_rate); needs_preset = preset_reloaded = True
            if needs_preset and not preset_reloaded:
                await self._deps.sync_preset_for_samplerate(
                    sample_rate_hz=target_rate,
                    reason=f"coordinator-{request.operation}",
                    detail=request.detail,
                    # The measurement entry owns the sample-rate session lock
                    # for its whole transition; the nested preset reload must
                    # not re-enter it (same contract as the direct helper
                    # syncs below).
                    _rate_lock_held=request.operation in {"measurement-entry", "measurement-restore"},
                )
                preset_reloaded = True
            if not await self.wait_for_dsp_output_ports(timeout):
                raise RuntimeError("Coordinator effects stage failed: native DSP output ports were not confirmed")
            if request.operation in {"measurement-entry", "output-mode-switch"} and not await self._deps.reconcile_sink_rate(target_rate, reason=f"effects-{request.operation}"):
                status = dict(self._deps.get_samplerate_status())
                raise RuntimeError(f"Coordinator effects stage rate reconcile failed: expected={target_rate} active={status.get('active_rate')} force={status.get('force_rate')}")
            if request.operation == "output-mode-switch":
                await self._deps.sync_runtime(audio_overview=overview, reason="coordinator-output-mode-switch", _rate_lock_held=False, target_overview=overview)
                helper_rebuilt = True
                if mode in self._deps.output_mode_subwoofer_modes:
                    await self.reconcile_subwoofer_links_only()
                elif not diagnosis.get("links_complete"):
                    await self.repair_stereo_output_links_once(diagnosis)
                if manager is not None:
                    lock = self._deps.get_dsp_preset_load_lock()
                    if lock is None:
                        lock = asyncio.Lock()
                    async with lock:
                        compare = manager.load_compare_state()
                        side = compare.get("activeSide") if compare.get("activeSide") in {"A", "B"} else None
                        target = compare.get("presetA") if side == "A" else compare.get("presetB") if side == "B" else None
                        if target and manager.get_active_preset() != target:
                            await self._deps.load_dsp_preset(target, convolver_sample_rate_hz=target_rate)
                            if not await self.wait_for_dsp_output_ports(timeout):
                                raise RuntimeError("Coordinator compare preset restore did not recreate native DSP output ports")
                            preset_reloaded = True
                links_reconciled = True
            else:
                snapshot = dict(self._deps.get_dsp_snapshot() or {}) if self._deps.get_dsp_snapshot else {}
                helper_needs_sync = bool(request.rate_change or not snapshot.get("active") or self._deps.helper_argument_sample_rate(snapshot) != target_rate or not diagnosis.get("helper_ports") or not all(diagnosis.get("links", {}).values()))
                if helper_needs_sync:
                    kwargs = {"reason": f"coordinator-{request.operation}"}
                    if request.operation in {"measurement-entry", "measurement-restore"}:
                        # The measurement-entry establishes its target rate inside
                        # this same transition (force pin + sink alignment), but
                        # the live audio overview still reflects the pre-rebuild
                        # hardware rate -- rebuilding the helper at the target is
                        # precisely what moves the device.  Handing the
                        # subordinate sync that pre-switch overview would make its
                        # stale-check compare requested(96000) != authoritative
                        # (48000) and wrongly suppress the required helper
                        # rebuild, so the token carries the transition's own
                        # target rate instead.
                        kwargs.update(
                            audio_overview=samplerate.audio_output_overview_with_effective_rate(overview, target_rate),
                            _rate_lock_held=True,
                        )
                    await self._deps.sync_runtime(**kwargs); helper_rebuilt = True
                if helper_needs_sync or not diagnosis.get("links_complete"):
                    if mode in self._deps.output_mode_subwoofer_modes: await self.reconcile_subwoofer_links_only()
                    else: await self.repair_stereo_output_links_once(diagnosis)
                    links_reconciled = True
        final = await self.playback_graph_diagnosis(overview, target_rate=target_rate)
        for _ in range(3):
            if final.get("links_complete"): break
            if final.get("bypass_only"): await self.reconcile_subwoofer_links_only()
            else: await self.relink_missing_production_links(final, require_source=False)
            await self._deps.sleep(self._deps.pipewire_poll_interval_ms * 5 / 1000)
            final = await self.playback_graph_diagnosis(overview, target_rate=target_rate)
        if not final.get("links_complete"):
            self.log_playback_graph_diagnosis(final, target_rate=target_rate, reason=f"coordinator-{request.operation}", detail=request.detail)
            raise RuntimeError("Coordinator effects/helper graph did not reach the canonical topology")
        return {"dsp_reinitialized": preset_reloaded, "preset_reloaded": preset_reloaded, "helper_rebuilt": helper_rebuilt, "links_reconciled": links_reconciled, "graph_complete": True, "graph_signature": final.get("signature")}

    async def playback_graph_links_complete(self, audio_overview: dict | None = None, *, source: str | None = None, target_rate: int | None = None, require_source: bool = False) -> bool:
        return (await self.playback_graph_diagnosis(audio_overview, source=source, target_rate=target_rate, require_source=require_source))["links_complete"]

    async def ensure_mpv_to_dsp_links(self, timeout_ms: int | None = None) -> bool:
        """Wait for MPV source ports, then reconcile only missing ingress links."""
        if self._deps.mpv_source_ports_present is None:
            raise RuntimeError("MPV source-port readiness is not configured")
        expected = (
            ("mpv:output_FL", "fxroute_dsp_sink:playback_FL"),
            ("mpv:output_FR", "fxroute_dsp_sink:playback_FR"),
        )
        readiness_timeout = self._deps.source_port_readiness_timeout_ms if timeout_ms is None else timeout_ms
        readiness_deadline = time.monotonic() + max(readiness_timeout, 0) / 1000
        while not await self._deps.mpv_source_ports_present():
            if time.monotonic() >= readiness_deadline:
                logger.warning(
                    "Radio handoff MPV source ports did not appear within %s ms; skipping link repair",
                    readiness_timeout,
                )
                return False
            await self._deps.sleep(self._deps.pipewire_poll_interval_ms / 1000)

        repair_deadline = time.monotonic() + self._deps.mpv_link_repair_timeout_ms / 1000
        while True:
            try:
                links_text = await self._deps.run_pw_link_command("-l")
                missing = [
                    (source, target)
                    for source, target in expected
                    if not self._deps.contains_link(links_text, source, target)
                ]
                if not missing:
                    logger.info("Radio handoff MPV->DSP ingress links complete")
                    return True
                for source, target in missing:
                    logger.info("Radio handoff repairing MPV->DSP ingress link: %s -> %s", source, target)
                    await self._deps.connect_ports((source,), target)
            except Exception as exc:
                if time.monotonic() >= repair_deadline:
                    logger.warning("Radio handoff MPV->DSP ingress link repair failed: %s", exc)
                    return False
            if time.monotonic() >= repair_deadline:
                break
            await self._deps.sleep(self._deps.pipewire_poll_interval_ms / 1000)
        try:
            links_text = await self._deps.run_pw_link_command("-l")
            return all(self._deps.contains_link(links_text, source, target) for source, target in expected)
        except Exception:
            return False


_configured: PlaybackOrchestrator | None = None


def configure(deps: PlaybackOrchestrationDeps) -> PlaybackOrchestrator:
    global _configured
    _configured = PlaybackOrchestrator(deps)
    return _configured


def configured() -> PlaybackOrchestrator:
    if _configured is None:
        raise RuntimeError("playback orchestration has not been configured")
    return _configured
