# SPDX-License-Identifier: AGPL-3.0-only

"""FXRoute-specific implementation of the playback TransitionRuntime contract.

This module owns the concrete runtime adapter for the generic
``PlaybackTransitionCoordinator`` from ``playback_transition.py``.  It is
deliberately decoupled from ``main.py``: every application-shell dependency
(player, EasyEffects manager, queue/track state, shared helpers) arrives
through the explicit ``PlaybackRuntimeDependencies`` wiring, resolved
late-bound so production wiring and test mocks observe the same attributes.

Module boundary: must never import ``main`` (enforced by
``scripts/check_router_structure.py``).
"""

from __future__ import annotations

import asyncio
import copy
import logging
import math
import os
import re
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import unquote

import samplerate
import samplerate_orchestration
from playback_queue import PlaybackQueue
from playback_transition import TransitionRequest, TransitionRuntime
from samplerate import (
    OUTPUT_MODE_STEREO,
    OUTPUT_MODE_SUBWOOFER_MODES,
    OUTPUT_MODE_SUBWOOFER_22_MODES,
    persist_sample_rate_policy,
)
from spotify import (
    play as spotify_play,
    next_track as spotify_next,
    previous as spotify_previous,
)

logger = logging.getLogger(__name__)

SOURCE_HANDOFF_SETTLE_MS = 260
RADIO_EXPECTED_SAMPLE_RATE_HZ = 44100


def _hardware_sink_for_transition(deps: PlaybackRuntimeDependencies) -> str:
    """Resolve the physical sink used by the coordinator output gate."""
    status = deps.get_samplerate_status()
    relevant_sink = status.get("relevant_sink") or {}
    output_key = str(relevant_sink.get("name") or "").strip()
    if output_key:
        return output_key
    overview = deps.get_audio_output_overview()
    output_mode = overview.get("output_mode") or {}
    output_key = str(output_mode.get("effective_output_key") or "").strip()
    if not output_key:
        raise RuntimeError("Playback transition output gate has no hardware sink")
    return output_key


def _playback_gate_state_path() -> Path:
    """Return the per-user marker used to recover a stale FXRoute mute."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return Path(runtime_dir) / "fxroute-playback-gate.json"
    return Path("/tmp") / f"fxroute-playback-gate-{os.getuid()}.json"


def _read_sink_mute(sink_name: str) -> bool:
    completed = subprocess.run(
        ["pactl", "get-sink-mute", sink_name],
        capture_output=True,
        text=True,
        check=False,
        timeout=1.5,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise RuntimeError(stderr or f"pactl get-sink-mute {sink_name} failed")
    match = re.search(
        r"(?:^|\n)\s*Mute:\s*(yes|no)\s*$",
        completed.stdout or "",
        re.IGNORECASE | re.MULTILINE,
    )
    if not match:
        raise RuntimeError(f"Could not parse mute state for sink {sink_name}")
    return match.group(1).lower() == "yes"


def _set_sink_mute(sink_name: str, muted: bool) -> None:
    completed = subprocess.run(
        ["pactl", "set-sink-mute", sink_name, "1" if muted else "0"],
        capture_output=True,
        text=True,
        check=False,
        timeout=1.5,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise RuntimeError(stderr or f"pactl set-sink-mute {sink_name} {int(muted)} failed")


def _read_hardware_sink_mute(output_key: str) -> bool:
    return _read_sink_mute(output_key)


def _set_hardware_sink_mute(output_key: str, muted: bool) -> None:
    _set_sink_mute(output_key, muted)


@dataclass(frozen=True)
class PlaybackRuntimeDependencies:
    """Late-bound accessors into the FXRoute application shell (main.py).

    Every field resolves the current shell state at call time, mirroring the
    existing ``configure_*`` dependency pattern: production wiring and test
    mocks observe the same module attributes.  No service locator, no global
    registry; the runtime constructor takes exactly this typed structure.
    """

    # Runtime services
    player: Callable[[], Any]
    easyeffects_manager: Callable[[], Any]
    subwoofer_runtime: Callable[[], Any]

    # Playback-context state owned by the application shell
    get_current_track_info: Callable[[], dict | None]
    set_current_track_info: Callable[[dict | None], None]
    get_playback_intent_generation: Callable[[], int]
    get_transition_epoch: Callable[[], int]
    set_footer_owner: Callable[[str], None]
    queue: Callable[[], PlaybackQueue]

    # Player / transport primitives (main.py)
    player_is_running: Callable[..., bool]
    load_player_paused: Callable[..., None]
    wait_for_player_current_file: Callable[..., Awaitable[bool]]
    wait_for_player_audio_samplerate: Callable[..., Awaitable[Any]]
    get_player_audio_samplerate: Callable[[], int | None]
    wait_for_radio_live_rate_after_load: Callable[..., Awaitable[Any]]
    wait_for_pipewire_mpv_release: Callable[..., Awaitable[bool]]
    wait_for_pipewire_spotify_release: Callable[..., Awaitable[bool]]
    wait_for_spotify_sink_input_samplerate: Callable[..., Awaitable[Any]]

    # Samplerate / coordinator helpers (main.py)
    get_samplerate_status: Callable[..., dict]
    get_audio_output_overview: Callable[..., dict]
    ensure_playback_samplerate_force: Callable[..., Awaitable[bool]]
    persist_audio_output_mode: Callable[..., dict]
    trigger_idle_sink_renegotiation: Callable[..., Awaitable[bool]]
    reconcile_transition_sink_rate: Callable[..., Awaitable[bool]]
    coordinator_source_rate: Callable[..., int | None]
    coordinator_target_rate: Callable[..., int | None]

    # Spotify / playback-state helpers (main.py)
    spotify_pause: Callable[..., Awaitable[Any]]
    get_spotify_ui_state: Callable[..., Awaitable[dict]]
    is_spotify_playback_active: Callable[..., bool]
    has_local_footer_context: Callable[..., bool]
    pause_spotify_for_local_playback_broadcast: Callable[[], Awaitable[None]]
    pause_local_playback_for_spotify_broadcast: Callable[[], Awaitable[None]]
    mark_player_state_authoritative: Callable[..., None]
    spotify_snapshot_identity_values: Callable[..., set]
    measurement_restore_intent_matches_live_state: Callable[..., Awaitable[bool]]

    # EasyEffects / worker helpers (main.py)
    easyeffects_mutation_lock: Callable[[], Any]
    drain_worker: Callable[..., Awaitable[Any]]
    load_easyeffects_preset: Callable[..., Awaitable[None]]
    sync_subwoofer_runtime: Callable[..., Awaitable[dict]]
    helper_argument_sample_rate: Callable[..., int | None]

    # Graph / effects-helper primitives (main.py)
    playback_graph_diagnosis: Callable[..., Awaitable[dict]]
    measurement_session_link_loss_is_repairable: Callable[..., bool]
    coordinator_reconcile_subwoofer_links_only: Callable[[], Awaitable[None]]
    repair_stereo_output_links_once: Callable[..., Awaitable[None]]
    coordinator_establish_effects_and_helper: Callable[..., Awaitable[dict]]
    ensure_mpv_to_easyeffects_links: Callable[[], Awaitable[bool]]
    playback_graph_links_complete: Callable[..., Awaitable[bool]]
    log_playback_graph_diagnosis: Callable[..., None]
    coordinator_reconcile_post_start_graph: Callable[..., Awaitable[dict]]


class FxrouteTransitionRuntime(TransitionRuntime):
    """Concrete runtime adapter; graph mutations only enter via the coordinator."""

    def __init__(self, deps: PlaybackRuntimeDependencies) -> None:
        self._deps = deps
        self._output_key: str | None = None
        self._staged_target_url: str | None = None

    @property
    def _player(self) -> Any:
        """MPV wrapper resolved late-bound through the app-shell wiring."""
        return self._deps.player()

    @property
    def _easyeffects_manager(self) -> Any:
        """EasyEffects manager resolved late-bound through the app-shell wiring."""
        return self._deps.easyeffects_manager()

    @property
    def _subwoofer_runtime(self) -> Any:
        """Subwoofer helper runtime resolved late-bound through the wiring."""
        return self._deps.subwoofer_runtime()

    async def read_hardware_mute(self) -> bool:
        self._output_key = _hardware_sink_for_transition(self._deps)
        return await asyncio.to_thread(_read_hardware_sink_mute, self._output_key)

    async def set_hardware_mute(self, muted: bool, transition_id: str) -> None:
        output_key = self._output_key or _hardware_sink_for_transition(self._deps)
        self._output_key = output_key
        await asyncio.to_thread(_set_hardware_sink_mute, output_key, muted)
        logger.info(
            "Playback transition output gate set: output=%s muted=%s transition_id=%s",
            output_key,
            muted,
            transition_id,
        )

    async def read_sink_mute(self, sink_name: str) -> bool:
        return await asyncio.to_thread(_read_sink_mute, sink_name)

    async def set_sink_mute(
        self, sink_name: str, muted: bool, transition_id: str
    ) -> None:
        await asyncio.to_thread(_set_sink_mute, sink_name, muted)
        logger.info(
            "Playback transition explicit sink mute set: sink=%s muted=%s transition_id=%s",
            sink_name,
            muted,
            transition_id,
        )

    async def read_measurement_session_graph(self, target_rate: int) -> dict[str, Any]:
        """Read the active-measurement graph without touching playback state."""
        rate = dict(self._deps.get_samplerate_status())
        diagnosis = await self._deps.playback_graph_diagnosis(
            target_rate=target_rate,
            require_source=False,
        )
        result = dict(diagnosis)
        result["active_rate"] = rate.get("active_rate")
        result["force_rate"] = rate.get("force_rate")
        result["measurement_rate_aligned"] = bool(
            samplerate.playback_rate_aligned(rate, target_rate)
        )
        result["repairable_link_loss"] = self._deps.measurement_session_link_loss_is_repairable(
            result,
            target_rate=target_rate,
        )
        return result

    async def reconcile_measurement_session_graph(self, _target_rate: int) -> None:
        """Repair only existing production links; never reload EE or the helper."""
        diagnosis = await self._deps.playback_graph_diagnosis(
            target_rate=_target_rate,
            require_source=False,
        )
        if diagnosis.get("mode") in OUTPUT_MODE_SUBWOOFER_MODES:
            await self._deps.coordinator_reconcile_subwoofer_links_only()
        elif diagnosis.get("mode") == OUTPUT_MODE_STEREO:
            await self._deps.repair_stereo_output_links_once(diagnosis)
        else:
            raise RuntimeError(
                "measurement session graph reconciliation has no mode repair path"
            )

    async def read_transition_snapshot(self, request: TransitionRequest) -> dict[str, Any]:
        self._staged_target_url = None
        state = dict(self._player.state if self._player else {})
        try:
            rate = dict(self._deps.get_samplerate_status())
        except Exception:
            rate = {}
        snapshot = {
            "player": state,
            "active_rate": rate.get("active_rate"),
            "force_rate": rate.get("force_rate"),
            "source": request.source,
            "target_url": request.target_url,
            "current_track": dict(self._deps.get_current_track_info() or {}),
            "playback_intent_generation": self._deps.get_playback_intent_generation(),
        }
        if request.operation == "output-mode-switch":
            snapshot["output_mode_overview"] = copy.deepcopy(self._deps.get_audio_output_overview())
            snapshot["output_mode_config"] = copy.deepcopy(
                samplerate._load_raw_audio_output_mode()
            )
            snapshot["ee_active_preset"] = (
                self._easyeffects_manager.get_active_preset()
                if self._easyeffects_manager is not None
                else None
            )
            snapshot["spotify"] = await self._deps.get_spotify_ui_state()
        return snapshot

    def target_source_staged(self, request: TransitionRequest) -> bool:
        """Report whether this transition has staged a new MPV target."""
        return bool(
            request.source in {"local", "radio"}
            and request.target_url
            and self._staged_target_url == request.target_url
        )

    async def abort_failed_transition(
        self,
        request: TransitionRequest,
        snapshot: Mapping[str, Any] | None,
        *,
        target_staged: bool,
        ensure_gate_closed: Callable[..., Awaitable[None]] | None = None,
    ) -> bool | None:
        """Finish a failed MPV handoff without mixing old and new context.

        The Coordinator has already attenuated and paused the source before it
        calls this hook. If MPV still exposes the exact pre-transition file,
        the committed context remains valid and nothing is invalidated.  Once
        a new target was staged (or the old file disappeared), stop the
        physical target and invalidate only the active track metadata, while
        preserving ``last_track_info`` and the committed queue state: a failed
        transition must never discard the previously working queue.
        """

        snapshot_track = dict((snapshot or {}).get("current_track") or {})
        previous_state = dict((snapshot or {}).get("player") or {})
        if request.source not in {"local", "radio"}:
            if request.source != "spotify":
                return
            # A failed Spotify handoff already quieted and stopped the
            # previously committed Local/Radio source and cleared its track
            # context before the Spotify start was verified (quiet_old_source
            # -> self._deps.pause_local_playback_for_spotify_broadcast).  Restore the
            # pre-transition committed source physically (sample rate, MPV
            # load, pause/play state, volume) from the Coordinator snapshot
            # so a failed Spotify start never loses both sources.  The
            # committed queue, last_track_info and radio-reconnect state were
            # never touched by the handoff and stay as they are.  On success
            # this returns True so the Coordinator restores the output gate
            # instead of latching a failure; on restore failure the existing
            # failure latch keeps the safe state.
            if snapshot_track.get("source") in {"local", "radio"} and bool(
                previous_state.get("current_file")
                or previous_state.get("playing")
                or previous_state.get("paused")
                or previous_state.get("ended")
            ):
                restored = await self._restore_committed_source_after_failed_transition(
                    request,
                    snapshot,
                    previous_state,
                    snapshot_track,
                    ensure_gate_closed=ensure_gate_closed,
                )
                if restored:
                    self._deps.set_current_track_info(dict(snapshot_track))
                    self._deps.set_footer_owner("local")
                    self._deps.mark_player_state_authoritative(self._player.state if self._player else {})
                    logger.warning(
                        "Spotify handoff failed; restored committed %s source for retry: "
                        "track_id=%s url=%s",
                        snapshot_track.get("source"),
                        snapshot_track.get("id"),
                        snapshot_track.get("url"),
                    )
                    return True
                logger.warning(
                    "Spotify handoff failed and the committed %s source could not be "
                    "restored; keeping the failure gate latched: track_id=%s url=%s",
                    snapshot_track.get("source"),
                    snapshot_track.get("id"),
                    snapshot_track.get("url"),
                )
            return

        previous_state = dict((snapshot or {}).get("player") or {})
        current_state = dict(self._player.state if self._player else {})
        previous_file = previous_state.get("current_file")
        current_file = current_state.get("current_file")
        previous_context_unchanged = (
            not target_staged
            and current_file == previous_file
            and not (current_file is None and self._deps.get_current_track_info())
        )

        if previous_context_unchanged:
            live_track = self._deps.get_current_track_info() or {}
            if current_file and live_track.get("url") not in {None, current_file}:
                snapshot_track = dict((snapshot or {}).get("current_track") or {})
                if snapshot_track.get("url") == current_file:
                    self._deps.set_current_track_info(snapshot_track)
                else:
                    previous_context_unchanged = False
            if previous_context_unchanged:
                # The committed queue and track context stay valid: MPV still
                # exposes the exact pre-transition file and a staged queue
                # candidate was never published.  Nothing to invalidate.
                return

        if snapshot_track.get("source") in {"local", "radio"} and previous_state.get("current_file"):
            restored = await self._restore_committed_source_after_failed_transition(
                request,
                snapshot,
                previous_state,
                snapshot_track,
                ensure_gate_closed=ensure_gate_closed,
            )
            if restored:
                self._deps.set_current_track_info(snapshot_track)
                self._deps.set_footer_owner("local")
                self._deps.mark_player_state_authoritative(self._player.state if self._player else {})
                return True

        # The target was staged, the old file disappeared, or the active
        # metadata no longer matches MPV. Stop the physical target first and
        # then invalidate only the active context. last_track_info is
        # deliberately untouched so the caller can offer a retry.  The
        # committed queue state is preserved.
        if self._deps.player_is_running():
            try:
                set_volume = getattr(self._player, "set_volume", None)
                if callable(set_volume):
                    set_volume(0)
            except Exception:
                logger.warning(
                    "Failed to attenuate MPV during failed transition abort",
                    exc_info=True,
                )
            try:
                stop_playback = getattr(self._player, "stop_playback", None)
                if callable(stop_playback):
                    stop_playback()
                else:
                    self._player.set_pause(True)
            except Exception:
                logger.warning(
                    "Failed to stop staged MPV target during transition abort",
                    exc_info=True,
                )
            # After a staged failure the retained committed queue can no
            # longer be trusted as a complete MPV-native playlist, even
            # when the transport cleanup itself failed.  Normalize it to
            # app-owned navigation so the queue data stays usable.
            self._deps.queue().normalize_after_native_loss()

        self._deps.set_current_track_info(None)
        self._deps.set_footer_owner("local")
        self._deps.mark_player_state_authoritative(self._player.state if self._player else {})

    async def _restore_committed_source_after_failed_transition(
        self,
        request: TransitionRequest,
        snapshot: Mapping[str, Any],
        previous_state: Mapping[str, Any],
        track: Mapping[str, Any],
        *,
        ensure_gate_closed: Callable[..., Awaitable[None]] | None = None,
    ) -> bool:
        """Physically restore the previously committed Local/Radio source and
        its full playback graph after a failed source transition.

        Runs the same bounded low-level Coordinator stage primitives that a
        normal Local/Radio transition executes under the still-closed output
        gate (never a nested Coordinator transition): old rate, effects and
        helper for the old rate, source/queue transport (including a
        committed native MPV playlist), post-start graph reconcile, staged
        graph readback, DSP stabilization when the failed Spotify transition
        reinitialized the DSP, and a final commit readback.  Between the
        critical stages the Coordinator-bound ``ensure_gate_closed`` boundary
        check re-confirms the physical output gate.  Returns True only when
        the old source is confirmed in its previous transport state on the
        complete old graph; any stage failure keeps the failure latch.
        """
        source = str(track.get("source") or "")
        target_url = str(track.get("url") or previous_state.get("current_file") or "")
        if source not in {"local", "radio"} or not target_url:
            return False
        # The committed native-queue request fields are the single canonical
        # source for both the restore decision and the carried playlist: a
        # committed native queue was already validated for homogeneity at
        # commit time, so the canonical gate is equivalent here.
        native_fields = self._deps.queue().native_request_fields()
        native_committed = bool(native_fields)
        # The authoritative restore rate comes from the previously committed
        # snapshot, not from the failed request: preferred positive
        # active_rate (the actually committed hardware rate), then positive
        # force_rate, then the track-derived Coordinator rate.
        snapshot_active = int((snapshot or {}).get("active_rate") or 0)
        snapshot_force = int((snapshot or {}).get("force_rate") or 0)
        restore_target_rate = (
            snapshot_active if snapshot_active > 0 else (snapshot_force if snapshot_force > 0 else 0)
        )
        if restore_target_rate <= 0:
            derived = self._deps.coordinator_target_rate(source, track)
            restore_target_rate = int(derived) if isinstance(derived, int) and derived > 0 else 0
        if restore_target_rate <= 0:
            logger.warning(
                "Failed-transition source restore aborted: no authoritative committed "
                "sample rate for %s source url=%s",
                source,
                target_url,
            )
            return False
        # rate_change is not a blind copy of the failed request: it must
        # cover both a real rate/DSP switch performed by the failed Spotify
        # handoff and a live state that currently differs from the committed
        # restore rate.  Unknown or missing live rate state counts as a
        # possible rate change (conservative), so effects/helper are
        # validated/reinitialized; establish_target_rate stays idempotent
        # when the hardware already stands correctly.
        try:
            live_status = dict(self._deps.get_samplerate_status())
        except Exception:
            live_status = {}
        live_active = int(live_status.get("active_rate") or 0)
        live_aligned = bool(live_active > 0 and live_active == restore_target_rate)
        restore_rate_change = bool(request.rate_change or not live_aligned)
        was_playing = bool(
            previous_state.get("playing")
            and not previous_state.get("paused")
            and not previous_state.get("ended")
        )
        previous_position = previous_state.get("position")
        restore_position = (
            max(0.0, float(previous_position))
            if source == "local"
            and isinstance(previous_position, (int, float))
            and previous_position > 0
            else None
        )
        restore_request = TransitionRequest(
            operation="replay",
            source=source,
            target_rate=restore_target_rate,
            target_url=target_url,
            target_track=dict(track),
            should_play=was_playing,
            rate_change=restore_rate_change,
            reload_source=True,
            restore_position=restore_position,
            native_queue=(
                tuple(native_fields["native_queue"]) if native_committed else None
            ),
            native_queue_index=native_fields.get("native_queue_index") if native_committed else None,
            native_queue_loop=bool(native_fields.get("native_queue_loop")) if native_committed else False,
            detail="failed-transition-restore",
        )
        restored = False
        try:
            # A verify failure after a successful Spotify start can leave the
            # Spotify sink input active while the old rate and graph are
            # restored.  Quiesce it through the existing bounded release
            # helper before any old-graph stage touches the graph; if the
            # active Spotify source does not release within the existing
            # bound, the restore fails and the failure latch stays.
            if request.source == "spotify" and not await self._deps.wait_for_pipewire_spotify_release():
                logger.warning(
                    "Failed-transition source restore aborted: active Spotify sink "
                    "input did not quiesce before the old source restore"
                )
                return False
            # The Coordinator-owned hardware gate must be physically closed
            # before ANY mutating restore stage (rate, effects/helper, MPV,
            # graph, volume) runs: the original Spotify transition may itself
            # have failed at output-gate-close, leaving the gate unverified.
            if ensure_gate_closed is not None:
                await ensure_gate_closed(stage="failed-transition-restore-before-rate")
            await self.establish_target_rate(restore_request)
            if ensure_gate_closed is not None:
                await ensure_gate_closed(stage="failed-transition-restore-after-rate")
            effects_state: dict[str, Any] = {}
            effects_result = await self.establish_effects_and_helper(restore_request)
            if isinstance(effects_result, Mapping):
                effects_state = dict(effects_result)
            dsp_reinitialized = bool(effects_state.get("dsp_reinitialized"))
            if ensure_gate_closed is not None:
                await ensure_gate_closed(stage="failed-transition-restore-after-effects-helper")
            await self.prepare_target_source(restore_request)
            if ensure_gate_closed is not None:
                await ensure_gate_closed(stage="failed-transition-restore-before-start")
            await self.start_target_source(restore_request)
            reconciler = getattr(self, "reconcile_post_start_graph", None)
            if callable(reconciler):
                post_state = await reconciler(restore_request)
                if not isinstance(post_state, Mapping) or not post_state.get(
                    "graph_complete", False
                ):
                    logger.warning(
                        "Failed-transition source restore aborted: post-start graph "
                        "reconciliation did not confirm a complete graph"
                    )
                    return False
            graph_state = await self.verify_transition_graph(restore_request)
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
            if ensure_gate_closed is not None:
                await ensure_gate_closed(stage="failed-transition-restore-before-volume")
            await self.set_source_volume(100, "failed-transition-restore")
            # DSP stabilization is not artificially forced for paused
            # restores; it keeps its existing rate/DSP-reinit condition.
            if restore_request.should_play and (restore_rate_change or dsp_reinitialized):
                dsp_state = await self.stabilize_effects_after_rate_change(
                    restore_request, dsp_reinitialized=dsp_reinitialized
                )
                if not isinstance(dsp_state, Mapping) or not dsp_state.get(
                    "stabilized", False
                ):
                    logger.warning(
                        "Failed-transition source restore aborted: DSP stabilization "
                        "was not confirmed"
                    )
                    return False
            if ensure_gate_closed is not None:
                await ensure_gate_closed(stage="failed-transition-restore-after-dsp")
            final_state = await self.verify_committed_transition(restore_request)
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
            restored = True
        except Exception as exc:
            logger.warning("Failed-transition source restore failed: %s", exc)
        if not restored and native_committed:
            # The native playlist could not be reconstructed; normalize to
            # the existing app-owned navigation so no later jump targets a
            # phantom MPV playlist (same contract as the staged abort path).
            self._deps.queue().normalize_after_native_loss()
        return restored

    async def validate_measurement_restore_intent(
        self,
        request: TransitionRequest,
        _snapshot: Mapping[str, Any] | None = None,
    ) -> bool:
        """Reject a measurement restore after the user changed playback intent."""
        intent = request.restore_intent or {}
        if not intent:
            return True

        expected_source = str(intent.get("source") or request.source)
        expected_spotify_identities: set[str] = set()
        if expected_source == "spotify":
            expected_spotify_identities = self._deps.spotify_snapshot_identity_values(intent)
            if not expected_spotify_identities:
                expected_spotify_identities = self._deps.spotify_snapshot_identity_values({
                    "target_url": request.target_url,
                    "track_info": request.target_track,
                })

        expected_id = intent.get("id")
        if expected_id in {None, ""}:
            expected_id = None
        return await self._deps.measurement_restore_intent_matches_live_state(
            expected_source=expected_source,
            expected_id=expected_id,
            expected_url=intent.get("url") or intent.get("path") or request.target_url,
            expected_file=intent.get("current_file"),
            expected_spotify_identities=expected_spotify_identities,
            intent_generation=intent.get("intent_generation"),
        )

    async def quiet_old_source(self, request: TransitionRequest) -> None:
        if request.graph_only:
            # A graph-only reconciliation must not pause, reload, or otherwise
            # disturb the source.  The coordinator still owns the output gate.
            return
        if request.source == "spotify":
            if request.operation == "recovery" and request.reload_source and request.should_play:
                # A Spotify samplerate recovery must release the old sink
                # input before the Coordinator changes the hardware rate and
                # starts Spotify again.  This is intentionally kept inside
                # the Coordinator-owned quiet stage.
                await self._deps.spotify_pause()
                released = await self._deps.wait_for_pipewire_spotify_release()
                if not released:
                    await asyncio.sleep(SOURCE_HANDOFF_SETTLE_MS / 1000)
                return
            if request.operation in {"measurement-entry", "output-mode-switch", "sample-rate-policy"}:
                spotify_state = await self._deps.get_spotify_ui_state()
                if self._deps.is_spotify_playback_active(spotify_state):
                    await self._deps.spotify_pause()
                    if not await self._deps.wait_for_pipewire_spotify_release():
                        raise RuntimeError(
                            "active Spotify sink input did not quiesce before guarded graph transition"
                        )
                return
            local_state = dict(self._player.state if self._player else {})
            local_track = self._deps.get_current_track_info() or {}
            if (
                local_track.get("source") in {"local", "radio"}
                and self._deps.has_local_footer_context(local_state)
            ):
                await self._deps.pause_local_playback_for_spotify_broadcast()
            return
        if request.source not in {"local", "radio"}:
            return
        spotify_state = await self._deps.get_spotify_ui_state()
        if self._deps.is_spotify_playback_active(spotify_state):
            await self._deps.pause_spotify_for_local_playback_broadcast()
            # The output gate is already closed at this Coordinator stage.
            # Do not touch rate/DSP/helper state until the active Spotify
            # stream has disappeared. Corked historical inputs are ignored by
            # the read-only release helper and therefore need not vanish.
            if not await self._deps.wait_for_pipewire_spotify_release():
                raise RuntimeError(
                    "active Spotify sink input did not quiesce before MPV handoff"
                )
        if not self._deps.player_is_running():
            return
        state = self._player.state
        set_volume = getattr(self._player, "set_volume", None)
        if callable(set_volume):
            set_volume(0)
        if (
            request.operation == "recovery"
            and state.get("current_file")
            and not request.rate_change
        ):
            self._player.set_pause(True)
            return
        # A healthy same-rate replacement keeps the existing MPV/PipeWire
        # stream alive and only quiets it.  A real rate change must release the
        # old stream before the target-rate negotiation begins.
        self._player.set_pause(True)
        should_release = bool(
            request.rate_change
            and request.operation not in {"measurement-entry", "output-mode-switch"}
            and (
                request.operation != "sample-rate-policy"
                or request.reload_source
            )
            and state.get("current_file")
        )
        if should_release:
            self._player.stop_playback()
            released = await self._deps.wait_for_pipewire_mpv_release()
            if not released:
                await asyncio.sleep(SOURCE_HANDOFF_SETTLE_MS / 1000)

    async def resolve_target_rate(self, request: TransitionRequest) -> int | None:
        """Resolve a post-load source rate while the output gate is closed."""
        if (
            request.source == "local"
            and request.reload_source
            and request.target_rate is None
        ):
            if not request.target_url:
                raise RuntimeError("Local playback fallback has no target URL")
            if not self._deps.player_is_running():
                raise RuntimeError("MPV player is not available")
            set_volume = getattr(self._player, "set_volume", None)
            if callable(set_volume):
                set_volume(0)
            self._deps.load_player_paused(request.target_url)
            if not await self._deps.wait_for_player_current_file(request.target_url):
                raise RuntimeError("local target did not settle while paused")
            live_rate = await self._deps.wait_for_player_audio_samplerate(
                expected_url=request.target_url,
            )
            if not isinstance(live_rate, int) or live_rate <= 0:
                raise RuntimeError(
                    "local target MPV audio-params did not expose a valid samplerate"
                )
            self._staged_target_url = request.target_url
            logger.info(
                "Local target samplerate resolved from MPV audio-params while paused: "
                "url=%s rate=%s",
                request.target_url,
                live_rate,
            )
            return samplerate.effective_playback_rate(live_rate, request.sample_rate_policy or None)

        if request.source != "radio" or not request.reload_source:
            return request.target_rate
        if not request.target_url:
            return request.target_rate
        if not self._deps.player_is_running():
            raise RuntimeError("MPV player is not available")

        previous_rate = self._deps.get_player_audio_samplerate()
        set_volume = getattr(self._player, "set_volume", None)
        if callable(set_volume):
            set_volume(0)
        self._deps.load_player_paused(request.target_url)
        if not await self._deps.wait_for_player_current_file(request.target_url):
            raise RuntimeError("radio target stream did not settle while paused")
        attempt_epoch = request.attempt_epoch
        if not isinstance(attempt_epoch, int):
            attempt_epoch = self._deps.get_transition_epoch()
        live_rate = await self._deps.wait_for_radio_live_rate_after_load(
            previous_rate,
            attempt_epoch,
        )
        if not isinstance(live_rate, int) or live_rate <= 0:
            live_rate = RADIO_EXPECTED_SAMPLE_RATE_HZ
            logger.warning(
                "Radio target rate unavailable while paused; using safe fallback=%s url=%s",
                live_rate,
                request.target_url,
            )
        self._staged_target_url = request.target_url
        return samplerate.effective_playback_rate(live_rate, request.sample_rate_policy or None)

    async def establish_target_rate(self, request: TransitionRequest) -> None:
        if request.graph_only:
            return
        if not isinstance(request.target_rate, int) or request.target_rate <= 0:
            raise RuntimeError("Playback transition has no target sample rate")
        try:
            status = dict(self._deps.get_samplerate_status())
        except Exception:
            status = {}
        if samplerate.playback_rate_aligned(status, request.target_rate):
            logger.info(
                "Playback transition target-rate no-op: rate=%s operation=%s source=%s",
                request.target_rate,
                request.operation,
                request.source,
            )
            return
        aligned = await self._deps.ensure_playback_samplerate_force(
            request.target_rate,
            f"coordinator:{request.operation}:{request.source}",
            allow_measurement_session=(request.operation == "measurement-restore"),
            policy=samplerate_orchestration.RADIO_POLICY,
        )
        if not aligned:
            aligned = await self._deps.trigger_idle_sink_renegotiation(request.target_rate)
        if not aligned:
            status = self._deps.get_samplerate_status()
            raise RuntimeError(
                "target hardware rate did not settle: "
                f"expected={request.target_rate} active={status.get('active_rate')} "
                f"force={status.get('force_rate')}"
            )

    async def establish_effects_and_helper(
        self, request: TransitionRequest
    ) -> dict[str, Any]:
        if request.operation == "pause":
            return {"dsp_reinitialized": False, "helper_rebuilt": False}
        if not isinstance(request.target_rate, int) or request.target_rate <= 0:
            return {"dsp_reinitialized": False, "helper_rebuilt": False}
        return await self._deps.coordinator_establish_effects_and_helper(request)

    async def prepare_target_source(self, request: TransitionRequest) -> None:
        if request.graph_only:
            return
        if request.source == "spotify":
            return
        if not self._deps.player_is_running():
            raise RuntimeError("MPV player is not available")

        if not request.reload_source and not request.should_play:
            self._player.set_pause(True)
            return

        set_volume = getattr(self._player, "set_volume", None)
        if callable(set_volume):
            set_volume(0)

        if request.native_queue:
            set_shuffle = getattr(self._player, "set_shuffle", None)
            if callable(set_shuffle):
                # Disable any legacy MPV-side permutation before staging or
                # jumping within the explicit FXRoute queue order.
                set_shuffle(False)
            queue_tracks = tuple(request.native_queue)
            if request.reload_source:
                start_index = request.native_queue_index
                if start_index is None:
                    start_index = 0
                if start_index < 0 or start_index >= len(queue_tracks):
                    raise RuntimeError(f"native MPV queue start index is out of range: {start_index}")
                first_url = str(queue_tracks[0].get("url") or "")
                if not first_url:
                    raise RuntimeError("native MPV queue has no first URL")
                self._deps.load_player_paused(first_url)
                for queued_track in queue_tracks[1:]:
                    queued_url = str(queued_track.get("url") or "")
                    if not queued_url:
                        raise RuntimeError("native MPV queue contains an empty URL")
                    self._player.loadfile(queued_url, mode="append")
                self._player.set_loop_playlist(bool(request.native_queue_loop))
                self._player.set_playlist_pos(start_index)
                self._staged_target_url = request.target_url
            else:
                self._player.set_pause(True)

            if not await self._deps.wait_for_player_current_file(request.target_url):
                raise RuntimeError("native MPV queue target did not settle while paused")

        if request.reload_source:
            if not request.target_url:
                raise RuntimeError("Playback transition has no target URL")
            if request.native_queue:
                # The native queue branch already staged the target and the
                # complete MPV playlist.  Do not replace it with app_replace.
                pass
            elif self._staged_target_url != request.target_url:
                # A non-native transition must not inherit loop/shuffle
                # controls from a previously committed native playlist.
                self._player.set_loop_playlist(False)
                set_shuffle = getattr(self._player, "set_shuffle", None)
                if callable(set_shuffle):
                    set_shuffle(False)
                self._deps.load_player_paused(request.target_url)
                self._staged_target_url = request.target_url
                if not await self._deps.wait_for_player_current_file(request.target_url):
                    raise RuntimeError("target MPV stream did not settle while paused")
            else:
                self._player.set_pause(True)

        if (
            request.operation in {"measurement-restore", "replay", "sample-rate-policy"}
            and request.source == "local"
            and request.restore_position is not None
        ):
            position = max(0.0, float(request.restore_position))
            seek = getattr(self._player, "seek", None)
            if not callable(seek):
                raise RuntimeError("MPV position restore is not available")
            self._player.set_pause(True)
            seek(position)
            get_property = getattr(self._player, "get_property", None)
            if callable(get_property):
                try:
                    readback = get_property("time-pos")
                except Exception as exc:
                    raise RuntimeError(
                        f"MPV position restore readback failed: {exc}"
                    ) from exc
                if isinstance(readback, (int, float)) and abs(float(readback) - position) > 0.5:
                    raise RuntimeError(
                        "MPV position restore was not confirmed: "
                        f"expected={position} actual={readback}"
                    )
            logger.info(
                "Local playback position restored under output gate: "
                "url=%s position=%.3f",
                request.target_url,
                position,
            )

        if not await self._deps.ensure_mpv_to_easyeffects_links():
            raise RuntimeError("target source to EasyEffects links were not confirmed")
        if not request.should_play:
            self._player.set_pause(True)

    async def start_target_source(self, request: TransitionRequest) -> None:
        if request.graph_only:
            return
        if request.source == "spotify":
            if request.operation == "spotify-next":
                data = await spotify_next()
            elif request.operation == "spotify-previous":
                data = await spotify_previous()
            elif request.should_play:
                data = await spotify_play()
            else:
                await self._deps.spotify_pause()
                data = {"status": "Paused"}
            if request.should_play and data.get("status") not in {"Playing", "playing"}:
                raise RuntimeError(f"Spotify did not enter Playing state: {data}")
            return
        if not self._deps.player_is_running():
            raise RuntimeError("MPV player is not available")
        self._player.set_pause(not request.should_play)
        if not request.should_play:
            return
        deadline = time.monotonic() + 1.8
        last_readback: dict[str, Any] = {}
        get_property = getattr(self._player, "get_property", None)
        while time.monotonic() <= deadline:
            if callable(get_property):
                try:
                    live_path = get_property("path")
                    live_paused = get_property("pause")
                    live_idle = get_property("idle-active")
                    live_time_pos = get_property("time-pos")
                    live_audio_params = get_property("audio-params")
                    last_readback = {
                        "path": live_path,
                        "pause": live_paused,
                        "idle-active": live_idle,
                        "time-pos": live_time_pos,
                        "audio-params": live_audio_params,
                    }
                    time_active = isinstance(live_time_pos, (int, float))
                    audio_active = isinstance(live_audio_params, Mapping) and bool(live_audio_params)
                    path_matches = not request.target_url or live_path == request.target_url
                    if (
                        not path_matches
                        and isinstance(live_path, str)
                        and live_path.startswith("file://")
                    ):
                        path_matches = unquote(live_path[7:]) == request.target_url
                    if (
                        path_matches
                        and live_paused is False
                        and live_idle is False
                        and time_active
                        and audio_active
                    ):
                        logger.info(
                            "Playback target start confirmed by MPV IPC: path=%s time_pos=%s audio=%s",
                            live_path,
                            live_time_pos,
                            live_audio_params,
                        )
                        return
                except Exception as exc:
                    last_readback = {"error": str(exc)}
            else:
                # Small test adapters may only expose the cached state.  The
                # production MPVWrapper always has get_property(), so the
                # actual runtime contract above remains IPC-based.
                state = self._player.state
                if (
                    (not request.target_url or state.get("current_file") == request.target_url)
                    and not state.get("paused")
                    and state.get("playing")
                    and isinstance(state.get("position"), (int, float))
                ):
                    return
            await asyncio.sleep(0.05)
        raise RuntimeError(
            "target MPV stream did not pass live IPC start readback: "
            f"{last_readback}"
        )

    async def _read_and_validate_effects_runtime(
        self, extras: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Read the active DSP work point after a guarded runtime update."""
        manager = self._easyeffects_manager
        if manager is None:
            return {}

        loudness = extras.get("loudness") or {}
        autogain = extras.get("autogain") or {}
        loudness_enabled = bool(loudness.get("enabled"))
        autogain_enabled = bool(autogain.get("enabled"))
        result: dict[str, Any] = {}

        read_loudness = getattr(manager, "read_loudness_runtime", None)
        if callable(read_loudness):
            try:
                loudness_runtime = await asyncio.to_thread(read_loudness)
            except Exception:
                if loudness_enabled:
                    raise
                loudness_runtime = None
            if isinstance(loudness_runtime, dict):
                try:
                    actual_volume = float(loudness_runtime["volume"])
                    actual_output_gain = float(loudness_runtime["output_gain"])
                except (KeyError, TypeError, ValueError) as exc:
                    if loudness_enabled:
                        raise RuntimeError(
                            f"EasyEffects Loudness readback is incomplete: {exc}"
                        ) from exc
                    loudness_runtime = None
                if isinstance(loudness_runtime, dict):
                    minimum = float(manager.LOUDNESS_PLUGIN_VOLUME_MIN_DB)
                    maximum = float(manager.LOUDNESS_PLUGIN_VOLUME_MAX_DB)
                    if not minimum <= actual_volume <= maximum:
                        raise RuntimeError(
                            "EasyEffects Loudness volume is outside the installed LSP range: "
                            f"{actual_volume} not in [{minimum}, {maximum}]"
                        )
                    expected_payload = manager._loudness_plugin_payload(
                        loudness, autogain
                    )
                    if loudness_enabled:
                        if loudness_runtime.get("bypass"):
                            raise RuntimeError(
                                "EasyEffects Loudness was bypassed after DSP stabilization"
                            )
                        if not math.isclose(
                            actual_volume,
                            float(expected_payload["volume"]),
                            rel_tol=0.0,
                            abs_tol=0.05,
                        ) or not math.isclose(
                            actual_output_gain,
                            float(expected_payload["output-gain"]),
                            rel_tol=0.0,
                            abs_tol=0.05,
                        ):
                            raise RuntimeError(
                                "EasyEffects Loudness work point mismatch: "
                                f"expected=({expected_payload['volume']}, "
                                f"{expected_payload['output-gain']}) "
                                f"actual=({actual_volume}, {actual_output_gain})"
                            )
                    result["loudness"] = {
                        "volume": actual_volume,
                        "output_gain": actual_output_gain,
                        "bypass": bool(loudness_runtime.get("bypass")),
                    }
        elif loudness_enabled:
            raise RuntimeError("EasyEffects Loudness readback is unavailable")

        read_autogain = getattr(manager, "read_autogain_runtime", None)
        if callable(read_autogain):
            try:
                autogain_runtime = await asyncio.to_thread(read_autogain)
            except Exception:
                if autogain_enabled:
                    raise
                autogain_runtime = None
            if isinstance(autogain_runtime, dict):
                try:
                    actual_target = float(autogain_runtime["target"])
                except (KeyError, TypeError, ValueError) as exc:
                    if autogain_enabled:
                        raise RuntimeError(
                            f"EasyEffects Auto Gain readback is incomplete: {exc}"
                        ) from exc
                    autogain_runtime = None
                if isinstance(autogain_runtime, dict):
                    expected_autogain = manager._autogain_plugin_payload(autogain)
                    if autogain_enabled:
                        if autogain_runtime.get("bypass"):
                            raise RuntimeError(
                                "EasyEffects Auto Gain was bypassed after DSP stabilization"
                            )
                        if not math.isclose(
                            actual_target,
                            float(expected_autogain["target"]),
                            rel_tol=0.0,
                            abs_tol=0.05,
                        ):
                            raise RuntimeError(
                                "EasyEffects Auto Gain target mismatch: "
                                f"expected={expected_autogain['target']} actual={actual_target}"
                            )
                    result["autogain"] = {
                        "target": actual_target,
                        "bypass": bool(autogain_runtime.get("bypass")),
                    }
        elif autogain_enabled:
            raise RuntimeError("EasyEffects Auto Gain readback is unavailable")

        return result

    async def stabilize_effects_after_rate_change(
        self,
        request: TransitionRequest,
        *,
        dsp_reinitialized: bool = False,
    ) -> dict[str, Any]:
        """Re-apply the canonical DSP work point after a rate/EE mutation.

        An output-mode switch may reload the compare preset or rebuild the
        EasyEffects graph, either of which can re-apply a stale preset
        loudness work point over the user volume.
        The switch therefore re-applies the canonical extras unconditionally
        instead of gating on rate_change/dsp_reinitialized, so the volume
        never resurrects an older preset value.
        """
        if not (
            request.rate_change
            or dsp_reinitialized
            or request.operation == "output-mode-switch"
        ):
            return {"stabilized": True, "no_op": True}
        if (
            not request.should_play
            and request.operation not in {"measurement-entry", "output-mode-switch"}
        ):
            return {"stabilized": True, "no_op": True}

        manager = self._easyeffects_manager
        if manager is None:
            return {"stabilized": True, "no_op": True}

        # The whole guarded re-apply runs under the central EasyEffects
        # mutation ownership, and the canonical extras are (re)read after
        # acquiring it: a parallel volume/extras/SPL mutation must never be
        # clobbered by a stale pre-lock snapshot.  The ownership is held
        # through the settle and the runtime readback/validation so the
        # coordinator validates the live DSP against the very extras it
        # applied, never against a snapshot made stale by a mutation that
        # landed between apply and verify.
        async with self._deps.easyeffects_mutation_lock():
            extras = manager.load_global_extras()
            loudness_enabled = bool((extras.get("loudness") or {}).get("enabled"))
            autogain_enabled = bool((extras.get("autogain") or {}).get("enabled"))
            if not loudness_enabled and not autogain_enabled:
                return {"stabilized": True, "no_op": True}

            apply_runtime = getattr(manager, "apply_autogain_loudness_runtime", None)
            if not callable(apply_runtime):
                raise RuntimeError("guarded Auto Gain/Loudness runtime is unavailable")

            # Passing the same canonical extras on both sides deliberately uses the
            # existing guarded order without reloading the preset.  The helper
            # method keeps the outputGain guard in place while the LSP volume port
            # settles, then ramps back to the canonical work point.  The blocking
            # manager call runs off the event loop; the caller owns the mutation
            # lock until the worker actually finished (cancellation-safe).
            await self._deps.drain_worker(
                apply_runtime,
                extras,
                extras,
                persist_all_presets=False,
            )
            settle_seconds = float(
                getattr(manager, "LOUDNESS_STRENGTH_VOLUME_SETTLE_SECONDS", 0.0)
            )
            if settle_seconds > 0:
                await asyncio.sleep(settle_seconds)

            effects_runtime = await self._read_and_validate_effects_runtime(extras)

        rate = dict(self._deps.get_samplerate_status())
        if rate.get("active_rate") != request.target_rate:
            raise RuntimeError(
                "target rate changed during DSP stabilization: "
                f"expected={request.target_rate} actual={rate.get('active_rate')}"
            )
        if rate.get("force_rate") not in {None, 0, request.target_rate}:
            raise RuntimeError(
                f"force-rate changed during DSP stabilization: {rate.get('force_rate')}"
            )
        graph_overview = (
            request.output_mode_target
            if request.operation == "output-mode-switch" and request.output_mode_target
            else None
        )
        source_required = bool(
            request.should_play
            or (request.operation == "output-mode-switch" and bool(request.target_url))
        )
        graph = await self._deps.playback_graph_diagnosis(
            graph_overview,
            source=request.source if source_required else None,
            target_rate=request.target_rate,
            require_source=source_required,
        )
        if not graph.get("links_complete"):
            # Link-only drift during DSP stabilization (EE reconfigures its
            # graph while the guarded runtime apply settles): one bounded
            # repair attempt, then re-read before failing the transition.
            graph_mode = graph.get("mode")
            try:
                if graph_mode == OUTPUT_MODE_STEREO:
                    await self._deps.repair_stereo_output_links_once(graph)
                elif graph_mode in OUTPUT_MODE_SUBWOOFER_MODES:
                    await self._deps.coordinator_reconcile_subwoofer_links_only()
                else:
                    graph_mode = None
                if graph_mode is not None:
                    graph = await self._deps.playback_graph_diagnosis(
                        graph_overview,
                        source=request.source if source_required else None,
                        target_rate=request.target_rate,
                        require_source=source_required,
                    )
            except Exception as exc:
                logger.warning(
                    "DSP stabilization link repair failed: %s",
                    exc,
                )
        if not graph.get("links_complete"):
            raise RuntimeError(
                "production graph changed during DSP stabilization: "
                f"{graph.get('signature')}"
            )

        result = {
            "stabilized": True,
            "no_op": False,
            "active_rate": rate.get("active_rate"),
            "force_rate": rate.get("force_rate"),
            "graph_complete": True,
            "graph_signature": graph.get("signature"),
            "effects_runtime": effects_runtime,
        }
        logger.info(
            "Playback transition post-start DSP stabilization complete: "
            "source=%s rate=%s force=%s loudness=%s autogain=%s graph=%s",
            request.source,
            result["active_rate"],
            result["force_rate"],
            effects_runtime.get("loudness"),
            effects_runtime.get("autogain"),
            result["graph_signature"],
        )
        return result

    async def set_source_volume(self, volume: int, transition_id: str) -> None:
        set_volume = getattr(self._player, "set_volume", None) if self._deps.player_is_running() else None
        if callable(set_volume):
            set_volume(volume)
        logger.info(
            "Playback transition source volume=%s transition_id=%s",
            volume,
            transition_id,
        )

    async def _verify_transition(
        self,
        request: TransitionRequest,
        *,
        require_source_volume: bool,
        require_effects_runtime: bool = True,
    ) -> dict[str, Any]:
        try:
            rate = dict(self._deps.get_samplerate_status())
        except Exception:
            rate = {}
        state = dict(self._player.state if self._player else {})
        if request.source != "spotify":
            if request.target_url and state.get("current_file") != request.target_url:
                raise RuntimeError(
                    f"MPV current_file mismatch: expected={request.target_url} actual={state.get('current_file')}"
                )
            if request.should_play and (state.get("paused") or not state.get("playing")):
                raise RuntimeError("MPV is not actually playing at transition commit")
            if not request.should_play and not state.get("paused"):
                raise RuntimeError("MPV pause state was not confirmed at transition commit")
            if require_source_volume and request.operation == "measurement-restore":
                if request.source in {"local", "radio"} and state.get("volume") != 100:
                    raise RuntimeError(
                        f"MPV source volume was not restored: {state.get('volume')}"
                    )
            elif (
                require_source_volume
                and request.should_play
                and state.get("volume") is not None
                and state.get("volume") != 100
            ):
                raise RuntimeError(f"MPV source volume was not restored: {state.get('volume')}")
        else:
            spotify_state = await self._deps.get_spotify_ui_state()
            expected_status = "Playing" if request.should_play else "Paused"
            if request.should_play and spotify_state.get("status") != expected_status:
                raise RuntimeError(f"Spotify status was not confirmed: {spotify_state.get('status')}")
            if not request.should_play and spotify_state.get("status") == "Playing":
                raise RuntimeError("Spotify pause state was not confirmed at transition commit")

        spotify_stream_rate = None
        if request.source == "spotify" and request.should_play:
            source_rate = self._deps.coordinator_source_rate("spotify", request.target_track)
            spotify_stream_rate = await self._deps.wait_for_spotify_sink_input_samplerate(expected_rate=source_rate)
            if (
                isinstance(source_rate, int)
                and spotify_stream_rate != source_rate
            ):
                raise RuntimeError(
                    "Spotify stream rate mismatch at commit: "
                    f"expected={source_rate} actual={spotify_stream_rate}"
                )

        if isinstance(request.target_rate, int) and request.target_rate > 0:
            if rate.get("active_rate") != request.target_rate:
                raise RuntimeError(
                    f"hardware rate mismatch at commit: expected={request.target_rate} actual={rate.get('active_rate')}"
                )
            if rate.get("force_rate") not in {None, 0, request.target_rate}:
                raise RuntimeError(f"force-rate mismatch at commit: {rate.get('force_rate')}")
        graph_complete = await self._deps.playback_graph_links_complete(
            source=request.source,
            target_rate=request.target_rate,
            require_source=True,
        )
        if not graph_complete:
            raise RuntimeError("production playback links were not complete at commit")

        helper_rate = None
        try:
            output_mode = (self._deps.get_audio_output_overview().get("output_mode") or {}).get("mode")
            if output_mode in OUTPUT_MODE_SUBWOOFER_MODES:
                if self._subwoofer_runtime is None:
                    raise RuntimeError("subwoofer helper runtime is not available at commit")
                helper_snapshot = self._subwoofer_runtime.snapshot()
                helper_rate = self._deps.helper_argument_sample_rate(helper_snapshot)
                if not helper_snapshot.get("active") or helper_rate != request.target_rate:
                    raise RuntimeError(
                        "subwoofer helper rate/state mismatch at commit: "
                        f"expected={request.target_rate} actual={helper_rate} "
                        f"active={helper_snapshot.get('active')}"
                    )
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"subwoofer helper readback failed at commit: {exc}") from exc

        effects_runtime = {}
        if self._easyeffects_manager and require_effects_runtime:
            preset = await asyncio.to_thread(self._easyeffects_manager.get_active_preset)
            if not preset:
                raise RuntimeError("EasyEffects active preset was not confirmed at commit")
            extras = self._easyeffects_manager.load_global_extras()
            try:
                effects_runtime = await self._read_and_validate_effects_runtime(extras)
            except RuntimeError:
                # Recoverable DSP work-point drift (e.g. a stale SPL-noise
                # state surviving an EasyEffects restart/preset reload):
                # re-apply the canonical runtime once under the still-closed
                # gate and re-validate before failing the transition.
                apply_runtime = getattr(
                    self._easyeffects_manager, "apply_autogain_loudness_runtime", None
                )
                if not callable(apply_runtime):
                    raise
                logger.warning(
                    "Playback commit effects runtime drifted; re-applying canonical "
                    "runtime: operation=%s",
                    request.operation,
                )
                # Re-acquire the mutation ownership and re-read the canonical
                # extras under it before re-applying: the runtime drift may be
                # observed while a volume/extras/SPL mutation is in flight.
                # The ownership stays held through the re-validation so the
                # re-applied runtime is verified against the very extras that
                # were re-read, not a snapshot a parallel mutation made stale.
                async with self._deps.easyeffects_mutation_lock():
                    extras = self._easyeffects_manager.load_global_extras()
                    await self._deps.drain_worker(
                        apply_runtime, extras, extras, persist_all_presets=False
                    )
                    effects_runtime = await self._read_and_validate_effects_runtime(
                        extras
                    )
        return {
            "committed": True,
            "player": state,
            "active_rate": rate.get("active_rate"),
            "force_rate": rate.get("force_rate"),
            "spotify_stream_rate": spotify_stream_rate,
            "graph_complete": True,
            "helper_rate": helper_rate,
            "source_volume": state.get("volume"),
            "effects_runtime": effects_runtime,
        }

    async def verify_transition_graph(self, request: TransitionRequest) -> dict[str, Any]:
        """Verify the production graph while the output gate is still closed."""
        return await self._verify_transition(
            request,
            require_source_volume=False,
            require_effects_runtime=False,
        )

    async def verify_measurement_entry(self, request: TransitionRequest) -> dict[str, Any]:
        """Confirm the paused measurement handoff without starting music."""
        status = dict(self._deps.get_samplerate_status())
        if not samplerate.playback_rate_aligned(status, request.target_rate):
            await self._deps.reconcile_transition_sink_rate(
                request.target_rate, reason="measurement-entry"
            )
            status = dict(self._deps.get_samplerate_status())
        if status.get("active_rate") != request.target_rate:
            raise RuntimeError(
                "measurement entry hardware rate mismatch: "
                f"expected={request.target_rate} actual={status.get('active_rate')}"
            )
        if status.get("force_rate") not in {None, 0, request.target_rate}:
            raise RuntimeError(
                "measurement entry force-rate mismatch: "
                f"expected={request.target_rate} actual={status.get('force_rate')}"
            )

        readbacks: list[dict[str, Any]] = []
        for _ in range(2):
            readbacks.append(
                await self._deps.playback_graph_diagnosis(
                    target_rate=request.target_rate,
                    require_source=False,
                )
            )
        signatures = [str(item.get("signature")) for item in readbacks]
        if not all(item.get("links_complete") for item in readbacks) or len(set(signatures)) != 1:
            final = readbacks[-1] if readbacks else {}
            self._deps.log_playback_graph_diagnosis(
                final,
                target_rate=int(request.target_rate or 0),
                reason="measurement-entry",
                detail=request.detail,
            )
            raise RuntimeError(
                "measurement entry canonical graph did not reach two stable readbacks"
            )

        if request.source in {"local", "radio"} and request.target_url:
            state = dict(self._player.state if self._player else {})
            if state.get("current_file") != request.target_url:
                raise RuntimeError(
                    "measurement entry changed the loaded music source: "
                    f"expected={request.target_url} actual={state.get('current_file')}"
                )
            if not state.get("paused"):
                raise RuntimeError("music source was not left paused for measurement")
        elif request.source == "spotify":
            spotify_state = await self._deps.get_spotify_ui_state()
            if spotify_state.get("status") == "Playing":
                raise RuntimeError("Spotify was not left paused for measurement")

        return {
            "committed": True,
            "measurement_entry": True,
            "active_rate": status.get("active_rate"),
            "force_rate": status.get("force_rate"),
            "graph_complete": True,
            "graph_signature": signatures[-1],
        }

    async def verify_output_mode_runtime(self, request: TransitionRequest) -> dict[str, Any]:
        """Confirm a target output-mode graph before its durable config write."""
        target_overview = copy.deepcopy(request.output_mode_target)
        if not target_overview:
            raise RuntimeError("output-mode transition has no target overview")
        target_rate = request.target_rate
        if not isinstance(target_rate, int) or target_rate <= 0:
            raise RuntimeError("output-mode transition has no authoritative sample rate")

        rate = dict(self._deps.get_samplerate_status())
        if not samplerate.playback_rate_aligned(rate, target_rate):
            await self._deps.reconcile_transition_sink_rate(
                target_rate, reason="output-mode-switch"
            )
            rate = dict(self._deps.get_samplerate_status())
        if rate.get("active_rate") != target_rate:
            raise RuntimeError(
                "output-mode transition hardware rate mismatch: "
                f"expected={target_rate} actual={rate.get('active_rate')}"
            )
        if rate.get("force_rate") not in {None, 0, target_rate}:
            raise RuntimeError(
                "output-mode transition force-rate mismatch: "
                f"expected={target_rate} actual={rate.get('force_rate')}"
            )

        readbacks: list[dict[str, Any]] = []
        for _ in range(2):
            readbacks.append(
                await self._deps.playback_graph_diagnosis(
                    target_overview,
                    source=(
                        request.source
                        if request.target_url or request.should_play
                        else None
                    ),
                    target_rate=target_rate,
                    require_source=bool(request.target_url or request.should_play),
                )
            )
        signatures = [str(item.get("signature")) for item in readbacks]
        if not all(item.get("links_complete") for item in readbacks) or len(set(signatures)) != 1:
            final = readbacks[-1] if readbacks else {}
            self._deps.log_playback_graph_diagnosis(
                final,
                target_rate=target_rate,
                reason="output-mode-switch",
                detail=request.detail,
            )
            raise RuntimeError(
                "output-mode graph did not reach two stable canonical readbacks"
            )

        spotify_stream_rate = None
        if request.source == "spotify" and request.should_play:
            source_rate = self._deps.coordinator_source_rate("spotify", request.target_track)
            spotify_stream_rate = await self._deps.wait_for_spotify_sink_input_samplerate(expected_rate=source_rate)
            if spotify_stream_rate != source_rate:
                raise RuntimeError(
                    "Spotify stream rate mismatch during output-mode commit: "
                    f"expected={source_rate} actual={spotify_stream_rate}"
                )

        if request.source in {"local", "radio"} and request.target_url:
            state = dict(self._player.state if self._player else {})
            if state.get("current_file") != request.target_url:
                raise RuntimeError(
                    "output-mode transition changed the loaded music source: "
                    f"expected={request.target_url} actual={state.get('current_file')}"
                )
            if request.should_play and (state.get("paused") or not state.get("playing")):
                raise RuntimeError("music transport did not resume for output-mode commit")
            if not request.should_play and not state.get("paused"):
                raise RuntimeError("music transport was not left paused for output-mode commit")
        elif request.source == "spotify":
            spotify_state = await self._deps.get_spotify_ui_state()
            if request.should_play and spotify_state.get("status") != "Playing":
                raise RuntimeError("Spotify did not resume for output-mode commit")
            if not request.should_play and spotify_state.get("status") == "Playing":
                raise RuntimeError("Spotify was not left paused for output-mode commit")

        return {
            "committed": True,
            "output_mode_graph": True,
            "graph_complete": True,
            "graph_signature": signatures[-1],
            "active_rate": rate.get("active_rate"),
            "force_rate": rate.get("force_rate"),
            "spotify_stream_rate": spotify_stream_rate,
        }

    async def commit_output_mode_runtime(self, request: TransitionRequest) -> dict[str, Any]:
        """Write the target mode only after the guarded graph readback."""
        if not request.output_mode_config:
            raise RuntimeError("output-mode transition has no durable target config")
        result = self._deps.persist_audio_output_mode(request.output_mode_config)
        return {
            "output_mode_persisted": True,
            "output_mode": dict(result.get("output_mode") or {}),
        }

    async def commit_sample_rate_policy(self, request: TransitionRequest) -> dict[str, Any]:
        if not request.sample_rate_policy:
            raise RuntimeError("sample-rate transition has no durable policy")
        policy = persist_sample_rate_policy(request.sample_rate_policy)
        return {"sample_rate_policy": policy}

    async def rollback_output_mode_runtime(
        self,
        request: TransitionRequest,
        snapshot: Mapping[str, Any] | None,
    ) -> None:
        """Restore the old mode graph/config while the failure gate is closed."""
        snapshot = snapshot or {}
        old_overview = snapshot.get("output_mode_overview")
        old_config = snapshot.get("output_mode_config")
        if not isinstance(old_overview, Mapping):
            raise RuntimeError("output-mode rollback has no previous overview")
        old_overview = copy.deepcopy(dict(old_overview))
        old_mode = (old_overview.get("output_mode") or {}).get("mode")
        old_preset = snapshot.get("ee_active_preset")
        if not isinstance(old_config, Mapping) or not old_config:
            old_output_mode = dict(old_overview.get("output_mode") or {})
            old_config = {"mode": old_mode or OUTPUT_MODE_STEREO}
            if old_mode in OUTPUT_MODE_SUBWOOFER_22_MODES:
                if old_output_mode.get("subwoofers"):
                    old_config["subwoofers"] = copy.deepcopy(old_output_mode["subwoofers"])
                if old_output_mode.get("subwoofer"):
                    old_config["subwoofer"] = copy.deepcopy(old_output_mode["subwoofer"])
            else:
                old_config["subwoofer"] = copy.deepcopy(old_output_mode.get("subwoofer") or {})
        # Restore persistence first.  If the old graph cannot be rebuilt, the
        # durable mode still cannot claim the failed target configuration.
        self._deps.persist_audio_output_mode(old_config)
        if self._easyeffects_manager is not None and old_preset:
            current_preset = self._easyeffects_manager.get_active_preset()
            if current_preset != old_preset:
                await self._deps.load_easyeffects_preset(old_preset, convolver_sample_rate_hz=request.target_rate)
        await self._deps.sync_subwoofer_runtime(
            old_overview,
            reason="coordinator-output-mode-rollback",
            target_overview=old_overview,
        )
        if old_mode in OUTPUT_MODE_SUBWOOFER_MODES:
            await self._deps.coordinator_reconcile_subwoofer_links_only()
        rollback_request = replace(request, output_mode_target=old_overview)
        await self._verify_output_mode_rollback(rollback_request, old_mode)

    async def _verify_output_mode_rollback(
        self,
        request: TransitionRequest,
        _old_mode: Any,
    ) -> None:
        readbacks = [
            await self._deps.playback_graph_diagnosis(
                request.output_mode_target,
                target_rate=request.target_rate,
                require_source=False,
            )
            for _ in range(2)
        ]
        if not all(item.get("links_complete") for item in readbacks) or len({str(item.get("signature")) for item in readbacks}) != 1:
            raise RuntimeError("previous output-mode graph could not be restored")

    async def restore_output_mode_transport(
        self,
        request: TransitionRequest,
        snapshot: Mapping[str, Any] | None,
        transition_id: str,
    ) -> None:
        """Restore playing/paused transport after the mode graph commits."""
        snapshot = snapshot or {}
        previous_player = dict(snapshot.get("player") or {})
        previous_spotify = dict(snapshot.get("spotify") or {})
        if request.source in {"local", "radio"} and self._deps.player_is_running():
            previous_volume = previous_player.get("volume")
            if isinstance(previous_volume, (int, float)):
                await self.set_source_volume(int(round(previous_volume)), transition_id)
            should_play = bool(
                previous_player.get("playing")
                and not previous_player.get("paused")
                and not previous_player.get("ended")
            )
            self._player.set_pause(not should_play)
            state = dict(self._player.state if self._player else {})
            if should_play and (state.get("paused") or not state.get("playing")):
                raise RuntimeError("local transport did not resume after output-mode commit")
            if not should_play and not state.get("paused"):
                raise RuntimeError("local pause state did not survive output-mode commit")
        elif request.source == "spotify":
            should_play = previous_spotify.get("status") == "Playing"
            if should_play:
                data = await spotify_play()
                if data.get("status") not in {"Playing", "playing"}:
                    raise RuntimeError("Spotify did not resume after output-mode commit")
            else:
                await self._deps.spotify_pause()

    async def reconcile_post_start_graph(self, request: TransitionRequest) -> dict[str, Any]:
        """Run the bounded final graph reconciliation before staged commit."""
        return await self._deps.coordinator_reconcile_post_start_graph(request)

    async def verify_committed_transition(self, request: TransitionRequest) -> dict[str, Any]:
        return await self._verify_transition(request, require_source_volume=True)

    async def pause_source_after_failure(self, request: TransitionRequest) -> None:
        if request.source == "spotify":
            try:
                await self._deps.spotify_pause()
            except Exception:
                pass
            return
        if self._deps.player_is_running():
            try:
                # Keep the safety invariant even when a lightweight test
                # adapter (or a partially initialized player) does not expose
                # a volume setter: pausing the source must never be skipped
                # because the preceding best-effort attenuation failed.
                set_volume = getattr(self._player, "set_volume", None)
                if callable(set_volume):
                    try:
                        set_volume(0)
                    except Exception:
                        logger.warning("Failed to attenuate MPV after transition failure", exc_info=True)
                self._player.set_pause(True)
            except Exception:
                logger.warning("Failed to pause MPV after transition failure", exc_info=True)
